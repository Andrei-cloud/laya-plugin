#!/usr/bin/env python
"""Aggregate terminal-bench matrix outputs (external bench, 3 models x 2 arms).

Layout (one terminal-bench run per task):
  /tmp/tb-<gtag>-<arm>/<task>/results.json        run-level, results[] rows
  gtag  q36 | q05 | q38     arm  base | laya
Guard verdicts (laya arm): /tmp/tbguard-<gtag>.jsonl. Prints per-cell
accuracy and guard engagement; writes bench/runs/tb_metrics.json.
Pure counting, no LLM.
"""
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
GTAGS = ("q36", "q05", "q38")

out = {}
for cell_dir in sorted(glob.glob("/tmp/tb-q3*-base") + glob.glob("/tmp/tb-q3*-laya")):
    base = os.path.basename(cell_dir)          # tb-q36-base
    gtag, arm = base[3:].rsplit("-", 1)
    if arm not in ("base", "laya") or gtag not in GTAGS:
        continue
    key = f"{gtag}/{arm}"
    rows = {}
    # per-task runs: <cell>/<task>/results.json; tolerate the older
    # <cell>/<run>/<task>/<trial>/results.json layout too
    for rf in sorted(glob.glob(os.path.join(cell_dir, "*", "results.json"))
                     + glob.glob(os.path.join(cell_dir, "*", "*", "*", "results.json"))):
        try:
            d = json.load(open(rf))
        except Exception:
            continue
        for r in d.get("results", []):
            trial_dir = os.path.dirname(rf)
            al = os.path.join(trial_dir, "agent-logs")
            if not glob.glob(al):              # run-level results.json: trials nest one deeper
                trial = os.path.join(trial_dir, r.get("task_id", ""))
                al = os.path.join(trial, glob.glob(os.path.join(trial, "*"))[0][len(trial) + 1:], "agent-logs") if glob.glob(os.path.join(trial, "*")) else al
            rows[r.get("task_id")] = {
                "resolved": bool(r.get("is_resolved")),
                "failure_mode": r.get("failure_mode"),
                "episodes_logged": len(glob.glob(os.path.join(al, "episode-*"))),
            }
    n = len(rows)
    res = sum(1 for r in rows.values() if r["resolved"])
    out[key] = {"n_tasks": n, "resolved": res,
                "accuracy_pct": round(100.0 * res / max(1, n), 1),
                "tasks": rows}
    if arm == "laya":
        g = f"/tmp/tbguard-{gtag}.jsonl"
        verdicts = []
        if os.path.exists(g):
            for l in open(g):
                try:
                    verdicts.append(json.loads(l))
                except Exception:
                    pass
        out[key]["guard_calls"] = len(verdicts)
        out[key]["guard_withheld"] = sum(1 for v in verdicts
                                         if v.get("verdict") != "allow")
        out[key]["guard_mean_ms"] = round(
            sum(v.get("guard_ms", 0) for v in verdicts) / max(1, len(verdicts)), 1)

dst = os.path.join(RUNS, "tb_metrics.json")
json.dump(out, open(dst, "w"), indent=1)
for k in sorted(out):
    v = out[k]
    extra = (f" guard: {v.get('guard_calls', 0)} calls, "
             f"{v.get('guard_withheld', 0)} withheld, "
             f"{v.get('guard_mean_ms', 0)}ms") if k.endswith("/laya") else ""
    print(f"{k:12s} {v['resolved']}/{v['n_tasks']} ({v['accuracy_pct']}%){extra}")
    fails = {t: r["failure_mode"] for t, r in v["tasks"].items() if not r["resolved"]}
    if fails:
        print("     unresolved:", fails)
print("wrote", dst)
