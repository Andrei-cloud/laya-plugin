# Changelog

## v0.2.0 — question API (Jev-compatible wire, Laya-native) — in progress

**Breaking (D2):** the MCP tool surface is **replaced**, not extended —
`laya_decide` is retired; the warm daemon and stdio server now expose
`laya_ask` (raw named wire questions `noul|choice|score` with the §1
invariants), `laya_guard`, `laya_status`. Migrate: send the question in the
wire envelope `{state, model?, questions:{name:q}}` instead of the old
`{task, state, options}`. The old HTTP bridge `server/laya_http.py`
(:11260) is **deleted** — the warm daemon (:11270) serves the same clients
through the question API (`POST /v1/systemone` ≡ `POST /v1/laya`).

- New: question API router (auth via `LAYA_TOKEN` alias, 60 000-char state
  cap, structured refusals, request budget → retryable 529), wire mappers
  with invariants by construction, D6 alias resolver (`server/naming.py`),
  security core (`server/laya_api_sec.py`), engine chain-rule wire path,
  latency harness, parity ledger (`docs/LAYA-JEV-PARITY.md`).
- OpenCode native plugin retargeted to the question API (golden rule D8).

## v0.1.0 — first release
Typed-task MCP plugin (laya_decide/laya_guard/laya_status), stdio + HTTP
bridge, Round-2 A/B bench (docs/RESULTS.md).
