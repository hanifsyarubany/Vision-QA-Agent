"""Automatic (non-LLM) page checks and visual-regression baselines."""
import io, json, os
from PIL import Image, ImageChops

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINES = os.path.join(HERE, "baselines")
VIEWPORTS = {"mobile": (375, 667), "tablet": (768, 1024), "desktop": (1280, 720)}
_AXE = None

BROKEN_IMG_JS = "() => [...document.images].filter(i => i.complete && i.naturalWidth === 0 && i.src).map(i => i.src).slice(0, 10)"
OVERFLOW_JS = "() => ({sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth})"
AXE_JS = "async (tags) => { const r = await axe.run(document, {runOnly: tags}); return r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help, tags: v.tags, nodes: v.nodes.length})); }"
PERF_JS = """
async () => {
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const fcpE = performance.getEntriesByName('first-contentful-paint')[0];
  let lcp = null, cls = 0;
  try { new PerformanceObserver(l => { const e = l.getEntries(); lcp = e[e.length - 1].startTime; }).observe({type: 'largest-contentful-paint', buffered: true}); } catch (e) {}
  try { new PerformanceObserver(l => { for (const e of l.getEntries()) if (!e.hadRecentInput) cls += e.value; }).observe({type: 'layout-shift', buffered: true}); } catch (e) {}
  await new Promise(r => setTimeout(r, 250));
  const res = performance.getEntriesByType('resource');
  return {ttfb: nav.responseStart || null, load: nav.loadEventEnd || null, dcl: nav.domContentLoadedEventEnd || null,
          fcp: fcpE ? fcpE.startTime : null, lcp, cls, requests: res.length,
          transfer_kb: Math.round(res.reduce((a, r) => a + (r.transferSize || 0), 0) / 1024)};
}
"""


def _axe_src():
    global _AXE
    if _AXE is None:
        _AXE = open(os.path.join(HERE, "vendor", "axe.min.js"), encoding="utf8").read()
    return _AXE


def check_page(page, use_axe=True, tags=None):
    """Checks that apply to the page as currently rendered. Never raises."""
    out = {"url": page.url, "broken_images": [], "axe": [], "perf": None, "errors": []}
    tags = tags or ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]
    try:
        out["perf"] = page.evaluate(PERF_JS)
    except Exception as e:
        out["errors"].append(f"perf failed: {str(e)[:100]}")
    try:
        out["broken_images"] = page.evaluate(BROKEN_IMG_JS)
    except Exception as e:
        out["errors"].append(f"broken-image check failed: {e}")
    if use_axe:
        try:
            page.evaluate(_axe_src())  # evaluate (not add_script_tag) so page CSP cannot block it
            out["axe"] = page.evaluate(AXE_JS, tags)
        except Exception as e:
            out["errors"].append(f"axe failed: {str(e)[:120]}")
    return out


def check_responsive(page, outdir):
    """Resize through mobile/tablet/desktop, test horizontal overflow, save screenshots, restore size."""
    orig = page.viewport_size
    res = []
    try:
        for name, (w, h) in VIEWPORTS.items():
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(400)
            m = page.evaluate(OVERFLOW_JS)
            page.screenshot(path=os.path.join(outdir, f"responsive_{name}.png"))
            res.append({"viewport": f"{name} {w}x{h}", "name": name, "scroll_width": m["sw"], "client_width": m["cw"],
                        "overflow": m["sw"] > m["cw"] + 2})
    except Exception as e:
        res.append({"viewport": "error", "overflow": False, "error": str(e)[:120]})
    finally:
        if orig:
            page.set_viewport_size(orig)
            page.wait_for_timeout(300)
    return res


def summarize(pages, responsive, failed_requests, console_errors, ignore=()):
    """Collect issues. Duplicates are collapsed (with a count); `ignore` = substrings of known-noise URLs/messages."""
    def keep(text):
        return not any(i in text for i in ignore)

    issues = []
    for p in pages:
        for src in p["broken_images"]:
            if keep(src):
                issues.append(f"broken image on {p['url']}: {src[:100]}")
        for v in p["axe"]:
            if v["impact"] in ("serious", "critical"):
                issues.append(f"a11y {v['impact']} on {p['url']}: {v['id']} ({v['nodes']} nodes) - {v['help']}")
    for r in responsive:
        if r.get("overflow"):
            issues.append(f"horizontal overflow at {r['viewport']} (content {r['scroll_width']}px > viewport {r['client_width']}px)")
    reqs = [r.split("?")[0] for r in failed_requests if keep(r)]
    for url, n in dict.fromkeys((u, reqs.count(u)) for u in reqs):
        issues.append(f"failed request: {url}" + (f" (x{n})" if n > 1 else ""))
    # browser's generic "Failed to load resource" console line just repeats a failed request
    errs = [e[:140] for e in console_errors if keep(e) and not (reqs and e.startswith("Failed to load resource"))]
    for e, n in dict.fromkeys((e, errs.count(e)) for e in errs):
        issues.append(f"console error: {e}" + (f" (x{n})" if n > 1 else ""))
    minor = sum(1 for p in pages for v in p["axe"] if v["impact"] in ("minor", "moderate"))
    axe_total = sum(len(p["axe"]) for p in pages)
    return {"pages": pages, "responsive": responsive, "failed_requests": failed_requests[:20], "issues": issues,
            "minor_a11y_count": minor, "axe_violations_total": axe_total, "status": "clean" if not issues else f"{len(issues)} issue(s)"}


# ---------------------------------------------------------------- baselines / regression
def _slug(name):
    return "".join(c if c.isalnum() else "_" for c in name).strip("_").lower()


def baseline_save(case, final_png, verdict):
    d = os.path.join(BASELINES, _slug(case["name"]))
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "final.png"), "wb").write(final_png)
    json.dump({"verdict": verdict.get("verdict"), "score": verdict.get("score"), "url": case["url"]}, open(os.path.join(d, "baseline.json"), "w"))
    return {"status": "baseline saved", "path": d}


def baseline_compare(case, final_png, verdict, outdir, threshold_pct=1.0):
    d = os.path.join(BASELINES, _slug(case["name"]))
    if not os.path.exists(os.path.join(d, "final.png")):
        return {"status": "no baseline (run with --baseline save first)"}
    base = Image.open(os.path.join(d, "final.png")).convert("RGB")
    cur = Image.open(io.BytesIO(final_png)).convert("RGB")
    same_size = base.size == cur.size
    if not same_size:
        cur = cur.resize(base.size)
    diff = ImageChops.difference(base, cur).convert("L")
    changed = sum(1 for px in diff.getdata() if px > 30)
    pct = round(100 * changed / (diff.size[0] * diff.size[1]), 2)
    meta = json.load(open(os.path.join(d, "baseline.json")))
    verdict_changed = meta.get("verdict") != verdict.get("verdict")
    diff.point(lambda v: 255 if v > 30 else 0).save(os.path.join(outdir, "diff.png"))
    if verdict_changed and verdict.get("verdict") != "pass":
        status = "REGRESSION (verdict pass->fail)" if meta.get("verdict") == "pass" else "still failing"
    elif pct > threshold_pct:
        status = f"visual change {pct}%"
    else:
        status = "match"
    return {"status": status, "diff_pct": pct, "baseline_verdict": meta.get("verdict"), "verdict_changed": verdict_changed,
            "size_changed": not same_size}
