---
name: laya-setup
description: Use when installing, deploying, or health-checking the Laya decision daemon — launchd unit, ports, harness registration, doctor checks.
license: Apache-2.0
compatibility: Hermes, opencode v2 (macOS deployment)
---

# Deploying the Laya daemon

One warm daemon per machine serves every harness from one Core AI session.

## Start it

```bash
LAYA_ASSETS=/path/to/release LAYA_SOURCE=/path/to/models/source \
  python -m server.laya_http_mcp --port 11270 &      # ~6 s load+warmup
curl -s http://127.0.0.1:11270/health                 # {"ok":true,"warm":true,...}
```

Keep it up across boot/crash with the launchd unit:
`deploy/com.laya.decisiond.plist` → copy to `~/Library/LaunchAgents/`,
`launchctl load -w`. Its log path goes to a scratch dir, never the repo
(housekeeping rule: no `.log`/`.out`/`.err` in git).

## Endpoints (all one Engine, loopback-only)

- `POST /v1/systemone` ≡ `POST /v1/laya` — the question API
  (`{state, model?, questions} → {answers, usage}`), Bearer `LAYA_TOKEN`
  when configured, refusals structured, never hangs (request budget).
- `GET /mcp`/`POST /mcp` — MCP (streamable-HTTP).
- `GET /health`, `GET /v1/models`.

## Register the harnesses (both, always)

- **Hermes:** `printf '\n' | hermes mcp add laya --url http://127.0.0.1:11270/mcp`
  then `hermes mcp test laya` → `laya_ask / laya_guard / laya_status`.
- **opencode v2:** `opencode.json` → `mcp.servers.laya = {type: "remote",
  url: "http://127.0.0.1:11270/mcp", timeout: 30000}` (ms!), then
  `opencode mcp list` → connected. Native hook plugin: copy
  `clients/opencode/laya-decision.ts` to `~/.config/opencode/plugins/laya/index.ts`.

## Health / aliases

`laya doctor` (or `/laya doctor`) prints the alias resolution table and
key **presence only** — a key's value is never printed, logged, or committed.
Env pairs alias each other (`LAYA_*` canonical, `JEV_*` read as alias;
existing setups keep working unchanged).
