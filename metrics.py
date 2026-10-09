"""QC scoring: turns raw run data into category scores, a grade, and a release gate.

Only 'functional' can FAIL; all other categories WARN (status OK / WARN) per the agreed policy.
"""
import json, os, re
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
IMPACT_ORDER = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}
SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
LABELS = {"functional": "Functional", "accessibility": "Accessibility", "technical_health": "Technical health",
          "visual_quality": "Visual quality", "responsive": "Responsive", "performance": "Performance", "regression": "Visual regression"}


def load(path=None):
    return json.load(open(path or os.path.join(HERE, "metrics.json"), encoding="utf8"))


def grade(score, cfg):
    for letter, lo in sorted(cfg["grade_bands"].items(), key=lambda kv: -kv[1]):
        if score >= lo:
            return letter
    return "F"


def _site(url):
    h = (urlparse(url).hostname or "").lower()
    return ".".join(h.split(".")[-2:])


def _clamp(x):
    return max(0, min(100, round(x)))


def _status(score, cfg):
    return "OK" if score >= cfg["warn_below"] else "WARN"


def _sc(rule_tags):
    """axe tag 'wcag143' -> '1.4.3'."""
    for t in rule_tags or []:
        m = re.fullmatch(r"wcag(\d)(\d)(\d+)", t)
        if m:
            return ".".join(m.groups())
    return ""


def _cat(score, cfg, findings, detail=None, status=None, warn=False):
    """warn=True forces WARN even when the score is above the threshold (hard-flag findings)."""
    st = status or ("WARN" if warn or _clamp(score) < cfg["warn_below"] else "OK")
    return {"score": _clamp(score), "status": st, "findings": findings, "detail": detail or {}}


def functional(judge, cfg):
    c = judge.get("criteria") or {}
    req = cfg["functional"]["pass_requires"]
    done, exp = float(c.get("task_completion", 0)), float(c.get("expected_result_met", 0))
    passed = judge.get("verdict") == "pass" and done >= req["task_completion"] and exp >= req["expected_result_met"]
    findings = [] if passed else [{"sev": "high", "text": "Functional failure: " + (judge.get("summary", "")[:200])}]
    return _cat((done + exp) / 2, cfg, findings, {"task_completion": done, "expected_result_met": exp}, "PASS" if passed else "FAIL")


def visual_quality(judge, cfg):
    c = judge.get("criteria") or {}
    findings = [{"sev": "medium", "text": "Visual: " + i[:200]} for i in (judge.get("ui_issues") or [])]
    return _cat(float(c.get("ui_quality", 100)), cfg, findings, {"assessed_by": "LLM judge (subjective)"}, warn=bool(findings))


def technical_health(failed_requests, console_errors, pages, site_host, ignore, cfg):
    pen = cfg["technical_health"]["penalty"]
    keep = lambda s: not any(i in s for i in ignore)
    js = sorted({e[:140] for e in console_errors if keep(e) and not e.startswith("Failed to load resource")})
    reqs = {r.split("?")[0] for r in failed_requests if keep(r)}
    same = sorted(r for r in reqs if _site(r.split(" ", 1)[-1]) == site_host)
    third = sorted(r for r in reqs if r not in same)
    broken = sorted({b for p in pages for b in p["broken_images"] if keep(b)})
    total = len(js) * pen["js_error"] + len(same) * pen["failed_request_same_site"] + len(third) * pen["failed_request_third_party"] + len(broken) * pen["broken_image"]
    f = [{"sev": "medium", "text": f"JavaScript error: {e}"} for e in js]
    f += [{"sev": "medium", "text": f"Failed request (same site): {r}"} for r in same]
    f += [{"sev": "low", "text": f"Failed request (third party): {r}"} for r in third]
    f += [{"sev": "high", "text": f"Broken image: {b[:140]}"} for b in broken]
    return _cat(100 - total, cfg, f, {"js_errors": len(js), "same_site_failures": len(same), "third_party_failures": len(third), "broken_images": len(broken)},
                warn=bool(js or same or broken))


def accessibility(pages, cfg):
    pen = cfg["accessibility"]["penalty_per_rule"]
    rules = {}
    for p in pages:
        for v in p["axe"]:
            r = rules.setdefault(v["id"], {"impact": v["impact"], "help": v["help"], "nodes": 0, "pages": set(), "sc": _sc(v.get("tags"))})
            r["nodes"] += v["nodes"]
            r["pages"].add(p["url"].split("#")[0])
            if IMPACT_ORDER.get(v["impact"], 9) < IMPACT_ORDER.get(r["impact"], 9):
                r["impact"] = v["impact"]
    total = sum(pen.get(r["impact"], 1) for r in rules.values())
    sev = {"critical": "high", "serious": "high", "moderate": "medium", "minor": "low"}
    f = [{"sev": sev.get(r["impact"], "low"), "key": f"a11y:{rid}",
          "text": f"{cfg['accessibility']['standard']} {('SC ' + r['sc'] + ' ') if r['sc'] else ''}[{r['impact']}] {rid}: {r['help']} ({r['nodes']} elements, {len(r['pages'])} page(s))"}
         for rid, r in sorted(rules.items(), key=lambda kv: (IMPACT_ORDER.get(kv[1]["impact"], 9), -kv[1]["nodes"]))]
    counts = {k: sum(1 for r in rules.values() if r["impact"] == k) for k in IMPACT_ORDER}
    return _cat(100 - total, cfg, f, {"standard": cfg["accessibility"]["standard"], "rules_violated": len(rules), **counts},
                warn=bool(counts["critical"] or counts["serious"]))


def responsive(resp, cfg):
    rs = [r for r in resp if "overflow" in r and r.get("viewport") != "error"]
    if not rs:
        return None
    bad = [r for r in rs if r["overflow"]]
    f = [{"sev": "medium", "text": f"Horizontal overflow at {r['viewport']} (content {r['scroll_width']}px in {r['client_width']}px viewport)"} for r in bad]
    return _cat(100 - 100 * len(bad) / len(rs), cfg, f, {"viewports_tested": len(rs), "viewports_overflowing": len(bad)}, warn=bool(bad))


def _tier(value, limits):
    good, poor = limits
    return "good" if value <= good else "needs_improvement" if value <= poor else "poor"


def performance(pages, cfg):
    pc = cfg["performance"]
    names = {"fcp_ms": "First Contentful Paint", "lcp_ms": "Largest Contentful Paint", "load_ms": "Page load", "cls": "Layout shift (CLS)"}
    keys = {"fcp_ms": "fcp", "lcp_ms": "lcp", "load_ms": "load", "cls": "cls"}
    scores, f, per_page = [], [], []
    for p in pages:
        pf = p.get("perf")
        if not pf:
            continue
        row = {"url": p["url"], "values": {}}
        for k, limits in pc["limits"].items():
            v = pf.get(keys[k])
            if v is None:
                continue
            tier = _tier(v, limits)
            scores.append(pc["tier_scores"][tier])
            row["values"][k] = {"value": round(v, 3 if k == "cls" else 0), "tier": tier}
            if tier != "good":
                unit = "" if k == "cls" else " ms"
                f.append({"sev": "medium" if tier == "poor" else "low", "key": f"perf:{k}:{p['url'].split('#')[0]}:{tier}",
                          "text": f"{names[k]} {round(v, 3 if k == 'cls' else 0)}{unit} is {tier.replace('_', ' ')} (good <= {limits[0]}, poor > {limits[1]}) on {p['url']}"})
        per_page.append(row)
    if not scores:
        return None
    poor = any(r["values"][k]["tier"] == "poor" for r in per_page for k in r["values"])
    return _cat(sum(scores) / len(scores), cfg, f, {"pages": per_page, "standard": pc["standard"]}, warn=poor)


def regression(rg, cfg):
    if not rg or "diff_pct" not in rg:
        return None
    pct = rg["diff_pct"]
    f = [] if rg["status"] == "match" else [{"sev": "high" if "REGRESSION" in rg["status"] else "medium", "text": f"Visual regression: {rg['status']}"}]
    return _cat(100 - pct * 2, cfg, f, {"diff_pct": pct, "status": rg["status"]}, warn=rg["status"] != "match")


def compute(judge, auto, failed_requests, console_errors, regression_res, case, cfg):
    """Per-run scorecard. `auto` is the checks.summarize() dict or None if checks were disabled."""
    ignore = case.get("ignore", [])
    host = _site(case["url"])
    cats = {"functional": functional(judge, cfg), "visual_quality": visual_quality(judge, cfg)}
    for f in cats["functional"]["findings"]:
        f["key"] = "functional:" + case["name"]
    if auto:
        cats["technical_health"] = technical_health(failed_requests, console_errors, auto["pages"], host, ignore, cfg)
        cats["accessibility"] = accessibility(auto["pages"], cfg)
        for k, v in (("responsive", responsive(auto["responsive"], cfg)), ("performance", performance(auto["pages"], cfg))):
            if v:
                cats[k] = v
    rg = regression(regression_res, cfg)
    if rg:
        cats["regression"] = rg
    w = {k: cfg["weights"].get(k, 0) for k in cats}
    tw = sum(w.values()) or 1
    overall = round(sum(cats[k]["score"] * w[k] for k in cats) / tw)
    return {"categories": cats, "overall": overall, "grade": grade(overall, cfg), "functional_pass": cats["functional"]["status"] == "PASS"}


def suite(run_metrics, total_runs, cfg):
    """Aggregate scorecard + release gate. total_runs includes crashed runs (counted as functional failures)."""
    passes = sum(1 for m in run_metrics if m["functional_pass"])
    rate = passes / total_runs if total_runs else 0
    bar = cfg["release_gate"]["functional_pass_rate"]
    cats = {}
    for name in LABELS:
        ms = [m["categories"][name] for m in run_metrics if name in m["categories"]]
        if not ms:
            continue
        s = round(sum(c["score"] for c in ms) / len(ms))
        cats[name] = {"score": s, "weight": cfg["weights"].get(name, 0), "runs_measured": len(ms),
                      "status": ("PASS" if rate >= bar else "FAIL") if name == "functional" else ("WARN" if any(c["status"] == "WARN" for c in ms) else "OK"),
                      "runs_warned": sum(1 for c in ms if c["status"] == "WARN")}
    cats["functional"]["score"] = round(rate * 100) if "functional" in cats else 0  # suite-level functional = pass rate
    w = {k: v["weight"] for k, v in cats.items()}
    tw = sum(w.values()) or 1
    overall = round(sum(cats[k]["score"] * w[k] for k in cats) / tw)
    agg = {}
    for m in run_metrics:
        for name, c in m["categories"].items():
            for f in c["findings"]:
                a = agg.setdefault((name, f.get("key") or f["text"]), {"category": name, "sev": f["sev"], "text": f["text"], "runs": 0})
                a["runs"] += 1
    top = sorted(agg.values(), key=lambda a: (SEV_ORDER[a["sev"]], -a["runs"]))
    return {"gate": "PASS" if rate >= bar else "FAIL", "pass_rate": rate, "bar": bar, "passes": passes, "total_runs": total_runs,
            "overall": overall, "grade": grade(overall, cfg), "categories": cats, "issues": top,
            "warn_count": sum(1 for k, c in cats.items() if k != "functional" and c["status"] == "WARN")}
