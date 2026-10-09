"""Self-contained HTML report for a run (screenshots embedded)."""
import base64, html, io, os
from PIL import Image


def _img(path, width=900):
    if not os.path.exists(path):
        return ""
    im = Image.open(path).convert("RGB")
    if im.width > width:
        im = im.resize((width, int(im.height * width / im.width)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=72)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _e(x):
    return html.escape(str(x))


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2330;--mute:#667085;--line:#e4e7ec;--pass:#12805c;--fail:#c4321c;--warn:#b25e09}
@media (prefers-color-scheme:dark){:root{--bg:#10141b;--card:#181e28;--fg:#e6e9ef;--mute:#98a2b3;--line:#2a3342;--pass:#4cc38a;--fail:#ff7b66;--warn:#f2a54a}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,Segoe UI,sans-serif;padding:24px max(16px,calc(50vw - 560px))}
h1{margin:0 0 4px;font-size:22px}h2{font-size:16px;margin:0}h3{font-size:13px;margin:16px 0 6px;color:var(--mute);text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--mute);margin-bottom:20px}.kpis{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:130px}.kpi b{display:block;font-size:22px}.kpi span{color:var(--mute);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:14px;overflow:hidden}
.card>summary{list-style:none;cursor:pointer;padding:12px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.card>summary::-webkit-details-marker{display:none}.body{padding:4px 16px 16px;border-top:1px solid var(--line)}
.tag{font-weight:600;border-radius:999px;padding:1px 10px;font-size:12px;border:1px solid currentColor}.pass{color:var(--pass)}.fail{color:var(--fail)}.warn{color:var(--warn)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mute);font-weight:500}
.scroll{overflow-x:auto}img{max-width:100%;border:1px solid var(--line);border-radius:6px}.imgs{display:flex;gap:10px;flex-wrap:wrap}.imgs figure{margin:0;flex:1 1 260px}.imgs figcaption{color:var(--mute);font-size:12px}
ul{margin:4px 0;padding-left:20px}code{font-size:12px;word-break:break-all}.mute{color:var(--mute)}
"""


def build(stamp_dir, meta, rows, records):
    """rows: per-case aggregates. records: list of dicts {idx,name,run,outdir,res}."""
    total_tokens = sum(r["res"].get("tokens", {}).get("total", 0) for r in records)
    n_issue = sum(len(r["res"].get("auto_checks", {}).get("issues", [])) for r in records)
    p = ["<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
         f"<title>QA Report {_e(meta['stamp'])}</title><style>{CSS}</style>",
         f"<h1>QA Run Report</h1><div class=sub>{_e(meta['stamp'])} &middot; {_e(meta['testfile'])} &middot; {meta['runs']} run(s)/case &middot; {meta['workers']} worker(s) &middot; {meta['seconds']}s wall time</div>",
         "<div class=kpis>",
         f"<div class=kpi><b class={'pass' if meta['overall']>=0.8 else 'fail'}>{meta['overall']:.0%}</b><span>overall pass rate</span></div>",
         f"<div class=kpi><b>{meta['mean_score']}</b><span>mean score /100</span></div>",
         f"<div class=kpi><b>{len(rows)}</b><span>test cases</span></div>",
         f"<div class=kpi><b class={'warn' if n_issue else 'pass'}>{n_issue}</b><span>auto-check issues</span></div>",
         f"<div class=kpi><b>{total_tokens:,}</b><span>LLM tokens</span></div></div>",
         _suite_html(meta), "<h3>Cases</h3><div class=scroll><table><tr><th>Case<th>Pass rate<th>Score<th>Steps<th>Time<th>Auto-check<th>Regression</tr>"]
    for r in rows:
        p.append(f"<tr><td>{_e(r['name'])}<td class={'pass' if r['pass_rate']==1 else 'fail' if r['pass_rate']==0 else 'warn'}>{r['pass_rate']:.0%} ({round(r['pass_rate']*r['runs'])}/{r['runs']})"
                 f"<td>{r['mean_score']}<td>{r['mean_steps']}<td>{r['mean_secs']}s<td>{_e(r['auto'])}<td>{_e(r['regression'])}</tr>")
    p.append("</table></div><h3>Runs</h3>")
    for rec in records:
        res, j, ac = rec["res"], rec["res"]["judge"], rec["res"].get("auto_checks", {})
        ok = j.get("verdict") == "pass"
        p.append(f"<details class=card><summary><h2>{_e(rec['name'])}</h2><span class='tag {'pass' if ok else 'fail'}'>{_e(str(j.get('verdict')).upper())}</span>"
                 f"<span>score {j.get('score')} ({res['metrics']['grade']})</span><span class=mute>run {rec['run']} &middot; {len(res['steps'])} steps &middot; {res['seconds']}s</span>"
                 f"<span class='tag {'pass' if not ac.get('issues') else 'warn'}'>{_e(ac.get('status','n/a'))}</span></summary><div class=body>")
        p.append(f"<h3>Goal / expected</h3><p>{_e(res['case']['goal'])}<br><span class=mute>Expected: {_e(res['case']['expected'])}</span></p>")
        p.append(_run_scorecard(res))
        p.append(f"<h3>Judge</h3><p>{_e(j.get('summary',''))}</p><p class=mute>{_e(j.get('criteria',{}))}</p>")
        if j.get("ui_issues"):
            p.append("<ul>" + "".join(f"<li>{_e(i)}</li>" for i in j["ui_issues"]) + "</ul>")
        if ac.get("issues"):
            p.append("<h3>Automatic check findings</h3><ul>" + "".join(f"<li>{_e(i)}</li>" for i in ac["issues"]) + "</ul>")
        if ac.get("responsive"):
            p.append("<h3>Responsive</h3><div class=scroll><table><tr><th>Viewport<th>Content width<th>Overflow</tr>" + "".join(
                f"<tr><td>{_e(x['viewport'])}<td>{x.get('scroll_width','')}/{x.get('client_width','')}<td class={'fail' if x.get('overflow') else 'pass'}>{'YES' if x.get('overflow') else 'no'}</tr>" for x in ac["responsive"]) + "</table></div>")
        rg = res.get("regression")
        if rg:
            p.append(f"<h3>Regression vs baseline</h3><p>{_e(rg.get('status'))}" + (f" (diff {rg['diff_pct']}%)" if "diff_pct" in rg else "") + "</p>")
        tk = res.get("tokens", {})
        p.append(f"<p class=mute>Tokens: {tk.get('prompt',0):,} prompt + {tk.get('completion',0):,} completion over {tk.get('calls',0)} LLM calls.</p>")
        p.append("<h3>Steps</h3><div class=scroll><table><tr><th>#<th>Action<th>Result<th>Thought</tr>" + "".join(
            f"<tr><td>{s['step']}<td><code>{_e(__import__('json').dumps(s['action'])[:120])}</code><td>{_e(s['result'])[:120]}<td>{_e(str(s['thought'])[:140])}</tr>" for s in res["steps"]) + "</table></div>")
        if res.get("video"):
            rel = os.path.relpath(os.path.join(rec["outdir"], res["video"]), stamp_dir).replace("\\", "/")
            p.append(f"<h3>Recording</h3><video controls preload=metadata style='max-width:100%;border:1px solid var(--line);border-radius:6px' src='{_e(rel)}'></video>")
        imgs = [("Final page", "final.png"), ("Mobile", "responsive_mobile.png"), ("Tablet", "responsive_tablet.png"), ("Visual diff vs baseline", "diff.png")]
        figs = "".join(f"<figure><img src='{_img(os.path.join(rec['outdir'], f))}' alt='{c}'><figcaption>{c}</figcaption></figure>" for c, f in imgs if os.path.exists(os.path.join(rec["outdir"], f)))
        p.append(f"<h3>Screenshots</h3><div class=imgs>{figs}</div></div></details>")
    path = os.path.join(stamp_dir, "report.html")
    open(path, "w", encoding="utf8").write("\n".join(p))
    return path


_CHIP = {"OK": "pass", "PASS": "pass", "WARN": "warn", "FAIL": "fail"}


def _suite_html(meta):
    s, cfg = meta["suite"], meta["cfg"]
    std = {"functional": f"pass bar {cfg['release_gate']['functional_pass_rate']:.0%}", "accessibility": cfg["accessibility"]["standard"], "performance": cfg["performance"]["standard"]}
    names = {"functional": "Functional", "accessibility": "Accessibility", "technical_health": "Technical health", "visual_quality": "Visual quality",
             "responsive": "Responsive", "performance": "Performance", "regression": "Visual regression"}
    out = [f"<div class=card style='padding:16px;margin-bottom:16px'><div style='display:flex;gap:18px;align-items:center;flex-wrap:wrap'>"
           f"<span class='tag {'pass' if s['gate'] == 'PASS' else 'fail'}' style='font-size:16px;padding:4px 16px'>RELEASE GATE: {s['gate']}</span>"
           f"<span><b style='font-size:26px'>{_e(s['grade'])}</b> <span class=mute>grade ({s['overall']}/100)</span></span>"
           f"<span>Functional pass rate <b>{s['pass_rate']:.0%}</b> ({s['passes']}/{s['total_runs']} runs) <span class=mute>vs bar {s['bar']:.0%}</span></span>"
           f"<span class='{'warn' if s['warn_count'] else 'pass'}'>{s['warn_count']} categor{'y' if s['warn_count'] == 1 else 'ies'} with warnings</span></div>"]
    if meta["workers"] > 1:
        out.append(f"<p class=mute style='margin:10px 0 0'>Note: run with {meta['workers']} parallel browsers; performance numbers are noisier. Use --workers 1 for stable performance results.</p>")
    out.append("</div><h3>QC scorecard</h3><div class=scroll><table><tr><th>Category<th>Score<th>Status<th>Weight<th>Standard / bar</tr>")
    for k, c in s["categories"].items():
        out.append(f"<tr><td>{names[k]}<td><b>{c['score']}</b><td><span class='tag {_CHIP[c['status']]}'>{c['status']}</span><td>{c['weight']}<td class=mute>{_e(std.get(k, ''))}</tr>")
    out.append("</table></div><p class=mute>Only Functional can fail; all other categories warn. Scores are 0-100; a category warns below " + str(cfg["warn_below"]) + ".</p>")
    if s["issues"]:
        out.append("<h3>Top issues to fix</h3><div class=scroll><table><tr><th>Severity<th>Category<th>Issue<th>Runs</tr>")
        for it in s["issues"][:15]:
            out.append(f"<tr><td class={'fail' if it['sev'] == 'high' else 'warn' if it['sev'] == 'medium' else 'mute'}>{it['sev']}<td>{names[it['category']]}<td>{_e(it['text'][:200])}<td>{it['runs']}</tr>")
        out.append("</table></div>")
    return "".join(out)


def _run_scorecard(res):
    m = res["metrics"]
    names = {"functional": "Functional", "accessibility": "Accessibility", "technical_health": "Technical health", "visual_quality": "Visual quality",
             "responsive": "Responsive", "performance": "Performance", "regression": "Visual regression"}
    rows = "".join(f"<tr><td>{names[k]}<td>{c['score']}<td><span class='tag {_CHIP[c['status']]}'>{c['status']}</span><td>{_e('; '.join(f['text'][:110] for f in c['findings'][:3]) or 'none')}</tr>" for k, c in m["categories"].items())
    return f"<h3>QC scorecard: {m['overall']}/100 (grade {m['grade']})</h3><div class=scroll><table><tr><th>Category<th>Score<th>Status<th>Findings</tr>{rows}</table></div>"
