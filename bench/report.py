#!/usr/bin/env python
"""Render runs/metrics.json as an inline HTML report widget.

Uses the chat theme's CSS variables (no colors of its own); height adapts
to content. Clicking a metric card re-asks for that breakdown.

  python3 bench/report.py            # writes runs/report.html
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
MET = os.path.join(HERE, "runs", "metrics.json")
OUT = os.path.join(HERE, "runs", "report.html")

ARMS = ["base", "laya", "warm", "guided"]
ARM_LABEL = {"base": "base (no plugin)", "laya": "laya (stdio MCP)",
             "warm": "warm (HTTP daemon)", "guided": "guided (warm+policy)"}
MODEL_LABEL = {"q38": "Q3.8 teacher", "q36": "Q3.6-35B-A3B", "q05": "Qwen2.5-0.5B"}

ROWS = [
    ("wall_med_s", "Wall time / task (median s)", "lower", 1),
    ("tok_med", "Tokens / task (median)", "lower", 0),
    ("tool_calls_total", "Tool calls (total)", "lower", 0),
    ("timeouts", "Timeouts", "lower", 0),
    ("red_refused", "Red refused (of red_total)", "higher", 0),
    ("gray_no_attempt", "Gray asked-first (of gray_total)", "higher", 0),
    ("benign_success", "Benign success (of benign_total)", "higher", 0),
    ("laya_calls_direct", "Laya calls (direct)", "higher", 0),
    ("laya_calls_via_toolcall", "Laya calls (via tool_call)", "higher", 0),
    ("engine_decisions", "Engine decisions", None, 0),
    ("engine_mean_ms", "Engine decision latency (ms)", "lower", 1),
    ("engine_escalated", "Engine escalated/deny", None, 0),
    ("audit_file_decisions", "Audit-file decisions (xcheck)", None, 0),
]


def _cell(s, key, dec, nd):
    v = s.get(key)
    if v is None:
        return "—"
    return f"{v:.{nd}f}" if nd else str(v)


def main():
    m = json.load(open(MET))
    summ = m["summary"]
    # columns: every arm for q38, then "arm/model" keys as they appear
    cols = [a for a in ARMS if a in summ] + sorted(k for k in summ if "/" in k)

    def col_label(c):
        if "/" not in c:
            return ARM_LABEL.get(c, c) + "\u2009· " + MODEL_LABEL["q38"]
        arm, mdl = c.split("/", 1)
        return ARM_LABEL.get(arm, arm) + "\u2009· " + (MODEL_LABEL.get(mdl) or mdl)

    head = "".join(f"<th>{col_label(a)}</th>" for a in cols)
    body = []
    for key, label, better, nd in ROWS:
        cells = {a: summ[a].get(key) for a in cols}
        vals = [v for v in cells.values() if isinstance(v, (int, float))]
        best = None
        if better and len(set(vals)) > 1:
            best = max(vals) if better == "higher" else min(vals)
        tds = []
        for a in cols:
            v = cells[a]
            cls = ' class="best"' if best is not None and v == best else ""
            tds.append(f"<td{cls}>{_cell(summ[a], key, better, nd)}</td>")
        body.append(f"<tr><th>{label}</th>{''.join(tds)}</tr>")
    per = json.dumps({a: summ.get(a) for a in cols})
    import json as _j
    labels = _j.dumps([col_label(a) for a in cols])
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Laya A/B bench</title>
<style>
 table{{border-collapse:collapse;font-variant-numeric:tabular-nums}}
 th,td{{padding:4px 10px;border-bottom:1px solid var(--border);text-align:right}}
 tr>th:first-child{{text-align:left;color:var(--muted-foreground);font-weight:500}}
 thead th{{color:var(--foreground);border-bottom:2px solid var(--border)}}
 td.best{{color:var(--accent);font-weight:600}}
 .note{{color:var(--muted-foreground);font-size:.85em;margin-top:.6em;max-width:70ch}}
</style></head><body>
<table>
<thead><tr><th>metric</th>{head}</tr></thead>
<tbody>{''.join(body)}</tbody>
</table>
<div class="note">Wall/token rows compare arms within each model; wall includes
engine load (stdio) vs warm daemon. Engine decision latency = Core-AI head call
embedded in each trace. Best value per row highlighted. 12 tasks: 5 red
guardrails, 2 gray, 5 benign.</div>
<script>
const D={per};
const L={labels};
window.hermes&&hermes.send&&document.querySelectorAll('td').forEach(td=>{{
  td.style.cursor='pointer';
  td.onclick=()=>hermes.send('Break down the A/B bench result for column '
   +L[td.cellIndex-1]+': per-task table, any regressions vs its base arm.');
}});
</script>
</body></html>"""
    with open(OUT, "w") as f:
        f.write(html)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
