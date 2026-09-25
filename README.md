# Laya Decision Plugin — unified harness plugin for the Laya Core AI decision asset

On-device **decision layer** for coding agents: given the agent state and a
typed question, a fine-tuned Apple Core AI model answers *guardrail*
(allow / ask_user / block), *tool_route* (which tool), *skill_route* (which
skill), *triage* (reply vs act), *lang_route* — with calibrated confidence and
an **act/escalate** readout. ~16 ms per decision on Apple Silicon GPU, fully
offline. Zero token generation: it *prevents* bad LLM tool decisions instead of
adding tokens.

Built on [`convaiinnovations/laya-multilingual`](https://huggingface.co/convaiinnovations/laya-multilingual);
combined 5-head asset: [`AndyInQtr/laya-decision-plugin`](https://huggingface.co/AndyInQtr/laya-decision-plugin)
(model repo, `laya-combined-f16.aimodel/`). Apache-2.0.

## What it does in a harness

- `laya_guard("rm -rf / --no-preserve-root")` → `block/ask_user, conf 0.79` —
  the plugin **raises caution**: the harness must ask/deny instead of auto-allow.
- `laya_decide("tool_route", state, options)` → `read_file conf 0.97` —
  routing advice with calibrated confidence; `confidence < 0.7` ⇒ `acted=false`
  ⇒ the decision is *escalated back to the LLM/human by design*.
- Never auto-allows, never loosens an existing deny; on any bridge failure it
  **no-ops** — the plugin can never hang or break the harness.

## Architecture

```
harness (Hermes / opencode / any MCP client)
   │  MCP stdio (per-session)            or  warm daemon :11270 (MCP-HTTP
   ▼                                        + question API /v1/systemone
server/laya_mcp.py            server/laya_http_mcp.py   ◄─ same Engine)
   └──────────────► server/engine.py ◄────────┘
        one Core AI session, one worker thread, shape-padded (~16 ms/call)
        core/laya_port/combined_agent.py (runtime: coreai-core, torch-free)
```

`engine.py` is the single decision core (loads
`laya-combined-f16.aimodel` once, pads every call to `L_max` — the fix that
turned per-call graph re-specialization of ~3.4 s into ~16 ms — applies each
head's fitted temperatures, entropy confidence, act floor 0.7, optional
per-decision audit JSONL via `LAYA_AUDIT`).

## Requirements

- Apple Silicon, macOS 26.5+ (Core AI runtime), Python 3.11–3.13
- `pip install coreai-core transformers numpy` (+ `mcp` for the MCP server)
- the asset + configs + tokenizer (see the HF model repo); env:
  `LAYA_ASSETS` (dir containing `laya-combined-f16.aimodel/`),
  `LAYA_SOURCE` (configs dir with `tokenizer/`), `LAYA_UNIT` (`gpu|cpu|ne`)

## Quick start

```bash
# stdio MCP server (what harnesses spawn):
LAYA_ASSETS=/path/to/release LAYA_SOURCE=/path/to/models/source \
LAYA_PYTHON=/path/to/venv/bin/python bin/laya-mcp
# warm daemon (question API + MCP-HTTP; what the opencode JS plugin and all
# warm harnesses talk to):
LAYA_UNIT=gpu python -m server.laya_http_mcp --port 11270 &  # ~6 s load+warmup
# self-tests (core + MCP transport acceptance):
LAYA_ASSETS=... LAYA_SOURCE=... python tests/acceptance_core.py && python tests/acceptance_mcp.py
```

## Harness registration

**Hermes** (MCP; no hot-reload — start a new session):
```bash
printf '\n' | hermes mcp add laya \
  --command /ABS/laya-plugin/bin/laya-mcp \
  --connect-timeout 180 \
  --env LAYA_ASSETS=/ABS/release \
  --env LAYA_SOURCE=/ABS/laya/models/source \
  --env LAYA_UNIT=gpu
hermes mcp test laya     # → Tools discovered: 3
```
Tools appear as `mcp_laya_laya_decide|laya_guard|laya_status`. A ready-made
arm profile for benchmarking: `bench/hermes-laya/`.

**opencode v2** (two equivalent paths):
- MCP (reuse the same stdio server) in `opencode.json` — schema v1 is flat
  `mcp.{name}`, v2 is `mcp.servers.{name}` (check `opencode --version`; wrong
  nesting loads silently without effect). `opencode mcp list` must show
  `✓ connected`.
- native plugin (advisory/enforce gates): `clients/opencode/laya-decision.ts`
  over the HTTP bridge — `permission.ask` can only *raise* caution
  (block→deny in `mode:"enforce"`, ask→ask in default `advisory`), plus an
  optional post-tool advisory. Copy to `~/.config/opencode/plugins/` and
  register in `opencode.json`.

**Any MCP client**: point it at `bin/laya-mcp` (stdio). **Anything else**: the
HTTP bridge (`/decide`, `/guard`, `/health`) is plain JSON over loopback.

## Warm-resident deployment (recommended for harnesses that spawn per session)

Stdio MCP servers are spawned **per session** — Hermes `-z` pays the ~8–30 s
engine load+warmup on every run. For zero-reload serving, run the daemon once:

```bash
LAYA_ASSETS=… LAYA_SOURCE=… LAYA_AUDIT=audit.jsonl \
  python -m server.laya_http_mcp --port 11270        # GET /health → warm:true
```
- **Hermes:** `printf '\n' | hermes mcp add laya --url http://127.0.0.1:11270/mcp`
  (remote transport; `hermes mcp test laya` → 3 tools)
- **opencode v2:** `{"type":"remote","url":"http://127.0.0.1:11270/mcp"}`
- Engine loads ONCE; every session reuses it (~15 ms/decision from the first
  call; verified by `tests/acceptance_warm_mcp.py`). The same daemon also serves the
  question API (`POST /v1/systemone` ≡ `/v1/laya`) for non-MCP clients like
  the opencode JS plugin.
- One daemon per Mac is enough — it serializes calls internally; keep it out
  of GPU contention with the teacher LLM when benchmarking.
- **Stay warm across everything** (boot, crash, shell exit) — *opt-in*: an
  example launchd unit is in `deploy/com.laya.decisiond.plist` (`launchctl
  load -w ~/Library/LaunchAgents/com.laya.decisiond.plist` to enable, `unload`
  + delete to stop; it is NOT installed by default — KeepAlive fights manual
  `kill`, so run it manually when you want it warm:
  `LAYA_ASSETS=… python -m server.laya_http_mcp --port 11270`). KeepAlive
  respawns within
  ~5 s (verified: kill -9 → new pid warm again, zero reload paid by callers;
  an ANE-compile crash is now survivable — worker thread catches all,
  rebuilds the agent, `laya_guard` degrades to fail-open rather than stall).



Stdio MCP servers are spawned **per session** — Hermes `-z` pays the ~8–30 s
engine load+warmup on every run. For zero-reload serving, run the daemon once:

```bash
LAYA_ASSETS=… LAYA_SOURCE=… LAYA_AUDIT=audit.jsonl \
  python -m server.laya_http_mcp --port 11270        # GET /health → warm:true
```
- **Hermes:** `printf '\n' | hermes mcp add laya --url http://127.0.0.1:11270/mcp`
  (remote transport; `hermes mcp test laya` → 3 tools)
- **opencode v2:** `{"type":"remote","url":"http://127.0.0.1:11270/mcp"}`
- Engine loads ONCE; every session reuses it (~15 ms/decision from the first
  call; verified by `tests/acceptance_warm_mcp.py`). The HTTP JSON bridge
  (`server/laya_http.py :11260`) is the same idea for non-MCP clients.
- One daemon per Mac is enough — it serializes calls internally; keep it out
  of GPU contention with the teacher LLM when benchmarking.

## Benchmark (`bench/`)

A/B/A/B: identical Hermes minimal profiles — base, +stdio plugin, +warm
daemon, +warm daemon **with the one-line consult policy** — same pinned LLM,
same 12 tasks (red refusals / gray confirms / benign / routing), destructive
commands measured as **attempts in session traces** (unattended approvals
deny-by-default + canary tripwire; nothing destructive ever ran). Dual-path
engagement: OpenAI-schema trace tool_calls ↔ engine audit JSONL (ts-window
attribution) — round 2 agrees 7↔7. Result: `docs/RESULTS.md`.
`bench/runs/metrics.json`. See `docs/PLAN.md` for the full design and
`docs/RESULTS.md` for numbers.

## Model facts (from the model repo / docs there)

- 4 GO + 1 GO-as-gate verdicts on a frozen, leak-verified 239-row test
  (guardrail red-line 2 seen/1 unseen; tool_route 0.750 @ 80% coverage;
  skill_route 0.615 @ 100%; lang 0.958; triage 0.875). ECE 0.188/0.135.
- Guardrail is a **confident gate with human-in-the-loop**, NOT frictionless
  autonomy: unseen-red band is 1–3 across seeds; keep a deterministic
  advisory backstop for catastrophic patterns.
- Specialist heads degrade **gracefully** (escalate) on foreign tool slates —
  supply your real 20-tool / skill slate via `LAYA_TOOL_SLATE` /
  `LAYA_SKILL_SLATE` (JSON) or per-call `options`.

## Layout

```
core/laya_port/    runtime inference (coreai-core; combined_agent + sequence)
server/            engine.py (decision core) · laya_mcp.py (MCP stdio) · laya_http_mcp.py (warm daemon: MCP-HTTP + question API) · laya_api_http.py (question router) · laya_api_sec.py (validation) · laya_ops.py (wire mappers) · naming.py (D6 aliasing)
bin/laya-mcp       cwd-proof stdio launcher
clients/opencode/  laya-decision.ts (v2 native plugin)
bench/             A/B rig: profiles, 12 tasks, runner, aggregator
docs/              PLAN.md · INTEGRATION.md · RESULTS.md
tests/acceptance_core.py    self-test (gate semantics)   tests/acceptance_mcp.py  MCP transport acceptance
```
