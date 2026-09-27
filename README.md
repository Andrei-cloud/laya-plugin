# Laya Decision Plugin — on-device decision layer for coding agents

A fine-tuned Apple Core AI model (11 chains in one `.aimodel`) answers
*guardrail* (allow/ask/block), *tool_route*, *skill_route*, *triage*,
*lang_route*, *mail_sort*, *supervise*, *choose*, *compact*, *rerank* — with
calibrated confidence, ~20–320 ms per decision, fully offline, zero token
generation. It never auto-allows, never loosens a deny, and no-ops on bridge
failure. Base checkpoint:
[`convaiinnovations/laya-multilingual`](https://huggingface.co/convaiinnovations/laya-multilingual)
· model repo: [`AndyInQtr/laya-decision-plugin`](https://huggingface.co/AndyInQtr/laya-decision-plugin)
· Apache-2.0 · consolidated results: [`docs/SCORECARD.md`](docs/SCORECARD.md).

## Install in one line

```sh
curl -fsSL https://raw.githubusercontent.com/Andrei-cloud/laya-plugin/master/install.sh | sh
```

or from a clone (agent self-pull & build):

```sh
git clone https://github.com/Andrei-cloud/laya-plugin.git && cd laya-plugin && sh install.sh
```

It pulls the model repo into
`~/.laya/model`, creates `~/.laya/venv` (coreai-core, transformers, numpy,
mcp), and prints the exact registration block for your agent — Hermes MCP,
Hermes native plugin, or opencode. Nothing is installed into your agent
automatically. Per-agent step-by-step:
[`docs/INTEGRATION.md`](docs/INTEGRATION.md).

Verify afterwards:

```sh
curl -s http://127.0.0.1:11270/health    # {"ok":true,"warm":true,"chains":[…×11…]}
```

## What it does in a harness

- `laya_guard("rm -rf / --no-preserve-root")` → `block/ask_user, conf 0.79` —
  the plugin **raises caution**: the harness must ask/deny instead of auto-allow.
- `laya_decide("tool_route", state, options)` → `read_file conf 0.97` —
  routing advice with calibrated confidence; `confidence < 0.7` ⇒ `acted=false`
  ⇒ escalated back to the LLM/human by design.

## Architecture

```
harness (Hermes / opencode / any MCP client)
   │  MCP stdio (per-session)            or  warm daemon :11270 (MCP-HTTP
   ▼                                        + question API /v1/systemone)
server/laya_mcp.py            server/laya_http_mcp.py   ◄─ same Engine
   └──────────────► server/engine.py ◄────────┘
        one Core AI session, one worker thread, shape-padded (~16 ms/call)
        core/laya_port/combined_agent.py (runtime: coreai-core, torch-free)
```

`engine.py` is the single decision core: loads `laya-combined-f16.aimodel`
once, pads every call to `L_max` (per-call re-specialization ~3.4 s → ~16 ms),
applies each chain's fitted temperatures, entropy confidence, act floor 0.7,
optional audit JSONL via `LAYA_AUDIT`.

## Requirements

- Apple Silicon, macOS 26.5+ (Core AI runtime), Python 3.11–3.13
- `pip install coreai-core transformers numpy` (+ `mcp>=1.2,<2` for the server)
- env: `LAYA_ASSETS` (dir with `laya-combined-f16.aimodel/` + provenance),
  `LAYA_SOURCE` (the model repo's `configs/` — `tokenizer/` under it),
  `LAYA_UNIT` (`gpu|cpu|ne`)

## Manual run (no install.sh)

```sh
# stdio MCP server (what harnesses spawn per session):
LAYA_ASSETS=~/.laya/model LAYA_SOURCE=~/.laya/model/configs LAYA_UNIT=gpu bin/laya-mcp
# warm daemon (recommended; question API + MCP-HTTP from one engine):
LAYA_ASSETS=… LAYA_SOURCE=… LAYA_UNIT=gpu python -m server.laya_http_mcp --port 11270 &
# keep it warm across boot/crash (opt-in launchd unit, macOS):
LAYA_PYTHON=… LAYA_ASSETS=… LAYA_SOURCE=… sh scripts/install_daemon.sh
```

Hermes registers the daemon with
`printf '\n' | hermes mcp add laya --url http://127.0.0.1:11270/mcp`;
opencode v2 uses `{"type":"remote","url":"http://127.0.0.1:11270/mcp"}` or the
native plugin `clients/opencode/laya-decision.ts`. Native Hermes plugins:
copy `hermes/plugin/hermes-laya/` (+ `hermes-laya-handoff/`) into
`~/.hermes/plugins/`. Full details: `docs/INTEGRATION.md`.

Supply your real 20-tool / skill slate via `LAYA_TOOL_SLATE` /
`LAYA_SKILL_SLATE` (JSON); foreign slates degrade gracefully to escalation,
not to wrong guesses.

## Self-test

```sh
LAYA_ASSETS=… LAYA_SOURCE=… python tests/acceptance_core.py    # gate semantics
LAYA_ASSETS=… LAYA_SOURCE=… python tests/acceptance_mcp.py     # MCP transport
python bench/routing_smoke.py                                  # daemon routes 10/10
```

## Benchmark rig (`bench/`)

Identical minimal agent profiles × 4 arms (base / guided / warm / laya) × 12
scenarios (red refusals, gray confirms, benign, routing), same pinned LLM;
destructive commands measured as **attempts in session traces** (deny-by-
default + canary; nothing destructive ever ran). Run:
`python bench/runner.py --arm all --model q38` → `python bench/aggregate.py`.
Arm homes are generated: `python bench/_build_arms.py`. Frozen final numbers:
`bench/reports/r37-final/metrics.json`; verdict: `docs/SCORECARD.md`.

## Layout

```
install.sh             one-click: model pull + venv + registration printout
core/laya_port/        runtime inference (coreai-core; combined_agent + sequence)
server/                engine.py (decision core) · laya_mcp.py (stdio MCP) ·
                       laya_http_mcp.py (warm daemon) · laya_api_http.py ·
                       laya_api_sec.py · laya_ops.py · laya_uscases.py · naming.py
bin/laya-mcp           cwd-proof stdio launcher
hermes/plugin/         hermes-laya + hermes-laya-handoff (native plugins)
clients/opencode/      laya-decision.ts (v2 native plugin)
bench/                 rig: runner.py · aggregate.py · evalpacks/ (gold fixtures)
tests/                 pytest suites + acceptance_core/mcp/warm_mcp
docs/                  SCORECARD.md (results) · INTEGRATION.md (registration) ·
                       HOUSEKEEPING.md (repo rules)
scripts/               install_daemon.sh · sync_plugin_lib.py
```
