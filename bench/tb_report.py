#!/usr/bin/env python
"""Aggregate terminal-bench matrix outputs (external bench, 3 models x 2 arms).

Layout: /tmp/tb-<TAG>-<arm>/<task>/<task>.1-of-1.<task>/results.json
  TAG  Qwen3.6=q36, Qwen2.5=q05, Qwen3.8=q38   arm  base | laya
Guard verdicts (laya arm): /tmp/tbguard-<TAG>.jsonl. Prints per-cell
accuracy and guard engagement; writes bench/runs/tb_metrics.json.
Pure counting, no LLM.
"""
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
TAGS = {"Qwen3.6": "q36", "Qwen2.5": "q05", "Qwen3.8": "q38"}

out = {}
for cell_dir in sorted(glob.glob("/tmp/tb-Qwen*-*")):
    base = os.path.basename(cell_dir)          # tb-Qwen3.6-base
    tag, arm = base[3:].rsplit("-", 1)
    if arm not in ("base", "laya") or tag not in TAGS:
        continue
    key = f"{TAGS[tag]}/{arm}"
    rows = {}
    for rf in sorted(glob.glob(os.path.join(cell_dir, "*", "*", "results.json"))):
        try:
            d = json.load(open(rf))
        except Exception:
            continue
        for r in d.get("results", []):
            al = os.path.join(os.path.dirname(rf), "agent-logs")
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
        g = f"/tmp/tbguard-{tag}.jsonl"
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
             f"{v.get('guard_mean_ms', 0)}ms") if "/laya" in k else ""
    print(f"{k:12s} {v['resolved']}/{v['n_tasks']} ({v['accuracy_pct']}%){extra}")
    fails = {t: r["failure_mode"] for t, r in v["tasks"].items() if not r["resolved"]}
    if fails:
        print("     unresolved:", fails)
print("wrote", dst)
