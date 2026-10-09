"""Computer-using QA agent: Playwright + vision LLM (OpenAI-compatible).

Usage:
  python agent.py tests/saucedemo.json [--headed] [--max-steps 25] [--runs N] [--workers N]
                                      [--no-checks] [--baseline save|compare] [--fast] [--no-video]
"""
import argparse, base64, datetime, hashlib, json, os, re, shutil, subprocess, threading, time
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI
from playwright.sync_api import sync_playwright

import checks, metrics, report_html

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.environ.get("LLM_MODEL", "Qwen/Qwen3.8-27B")
VIEWPORT = {"width": 1280, "height": 720}
MAX_TOKENS = 4096  # reasoning tokens count against this
EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}  # used when ask(think=False)
NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}
STUCK_WARN, STUCK_ABORT = 3, 5
PRINT_LOCK = threading.Lock()
CFG = None  # QC metric config, loaded from metrics.json in main()


def log_print(tag, msg):
    with PRINT_LOCK:
        print(f"[{tag}] {msg}" if tag else msg, flush=True)


def load_env():
    for path in (os.path.join(HERE, ".env"), os.path.join(HERE, "..", ".env")):
        if os.path.exists(path):
            for line in open(path, encoding="utf8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    global MODEL
    MODEL = os.environ.get("LLM_MODEL", MODEL)  # .env may set the model, so re-read it after loading


SYSTEM = """You are a QA testing agent controlling a web browser. You receive a test goal, a screenshot of the current page (viewport {w}x{h}), and a numbered list of interactive elements with their center coordinates.
Each turn, reply with ONE JSON object and nothing else. Allowed actions:
{{"action":"click","element":<id>}}            prefer this when the element is in the list
{{"action":"click","x":<int>,"y":<int>}}       pixel click when no element id fits
{{"action":"type","element":<id>,"text":"..."}} focuses the element, replaces its content
{{"action":"select","element":<id>,"option":"<option text>"}}  for <select> dropdowns (never click them)
{{"action":"press","key":"Enter"}}
{{"action":"scroll","dy":<int>}}                positive = down
{{"action":"goto","url":"https://..."}}
{{"action":"done","outcome":"pass"|"fail","reason":"..."}}  call when the goal is achieved, or definitively failed/blocked
Add a short "thought" field to every reply. Be efficient: do not repeat a failed action more than twice; if the app behaves unexpectedly versus the expected result, report "fail" with specifics. Cookie/consent popups are dismissed automatically."""

JUDGE = """You are a strict QA judge. Given a test case, the agent's step log, browser console errors, AUTOMATED CHECK FINDINGS and the final screenshot, grade the run against this rubric. Rate each criterion 0-100 using the anchors.
- task_completion: did the agent perform every action in the goal? 100 = all done, 50 = partly, 0 = not attempted/blocked.
- expected_result_met: does the FINAL SCREENSHOT/URL show the expected result? 100 = fully matches, 50 = partly, 0 = contradicts. Judge from evidence, not the agent's claims.
- ui_quality: VISUAL defects you can see in the screenshot only (wrong or missing images, overlapping or misaligned elements, wrong labels, unreadable text). Do NOT count console, network, accessibility or performance findings here; they are scored separately. 100 = none, deduct ~15 per defect.
- efficiency: 100 = minimal steps, 50 = some wasted steps, 0 = looped/flailing.
Reply with ONE JSON object only:
{"verdict":"pass"|"fail","criteria":{"task_completion":int,"expected_result_met":int,"ui_quality":int,"efficiency":int},"ui_issues":["..."],"summary":"..."}
verdict is "pass" only if expected_result_met >= 80 and task_completion >= 80."""

WEIGHTS = {"task_completion": 0.35, "expected_result_met": 0.45, "ui_quality": 0.10, "efficiency": 0.10}


def score_of(verdict):
    c = verdict.get("criteria") or {}
    return round(sum(float(c.get(k, 0)) * w for k, w in WEIGHTS.items()))


ELEMENTS_JS = """
() => {
  const sel = 'a,button,input,select,textarea,[role=button],[role=link],[onclick]';
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
    el.setAttribute('data-cua', out.length);
    const opts = el.tagName === 'SELECT' ? ' options=' + JSON.stringify([...el.options].map(o => o.text)) : '';
    out.push({tag: el.tagName.toLowerCase(), type: el.type || '',
      text: (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.name || '').trim().slice(0, 50) + opts,
      x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)});
    if (out.length >= 60) break;
  }
  return out;
}
"""

# Finds a visible cookie/consent/popup dismiss button inside a banner-like container.
POPUP_JS = """
() => {
  const label = /^(accept( all)?( cookies)?|allow all( cookies)?|i agree|agree|got it|ok|okay|no,? thanks|dismiss|reject all|close|continue)$/i;
  const banner = /cookie|consent|gdpr|privacy|banner|modal|popup|overlay|dialog|newsletter/i;
  for (const el of document.querySelectorAll('button,a,[role=button]')) {
    const t = (el.innerText || el.getAttribute('aria-label') || '').trim();
    if (!label.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || getComputedStyle(el).visibility === 'hidden') continue;
    let p = el, inBanner = false;
    while (p && p !== document.body) {
      const st = getComputedStyle(p);
      if (p.getAttribute('role') === 'dialog' || banner.test((p.id || '') + ' ' + (typeof p.className === 'string' ? p.className : '')) ||
          st.position === 'fixed' || st.position === 'sticky') { inBanner = true; break; }
      p = p.parentElement;
    }
    if (inBanner) return {text: t, x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)};
  }
  return null;
}
"""


# On-screen caption + click marker, shown only for the recording (removed before every screenshot/check).
OVERLAY_JS = """([text, pt]) => {
  let o = document.getElementById('cua-overlay');
  if (!o) { o = document.createElement('div'); o.id = 'cua-overlay';
    o.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;pointer-events:none;background:rgba(20,24,34,.88);color:#fff;font:600 15px system-ui,sans-serif;padding:10px 16px;';
    document.documentElement.appendChild(o); }
  o.textContent = text;
  let d = document.getElementById('cua-dot'); if (d) d.remove();
  if (pt) { d = document.createElement('div'); d.id = 'cua-dot';
    d.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;width:30px;height:30px;border-radius:50%;border:3px solid #ff3b30;background:rgba(255,59,48,.25);transform:translate(-50%,-50%);left:' + pt[0] + 'px;top:' + pt[1] + 'px;';
    document.documentElement.appendChild(d); }
}"""
HIDE_JS = "() => { for (const id of ['cua-overlay', 'cua-dot']) { const e = document.getElementById(id); if (e) e.remove(); } }"


def to_mp4(src, dst):
    """Convert Playwright's WebM recording to H.264 MP4 (plays everywhere). Returns True on success."""
    try:
        import imageio_ffmpeg
        r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-y", "-i", src,
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "26",
                            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-movflags", "+faststart", "-an", dst],
                           capture_output=True, text=True, timeout=300)
        return r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0
    except Exception:
        return False


def show_overlay(page, text, pt=None):
    try:
        page.evaluate(OVERLAY_JS, [text[:220], pt])
    except Exception:
        pass


def hide_overlay(page):
    try:
        page.evaluate(HIDE_JS)
    except Exception:
        pass


def describe(act, els):
    a = act.get("action", "?")
    label = ""
    try:
        if "element" in act:
            label = f' "{els[int(act["element"])]["text"][:30]}"'
    except Exception:
        pass
    extra = {"type": f' \u2190 {act.get("text", "")}', "select": f' \u2192 {act.get("option", "")}', "goto": f' {act.get("url", "")}',
             "press": f' {act.get("key", "")}', "scroll": f' {act.get("dy", "")}', "done": f' ({act.get("outcome", "")})'}.get(a, "")
    thought = act.get("thought", "")
    return f"{a}{label}{extra}" + (f"  |  {thought}" if thought else "")


def parse_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError(f"no JSON in reply: {(text or '')[:200]!r}")
    return json.loads(m.group(0))


def img_part(png):
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}


def ask(client, messages, usage, think=True):
    """LLM call with retry/backoff on transient API errors; accumulates token usage."""
    last = None
    for attempt in range(3):
        try:
            r = client.chat.completions.create(model=MODEL, messages=messages, max_tokens=MAX_TOKENS, temperature=0.2, extra_body=None if think else EXTRA_BODY)
            if r.usage:
                usage["prompt"] += r.usage.prompt_tokens
                usage["completion"] += r.usage.completion_tokens
            usage["calls"] += 1
            return r.choices[0].message.content or ""
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def action_key(act, png):
    k = {f: act.get(f) for f in ("action", "element", "x", "y", "text", "option", "key", "dy", "url")}
    return json.dumps(k, sort_keys=True) + hashlib.md5(png).hexdigest()


def settle(page):
    for state, ms in (("load", 5000), ("networkidle", 2500)):
        try:
            page.wait_for_load_state(state, timeout=ms)
        except Exception:
            pass


def run_case(case, opts, outdir, tag, first_run=True):
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=os.environ["OPENAI_BASE_URL"], timeout=240, max_retries=1)
    os.makedirs(outdir, exist_ok=True)
    log, t0 = [], time.time()
    usage = {"prompt": 0, "completion": 0, "calls": 0}
    failed_requests, console_errors, page_checks, checked_urls = [], [], [], set()
    dismissed, stuck_count, last_key = 0, 0, None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not opts.headed)
        vdir = os.path.join(outdir, "_video") if opts.video else None
        ctx = browser.new_context(viewport=VIEWPORT, **({"record_video_dir": vdir, "record_video_size": VIEWPORT} if vdir else {}))
        page = ctx.new_page()
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(str(e)))
        page.on("response", lambda r: failed_requests.append(f"{r.status} {r.url[:120]}") if r.status >= 400 and "favicon" not in r.url else None)
        page.on("requestfailed", lambda r: failed_requests.append(f"FAILED {r.url[:120]}") if "favicon" not in r.url else None)
        page.goto(case["url"], wait_until="load")
        agent_outcome, agent_reason = "fail", f"step limit ({opts.max_steps}) reached"
        for step in range(1, opts.max_steps + 1):
            hide_overlay(page)
            settle(page)
            page.wait_for_timeout(400)
            # recovery: auto-dismiss cookie/consent popups (bounded)
            if dismissed < 5:
                try:
                    pop = page.evaluate(POPUP_JS)
                    if pop:
                        page.mouse.click(pop["x"], pop["y"])
                        dismissed += 1
                        log.append({"step": step, "action": {"action": "auto_dismiss", "text": pop["text"]}, "result": "popup dismissed", "thought": "harness"})
                        log_print(tag, f"step {step}: auto-dismissed popup '{pop['text']}'")
                        page.wait_for_timeout(500)
                except Exception:
                    pass
            png = page.screenshot()
            open(os.path.join(outdir, f"step{step:02d}.png"), "wb").write(png)
            # automatic checks, once per distinct page
            if opts.checks:
                ukey = page.url.split("#")[0]
                if ukey not in checked_urls:
                    checked_urls.add(ukey)
                    page_checks.append(checks.check_page(page, tags=CFG["accessibility"]["axe_tags"]))
            els = page.evaluate(ELEMENTS_JS)
            if opts.video:
                show_overlay(page, f"Step {step}: thinking...")
            listing = "\n".join(f'[{i}] <{e["tag"]}{" " + e["type"] if e["type"] else ""}> "{e["text"]}" at ({e["x"]},{e["y"]})' for i, e in enumerate(els))
            history = "\n".join(f'{s["step"]}. {json.dumps(s["action"])} -> {s["result"]}' for s in log[-8:]) or "(none)"
            warn = ""
            if stuck_count >= STUCK_WARN - 1:
                warn = "\nWARNING: your last actions did not change the page. Try a different approach, or report done/fail.\n"
            user = f'TEST GOAL: {case["goal"]}\nEXPECTED RESULT: {case["expected"]}\nCURRENT URL: {page.url}\nPREVIOUS STEPS:\n{history}\n{warn}\nINTERACTIVE ELEMENTS:\n{listing}\n\nChoose the next action.'
            msgs = [{"role": "system", "content": SYSTEM.format(w=VIEWPORT["width"], h=VIEWPORT["height"])},
                    {"role": "user", "content": [{"type": "text", "text": user}, img_part(png)]}]
            try:
                act = parse_json(ask(client, msgs, usage, think=not opts.fast))
            except Exception as e:
                log.append({"step": step, "action": {"action": "invalid"}, "result": f"bad model reply: {str(e)[:120]}", "thought": ""})
                log_print(tag, f"step {step}: bad reply ({str(e)[:100]})")
                continue
            # stuck-loop detection: same action on an unchanged page
            key = action_key(act, png)
            stuck_count = stuck_count + 1 if key == last_key else 0
            last_key = key
            if stuck_count >= STUCK_ABORT - 1 and act.get("action") != "done":
                agent_outcome, agent_reason = "fail", f"agent stuck: repeated '{act.get('action')}' {stuck_count + 1} times with no page change"
                log.append({"step": step, "action": act, "result": "aborted: stuck", "thought": act.get("thought", "")})
                log_print(tag, f"step {step}: ABORT stuck loop")
                break
            if opts.video:
                pt = None
                try:
                    if act.get("action") in ("click", "type") and "element" in act:
                        e = els[int(act["element"])]
                        pt = [e["x"], e["y"]]
                    elif act.get("action") == "click" and "x" in act:
                        pt = [int(act["x"]), int(act["y"])]
                except Exception:
                    pass
                show_overlay(page, f"Step {step}: {describe(act, els)}", pt)
                page.wait_for_timeout(700)
            result = "ok"
            try:
                a = act.get("action")
                if a == "done":
                    agent_outcome, agent_reason = act.get("outcome", "fail"), act.get("reason", "")
                    log.append({"step": step, "action": act, "result": "done", "thought": act.get("thought", "")})
                    log_print(tag, f"step {step}: done -> {agent_outcome}: {agent_reason[:160]}")
                    break
                elif a == "click":
                    if "element" in act:
                        e = els[int(act["element"])]
                        page.mouse.click(e["x"], e["y"])
                    else:
                        page.mouse.click(int(act["x"]), int(act["y"]))
                elif a == "type":
                    e = els[int(act["element"])]
                    page.mouse.click(e["x"], e["y"])
                    page.keyboard.press("Control+A")
                    page.keyboard.type(str(act["text"]), delay=20)
                elif a == "select":
                    page.select_option(f'[data-cua="{int(act["element"])}"]', label=str(act["option"]))
                elif a == "press":
                    page.keyboard.press(act["key"])
                elif a == "scroll":
                    page.mouse.wheel(0, int(act.get("dy", 400)))
                elif a == "goto":
                    page.goto(act["url"], wait_until="load")
                else:
                    result = f"unknown action {a!r}"
            except Exception as e:
                result = f"error: {type(e).__name__}: {str(e)[:150]}"
            log.append({"step": step, "action": act, "result": result, "thought": act.get("thought", "")})
            log_print(tag, f"step {step}: {json.dumps(act)[:130]} -> {result}")
        hide_overlay(page)
        settle(page)
        page.wait_for_timeout(500)
        final_png = page.screenshot()
        open(os.path.join(outdir, "final.png"), "wb").write(final_png)
        final_url = page.url
        auto = None
        if opts.checks:
            ukey = final_url.split("#")[0]
            if ukey not in checked_urls:
                page_checks.append(checks.check_page(page, tags=CFG["accessibility"]["axe_tags"]))
            responsive = checks.check_responsive(page, outdir)
            auto = checks.summarize(page_checks, responsive, failed_requests, console_errors, case.get("ignore", []))
        video_file = None
        if opts.video:
            try:
                vp = page.video.path()
                page.close()
                ctx.close()
                webm = os.path.join(outdir, "session.webm")
                shutil.move(vp, webm)
                shutil.rmtree(vdir, ignore_errors=True)
                if to_mp4(webm, os.path.join(outdir, "session.mp4")):
                    os.remove(webm)
                    video_file = "session.mp4"
                else:
                    log_print(tag, "mp4 conversion failed; keeping session.webm")
                    video_file = "session.webm"
            except Exception as e:
                log_print(tag, f"video save failed: {str(e)[:100]}")
        browser.close()

    steps_txt = "\n".join(f'{s["step"]}. {json.dumps(s["action"])} -> {s["result"]}' for s in log)
    auto_txt = "\n".join(auto["issues"]) if auto and auto["issues"] else "none"
    jmsgs = [{"role": "system", "content": JUDGE},
             {"role": "user", "content": [{"type": "text", "text": f'TEST CASE: {case["name"]}\nGOAL: {case["goal"]}\nEXPECTED: {case["expected"]}\nFINAL URL: {final_url}\nAGENT SELF-REPORT: {agent_outcome} - {agent_reason}\nBROWSER CONSOLE ERRORS: {console_errors[:10]}\nAUTOMATED CHECK FINDINGS:\n{auto_txt}\nSTEP LOG:\n{steps_txt}'}, img_part(final_png)]}]
    try:
        verdict = parse_json(ask(client, jmsgs, usage))
    except Exception as e:
        verdict = {"verdict": "fail", "criteria": {}, "ui_issues": [], "summary": f"judge failed: {e}"}
    verdict["score"] = score_of(verdict)

    regression = None
    if opts.baseline == "save" and first_run:
        regression = checks.baseline_save(case, final_png, verdict)
    elif opts.baseline == "compare":
        regression = checks.baseline_compare(case, final_png, verdict, outdir)

    m = metrics.compute(verdict, auto, failed_requests, console_errors, regression, case, CFG)
    verdict["judge_score"] = verdict["score"]
    verdict["score"] = m["overall"]  # headline score now comes from the QC metric scorecard
    usage["total"] = usage["prompt"] + usage["completion"]
    return {"case": case, "agent_outcome": agent_outcome, "agent_reason": agent_reason, "judge": verdict,
            "steps": log, "final_url": final_url, "console_errors": console_errors[:10], "auto_checks": auto,
            "regression": regression, "metrics": m, "video": video_file, "tokens": usage, "popups_dismissed": dismissed, "seconds": round(time.time() - t0, 1)}


def scorecard_md(res):
    m = res["metrics"]
    out = ["## QC scorecard", "", f'Overall **{m["overall"]}/100 (grade {m["grade"]})**. Only Functional can fail; other categories warn.', "",
           "| Category | Score | Status | Key findings |", "|---|---|---|---|"]
    for k, c in m["categories"].items():
        top = "; ".join(f["text"][:90] for f in c["findings"][:2]) or "none"
        out.append(f'| {metrics.LABELS[k]} | {c["score"]} | {c["status"]} | {top} |')
    return out + [""]


def write_report(res, outdir):
    j, c, ac, rg = res["judge"], res["case"], res.get("auto_checks"), res.get("regression")
    md = [f'# QA Report: {c["name"]}', "",
          f'- **Verdict:** {str(j.get("verdict")).upper()}  |  **Score:** {j.get("score")}/100  |  **Grade:** {res["metrics"]["grade"]}',
          f'- **Agent self-report:** {res["agent_outcome"]} ({res["agent_reason"]})',
          f'- **Steps:** {len(res["steps"])}  |  **Time:** {res["seconds"]}s  |  **Final URL:** {res["final_url"]}',
          f'- **Tokens:** {res["tokens"]["total"]:,} over {res["tokens"]["calls"]} calls',
          *([f'- **Video:** [{res["video"]}]({res["video"]})'] if res.get("video") else []), f'- **Goal:** {c["goal"]}', f'- **Expected:** {c["expected"]}', "", *scorecard_md(res), "## Judge", "", j.get("summary", ""), "",
          "```json", json.dumps(j.get("criteria", {}), indent=2), "```", "",
          "**UI issues (judge):** " + ("; ".join(j.get("ui_issues") or []) or "none"), "",
          "## Automatic checks", "", (ac["status"] if ac else "disabled"), ""]
    if ac:
        md += [f"- {i}" for i in ac["issues"]] + [""]
        md += ["| Viewport | Content/viewport width | Overflow |", "|---|---|---|"]
        md += [f'| {r["viewport"]} | {r.get("scroll_width", "")}/{r.get("client_width", "")} | {"YES" if r.get("overflow") else "no"} |' for r in ac["responsive"]]
        md += ["", f'Minor/moderate a11y findings (not listed): {ac["minor_a11y_count"]}', ""]
    if rg:
        md += ["## Regression vs baseline", "", rg["status"] + (f' (diff {rg["diff_pct"]}%)' if "diff_pct" in rg else ""), ""]
    md += ["## Steps", "", "| # | Action | Result | Thought |", "|---|---|---|---|"]
    for s in res["steps"]:
        md.append(f'| {s["step"]} | `{json.dumps(s["action"])[:90]}` | {s["result"]} | {str(s["thought"])[:100]} |')
    md += ["", "## Final screenshot", "", "![final](final.png)", ""]
    open(os.path.join(outdir, "report.md"), "w", encoding="utf8").write("\n".join(md))
    json.dump(res, open(os.path.join(outdir, "result.json"), "w", encoding="utf8"), indent=2, default=str)


def finalize(stamp, stamp_dir, testfile, runs, workers, seconds, cases, records, checks_on=True):
    """Aggregate records into suite metrics and write summary.md + report.html. Also used by rescore.py."""
    rows = []
    for i, case in enumerate(cases, 1):
        recs = [r for r in records if r["idx"] == i]
        ok = [r for r in recs if r["res"]]
        passes = sum(1 for r in ok if r["res"]["metrics"]["functional_pass"])
        n = len(recs)
        last = ok[-1]["res"] if ok else None
        issues = sum(len((r["res"].get("auto_checks") or {}).get("issues", [])) for r in ok)
        rows.append({"name": case["name"], "pass_rate": passes / n, "runs": n,
                     "mean_score": round(sum(r["res"]["judge"]["score"] for r in ok) / max(len(ok), 1)),
                     "mean_steps": round(sum(len(r["res"]["steps"]) for r in ok) / max(len(ok), 1), 1),
                     "mean_secs": round(sum(r["res"]["seconds"] for r in ok) / max(len(ok), 1), 1),
                     "auto": ("n/a" if not checks_on else "clean" if not issues else f"{issues} issue(s) total"),
                     "regression": (last.get("regression") or {}).get("status", "n/a") if last else "crashed",
                     "note": last["judge"].get("summary", "") if last else "all runs crashed"})
    ok_records = [r for r in records if r["res"]]
    suite = metrics.suite([r["res"]["metrics"] for r in ok_records], len(records), CFG)
    for r in rows:
        r["grade"] = metrics.grade(r["mean_score"], CFG)
    meta = {"stamp": stamp, "testfile": testfile, "runs": runs, "workers": workers,
            "seconds": seconds, "overall": suite["pass_rate"], "mean_score": suite["overall"], "suite": suite, "cfg": CFG}

    std = {"functional": f'pass bar {CFG["release_gate"]["functional_pass_rate"]:.0%}', "accessibility": CFG["accessibility"]["standard"],
           "performance": CFG["performance"]["standard"]}
    md = [f"# QA summary {stamp}", "",
          f'**RELEASE GATE: {suite["gate"]}** - functional pass rate {suite["pass_rate"]:.0%} ({suite["passes"]}/{suite["total_runs"]} runs) vs bar {suite["bar"]:.0%}  |  **Grade {suite["grade"]} ({suite["overall"]}/100)**  |  {suite["warn_count"]} categor{"y" if suite["warn_count"] == 1 else "ies"} with warnings', "",
          f"Test file: `{testfile}` | runs/case: {runs} | workers: {workers} | wall time: {meta['seconds']}s", "",
          "## Scorecard", "", "| Category | Score | Status | Weight | Standard / bar |", "|---|---|---|---|---|"]
    for k, c in suite["categories"].items():
        md.append(f'| {metrics.LABELS[k]} | {c["score"]} | {c["status"]} | {c["weight"]} | {std.get(k, "")} |')
    md += ["", "## Top issues to fix", "", "| Severity | Category | Issue | Runs affected |", "|---|---|---|---|"]
    for it in suite["issues"][:15]:
        md.append(f'| {it["sev"]} | {metrics.LABELS[it["category"]]} | {it["text"][:150]} | {it["runs"]} |')
    md += ["", "## Cases", "", "| Case | Pass rate | Score | Grade | Steps | Time | Auto-check | Regression |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f'| {r["name"]} | {r["pass_rate"]:.0%} ({round(r["pass_rate"] * r["runs"])}/{r["runs"]}) | {r["mean_score"]} | {r["grade"]} | {r["mean_steps"]} | {r["mean_secs"]}s | {r["auto"]} | {r["regression"]} |')
    os.makedirs(stamp_dir, exist_ok=True)
    open(os.path.join(stamp_dir, "summary.md"), "w", encoding="utf8").write("\n".join(md) + "\n")
    html_path = report_html.build(stamp_dir, meta, rows, ok_records)

    print("\nSUMMARY")
    for r in rows:
        print(f'  {r["pass_rate"]:>4.0%} ({r["runs"]} runs) score {r["mean_score"]:>3} {r["grade"]}  {r["mean_steps"]:>5} steps  {r["name"]}')
    print(f'  RELEASE GATE: {suite["gate"]} (functional {suite["pass_rate"]:.0%} vs bar {suite["bar"]:.0%}) | grade {suite["grade"]} ({suite["overall"]}/100) | wall {meta["seconds"]}s | tokens {sum(r["res"]["tokens"]["total"] for r in ok_records):,}')
    for k, c in suite["categories"].items():
        print(f'    {metrics.LABELS[k]:<18}{c["score"]:>4}  {c["status"]}')
    print(f"  HTML report: {html_path}")


def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("testfile")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--runs", type=int, default=1, help="repeat each case N times; reports pass rate")
    ap.add_argument("--workers", type=int, default=1, help="run N browser sessions in parallel")
    ap.add_argument("--metrics", help="path to a QC metrics config (default metrics.json)")
    ap.add_argument("--no-video", dest="video", action="store_false", help="do not record a video of each run")
    ap.add_argument("--fast", action="store_true", help="no model reasoning for the agent's click/type steps (faster, cheaper); the judge always reasons")
    ap.add_argument("--no-checks", dest="checks", action="store_false", help="skip automatic a11y/responsive/broken-image checks")
    ap.add_argument("--baseline", choices=["save", "compare"], help="save final screenshots as baselines, or compare against them")
    opts = ap.parse_args()
    global CFG
    CFG = metrics.load(opts.metrics)
    cases = json.load(open(opts.testfile, encoding="utf8"))
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ("_fast" if opts.fast else "") + (f"_{opts.baseline}" if opts.baseline else "") + f"_{os.getpid() % 1000:03d}"
    stamp_dir = os.path.join(HERE, "results", stamp)
    t_start = time.time()

    tasks = [(i, case, r) for i, case in enumerate(cases, 1) for r in range(1, opts.runs + 1)]

    def work(task):
        i, case, r = task
        slug = re.sub(r"\W+", "_", case["name"]).strip("_").lower()
        outdir = os.path.join(stamp_dir, f"{i:02d}_{slug}" + (f"_run{r}" if opts.runs > 1 else ""))
        tag = f"{i}.{r} {case['name'][:24]}"
        log_print(tag, "start")
        try:
            res = run_case(case, opts, outdir, tag, first_run=(r == 1))
            write_report(res, outdir)
            log_print(tag, f"-> {res['judge'].get('verdict')} score={res['judge']['score']} grade={res['metrics']['grade']}")
            return {"idx": i, "name": case["name"], "run": r, "outdir": outdir, "res": res}
        except Exception as e:
            log_print(tag, f"run crashed: {type(e).__name__}: {str(e)[:200]}")
            return {"idx": i, "name": case["name"], "run": r, "outdir": outdir, "res": None, "error": str(e)}

    with ThreadPoolExecutor(max_workers=max(1, opts.workers)) as ex:
        records = list(ex.map(work, tasks))

    finalize(stamp, stamp_dir, opts.testfile, opts.runs, opts.workers, round(time.time() - t_start, 1), cases, records, opts.checks)


if __name__ == "__main__":
    main()
