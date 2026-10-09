"""Re-score a finished run with the current metrics.json (no browser, no LLM calls).

Usage: python rescore.py results/<run-folder> [--metrics other_metrics.json]
Writes summary.md + report.html into a new folder '<run-folder>_rescored'.
"""
import argparse, glob, json, os, re, shutil, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent, metrics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--metrics")
    a = ap.parse_args()
    agent.CFG = cfg = metrics.load(a.metrics)
    src = os.path.abspath(a.run_dir)
    dst = src.rstrip("\\/") + "_rescored"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    records = []
    for f in sorted(glob.glob(os.path.join(dst, "*", "result.json"))):
        d = os.path.dirname(f)
        res = json.load(open(f, encoding="utf8"))
        base = os.path.basename(d)
        idx = int(base.split("_")[0])
        mrun = re.search(r"_run(\d+)$", base)
        auto = res.get("auto_checks")
        res["metrics"] = metrics.compute(res["judge"], auto, (auto or {}).get("failed_requests", []), res.get("console_errors", []),
                                         res.get("regression"), res["case"], cfg)
        res["judge"]["score"] = res["metrics"]["overall"]
        json.dump(res, open(f, "w", encoding="utf8"), indent=2, default=str)
        agent.write_report(res, d)
        records.append({"idx": idx, "name": res["case"]["name"], "run": int(mrun.group(1)) if mrun else 1, "outdir": d, "res": res})
    cases = {}
    for r in records:
        cases.setdefault(r["idx"], r["res"]["case"])
    cases = [cases[k] for k in sorted(cases)]
    for r in records:
        r["idx"] = sorted(set(x["idx"] for x in records)).index(r["idx"]) + 1
    runs = max(r["run"] for r in records)
    agent.finalize(os.path.basename(dst), dst, "(rescored) " + os.path.basename(src), runs, 1, 0, cases, records)


if __name__ == "__main__":
    main()
