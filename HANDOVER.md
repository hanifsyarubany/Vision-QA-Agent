# CUA QA Agent: Handover Summary

**Status:** working proof of concept (POC). Verified on SauceDemo and the public U-AIM site. **Not yet run against a production or in-house application.**
**LLM:** every model call goes to the POC Research internal LiteLLM endpoint (`OPENAI_BASE_URL` in `.env`), model `Qwen/Qwen3.8-27B`. No other AI provider is used.

---

## 1. What it is

A computer-using agent (CUA) that tests a website the way a human QA tester would. It opens a real browser, looks at the screen, clicks, types, scrolls and picks dropdown options to carry out a plain-language test, then produces a **graded QA report with evidence**: scores, screenshots, a step log and a video.

**In one line:** test cases in (JSON), graded report and video out.

## 2. How it works

For each test case the agent loops:

1. Take a screenshot and list the clickable elements on the page.
2. Send the goal, the screenshot and the element list to the LLM, which replies with one action (click, type, select, scroll, press, go to a URL, or done).
3. Run the action in the browser (Playwright). Repeat up to the step limit.
4. Run **automatic checks** that do not use the LLM: accessibility (axe-core), broken images, failed network requests, console errors, horizontal overflow at mobile/tablet/desktop widths, and performance timings.
5. A separate **judge** LLM call grades the run against the expected result, using the final screenshot and the step log.
6. The harness converts everything into a **scorecard**, a **grade** and a **release gate** (section 5).

Safety nets built in: automatic cookie/consent popup dismissal, waiting for page load, API retry and timeout, stuck-loop detection (warns after 3 repeated no-effect actions, aborts after 5), a step limit, and token counting.

## 3. Quick start

```
pip install -r requirements.txt
python -m playwright install chromium
# .env (in Icon Plus/ or CUA_project/) must contain OPENAI_API_KEY and OPENAI_BASE_URL

python agent.py tests/uaim.json                         # run a suite
python agent.py tests/uaim.json --runs 3                # repeat each case 3x, get a pass rate
python agent.py tests/uaim.json --workers 3 --fast      # parallel browsers, faster agent steps
python agent.py tests/uaim.json --workers 1             # use this for trustworthy performance numbers
python agent.py tests/uaim.json --baseline save         # store approved screenshots
python agent.py tests/uaim.json --baseline compare      # later: detect visual changes / regressions
python agent.py tests/uaim.json --headed                # watch the browser live
python generate_tests.py requirements/x.md --url https://site   # draft test cases from plain English
python rescore.py results/<run>                         # re-grade an old run after changing metrics.json
```

Other options: `--max-steps N` (default 25), `--no-checks`, `--no-video`, `--metrics other.json`.

## 4. Inputs and outputs

**Inputs**

| Input | Where |
|---|---|
| Test cases: `name`, `url`, `goal`, `expected`, optional `ignore` (known noise to skip) | `tests/*.json` |
| Requirements text (optional, to draft test cases) | `requirements/*.md` |
| API URL and key | `.env` |
| QC policy: weights, thresholds, standards, pass bar | `metrics.json` |

**Outputs** (folder `results/<timestamp>/`)

| Output | Audience |
|---|---|
| `report.html`: release gate, grade, scorecard, top issues, then per-case detail, video and screenshots | managers and developers |
| `summary.md`: the same gate, scorecard and issues as text | quick view |
| `<case>/report.md`, `result.json`: full per-run detail and raw data | developers, tools |
| `<case>/session.mp4`: H.264 video with a caption per step, a "thinking" caption and a red click marker | everyone |
| `<case>/step*.png`, `final.png`, `responsive_*.png`, `diff.png` | evidence |

## 5. QC metrics (agreed policy)

Agreed with the project owner: **all categories matter, only Functional can fail, a percentage pass bar, WCAG as the standard, and the report serves both developers and managers.**

| Category | Weight | How it is scored | Standard |
|---|---|---|---|
| Functional | 35 | judge: task completion and expected result (each must be >= 80 to pass) | suite pass bar **90%** of runs |
| Accessibility | 20 | axe rules violated, penalised by impact (critical 15, serious 8, moderate 3, minor 1) | **WCAG 2.1 AA** |
| Technical health | 15 | distinct JS errors, failed same-site/third-party requests, broken images | none |
| Visual quality | 10 | judge-assessed visual defects (subjective) | none |
| Responsive | 10 | horizontal overflow at 375 / 768 / 1280 px | none |
| Performance | 10 | First Contentful Paint, Largest Contentful Paint, page load, layout shift | Core Web Vitals limits |
| Visual regression | 0 | pixel diff vs baseline (only with `--baseline compare`) | informational |

- **Release gate:** PASS when the functional pass rate is at least 90%.
- **Grade:** A 90+, B 80+, C 70+, D 60+, otherwise F, from the weighted overall score.
- **WARN rule:** a non-functional category warns when its score is below 80, or when it has a hard-flag finding (any serious/critical accessibility rule, same-site failure, JS error, broken image, overflow, or a "poor" performance metric).
- **The weights and thresholds are my starting defaults and were not formally approved.** Change them in `metrics.json`, then use `rescore.py` to see the effect on old runs.

## 6. What has been verified

| Area | Result |
|---|---|
| LLM API (per the guide docx) | curl, SDK, streaming, system/multi-turn messages, parameters and error codes all tested. Results in `../TEST_RESULTS.md`. |
| Vision | Model reads screenshots correctly and gives click coordinates within 2 px (`../vision_check.py`). |
| SauceDemo (15 cases) | Normal flows pass. Deliberately broken cases (`problem_user`, locked-out user, wrong expectations) correctly fail. Found a real analytics 401. |
| U-AIM public site (6 cases, read-only) | Gate FAIL (83% functional vs 90% bar), grade B (85/100). Findings below. |
| Features | Parallel runs, popup handling, stuck-loop abort, baseline regression (match / visual change / pass-to-fail regression all exercised), auto-generated test cases, MP4 video, HTML reporting, re-scoring. |

**U-AIM findings (https://sanctusfactory.com/u-aim/):** no usable search box on desktop (the input exists but is 0x0 px); `$(...).slick is not a function` JavaScript error on inner pages; Publications page overflows horizontally (1273 px content in a 375 px mobile viewport, 1584 px on a 1280 px desktop); WCAG 2.1 AA violations on every page (colour contrast, unnamed frames, malformed lists, links without text); slow first load (LCP about 5 s cold, about 1.8 s warm).

## 7. Known limitations and risks

1. **Not tested on a real app.** SauceDemo is clean and stable. Expect new problems on dynamic apps.
2. **Non-deterministic.** The same test can give different verdicts between runs. Use `--runs 3` or more before trusting a result, and tune after seeing real variance.
3. **The judge is an LLM.** The Functional and Visual quality scores include model opinion. Fast mode (`--fast`) applies only to the agent's click steps. The judge always reasons, because a non-reasoning judge was too lenient in testing.
4. **Reasoning tokens count against `max_tokens`.** Too small a limit gives empty answers. The harness uses 4096 for agent steps.
5. **Generated test cases need a human skim.** The generator once invented an expectation not in the requirements.
6. **Performance is a lab measurement** from the test machine. Use `--workers 1`. The first load is slower (cold cache).
7. **Baseline comparison is a pixel diff.** Pages with dates, ads or animations produce false alarms.
8. **Untested interactions:** file uploads, iframes, new tabs, drag and drop, 2FA, CAPTCHA, very long pages, and logins that need credentials from `.env`.
9. **Responsive check covers overflow only,** not whether the layout looks good.
10. **Disk use:** about 127 MB of results so far. Old `results/` folders can be deleted.

## 8. Security and data handling

- Screenshots of tested pages, element lists and step logs are sent to the internal LLM endpoint. Use test accounts and non-production data. Check that the endpoint is approved before testing anything containing real customer data.
- The API key is in plain text in `.env`. Acceptable for a POC. For shared or scheduled use, move it to a secrets store. `.gitignore` already excludes `.env`.
- The U-AIM tests were read-only (navigation and search). No logins or form submissions were made on third-party sites. Keep it that way for sites you do not own.
- `baselines/` and `results/` contain screenshots of the tested pages.

## 9. Recommended next steps

1. **Run against a real app (highest value).** Needed: URL, 2-3 key flows, and a test login if required. Write 5-10 cases, run with `--runs 3`.
2. **Confirm the metrics:** weights, the 90% bar, and the warn threshold of 80.
3. **Decide on the U-AIM Search case:** if the site is not meant to have search, remove it and the gate passes.
4. **Add credential handling:** per-app logins from `.env`, so test files hold no passwords.
5. **Harden against real-app behaviour:** popups, uploads, iframes, multi-tab. Add masking for dynamic regions in baseline comparison.
6. **Automate:** scheduled run or run on each deploy, sending the summary. Only after the pass rate is stable.
7. **Optional:** a simple UI for editing test cases; a separate performance run with one worker.

## 10. File map

```
Icon Plus/
  POC Research Internal CUA LLM API.docx   source guide for the LLM API
  TEST_RESULTS.md, run_all_tests.py, test_llm.py   API connection tests
  vision_check.py, vision_test.png         vision capability check
  .env, .env.example, .gitignore
  CUA_project/
    agent.py          the agent, judge, orchestration, reports
    checks.py         automatic checks (axe, images, overflow, performance) and baselines
    metrics.json      QC policy (edit this)       metrics.py   scoring, grade, release gate
    report_html.py    HTML report                 rescore.py   re-grade a saved run
    generate_tests.py test cases from plain English
    tests/            test suites (saucedemo, advanced, negative, generated, uaim)
    requirements/     sample requirements         baselines/   saved reference screenshots
    vendor/axe.min.js axe-core 4.10.2, saved locally   results/   all run outputs
    README.md, HANDOVER.md, requirements.txt
```

~1,080 lines of Python across 6 modules. Reference run for demonstration: `results/20261005_231257_fast_728/report.html` (U-AIM, with videos).
