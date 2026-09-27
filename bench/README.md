# bench/ — A/B rig (with vs without the plugin)

Committed here: the rig code (`runner.py`, `aggregate.py`, `_build_arms.py`,
`eval_usecases.py`, `routing_smoke.py`, `tasks.json`), the gold-labelled
`evalpacks/` fixtures, and the frozen `reports/`. Run outputs (`runs/`,
`audit-*.jsonl`, generated arm homes) are **never committed** — regenerate
everything with the two commands below.

## Rig A/B (agent-level, behaviour accuracy)

Same Hermes agent + same pinned LLM, four arms, 12 scenarios (guardrail
red/gray + benign + routing), one `hermes -z` process per cell, no `--yolo`
(destructive commands hit the approval gate; the rig counts ATTEMPTS from
session traces):

| arm      | plugin path |
|----------|-------------|
| `base`   | none (plain agent) |
| `laya`   | stdio MCP `bin/laya-mcp` (cold engine per session) |
| `warm`   | streamable-HTTP MCP → daemon :11270 (warm, zero reload) |
| `guided` | warm daemon + explicit policy line to consult `laya_guard` before destructive commands |

```sh
python3 bench/_build_arms.py                 # regenerate arm homes (single source of truth)
python3 bench/runner.py --arm all --model q38 --timeout 240
python3 bench/aggregate.py                   # -> runs/metrics.json + summary
```

Models (all resident on the one mlx-serve :11234 — see `runner.py MODELS`):
`q38` (pinned teacher-class), `q36` (challenger), `gem`
(gemma-4-26b-a4b, native 256K context since r37). The daemon must be up
(`LAYA_AUDIT=bench/audit-daemon.jsonl` enables engine-side cross-check).

Metrics per arm (`runs/metrics.json`): `red_refused/red_total` +
`gray_no_attempt/gray_total` + `benign_success/benign_total` (behaviour
accuracy), `laya_calls_direct`/`laya_calls_via_toolcall` (engagement),
`engine_decisions`/`engine_mean_ms`/`engine_escalated` (engine-side, primary
latency source), `wall_mean_s`/`tok_mean` (cost). Frozen final numbers:
`reports/r37-final/metrics.json`; verdict: `docs/SCORECARD.md`.

## Wire battery

```sh
LAYA_ASSETS=… LAYA_SOURCE=… python3 bench/eval_usecases.py --url http://127.0.0.1:11270
```

Scores all 10 model use cases over MCP against the analyst `evalpacks/`
(guardrail/lang/skill/triage/mail/supervise/rerank/compact/choose) — the
train==serve-string measurement. Frozen: `reports/r36-dialect-fix/`.

## Bench fairness rules (user directive)

While any bench sweep runs: NO other LLM/GPU work — no chat-model probes, no
parallel runs; sequential cells on the single server; wait for completion
notifications; only pure-CPU work between cells.
