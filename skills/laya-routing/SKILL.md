---
name: laya-routing
description: Use when running several models behind one agent — Laya per-turn model routing with layered routing.json, tier walk, and sticky context guard.
license: Apache-2.0
compatibility: harness-agnostic (Hermes, opencode v2; any MCP or HTTP client)
---

# Model routing with Laya

Per turn, `route` asks the decision engine three questions in one batch —
difficulty (`score`: Trivial→Expert), kind (`choice`: coding/writing/
research/general), costly-mistake risk (`noul`) — and picks the cheapest
model tier that fits.

## Rails (code decides; the model only informs)

- **Unsure** = confidence < 0.6 → keep the current model.
- **Hard** = P(level ≥ 2) ≥ 0.6, or risk words / stakes > 0.85 ∧ p_hard ≥
  0.35 → route up to a heavy tier.
- **Simple** = P(Trivial) ≥ 0.7 ∧ confidence ≥ 0.85 → a cheap tier is fine.
- Risk words or stakes > 0.4 floor simple → medium. Never below medium for
  anything that says *production, delete, deploy, credentials, customer*.
- **Pool walk:** tier then up, never down (vision → specialty → general).
- **Sticky guard:** above 32 000 context tokens never downgrade — a mid-task
  model swap costs more than one expensive turn.
- User pinned a model (`/model`) → the pin wins, always.

## Config

`routing.json` layers, first hit wins, Laya layer wins conflicts:
`LAYA_ROUTING_CONFIG` env → `~/.config/laya/` → the `jev/` fallback dirs
(existing setups keep working). Shape: `{tiers: {tier: {pool:
["provider:model"]}}, thresholds…}`.

## Switches

`/laya routing on|shadow|off` (alias `/jev`). **shadow** decides and logs but
changes nothing — run shadow first, check `logs/laya-decisions.jsonl` (both
names carry identical lines), then flip on. Every refusal and fail-open is a
usable "kept current" answer, never an outage.
