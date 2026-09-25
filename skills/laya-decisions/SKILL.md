---
name: laya-decisions
description: Use when a cheap, calibrated decision (yes/no, closed-set pick, rubric rating) is needed faster than asking the big model — local Core AI decision engine.
license: Apache-2.0
compatibility: harness-agnostic (Hermes, opencode v2; any MCP or HTTP client)
---

# Laya decisions

Laya is an on-device decision engine that answers three kinds of questions in
milliseconds with calibrated probabilities. Use it for the cheap decisions so
the big model spends its context on the hard ones.

## Tools

- **`laya_ask`** — one call, many named questions, one answer each:
  - `noul` — probability of yes to a single yes/no question.
  - `choice` — pick one option from a closed set; returns full calibrated
    `probabilities` plus `choice` and `confidence`.
  - `score` — rate against an ordered rubric (lowest level first, e.g.
    `Trivial → Expert`); returns per-level `probabilities` and `score` =
    Σ level·p. The spread is the real signal: a mid `score` with mass on the
    extremes is *unsure*, not "medium".
- **`laya_guard`** — safety verdict for one exact command before you run it
  (see the laya-guard skill).
- **`laya_status`** — engine health, chains, mean latency.

## The acting rule (deployed rail)

`confidence ≥ 0.7` → act on the answer. Below that the engine is telling you
it does not know: escalate to the LLM or a human. Never invent a threshold
per call-site — one rail, everywhere.

## Question hygiene

- `instructions` is the whole question, non-empty, and must differ from the
  question's own name.
- `noul` carries **no** criteria. `choice` needs ≥ 2 options with meanings.
  `score` needs ≥ 2 rubric levels, lowest first.
- State is ≤ 60 000 JSON chars. Put only what the decision needs; strip
  secrets before asking (the server redacts, but do not send them).

If the engine is down or refuses, every answer degrades to a usable
fail-open default — take that as "ask the big model", not as an error.
