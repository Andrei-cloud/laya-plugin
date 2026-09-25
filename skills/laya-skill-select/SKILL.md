---
name: laya-skill-select
description: Use before loading a SKILL.md into context — two-stage Laya selection (shortlist from 120-skill batches, then verify) with anti-spurious stage 2.
license: Apache-2.0
compatibility: harness-agnostic (Hermes, opencode v2; any MCP or HTTP client)
---

# Skill selection with Laya

Loading the wrong skill costs more than loading none: it fills the window
and steers the model. Two stages, both cheap:

## Stage 1 — shortlist

One `choice` question per batch of up to 120 skills (cap 960/request),
options `S<i>: <name>: <description ≤200 chars>` plus `none`. Finalists carry
probability ≥ 0.02. Skip the model call entirely when the turn is a trivial
acknowledgement (≤ 6 words, no request in it) — most turns are.

## Stage 2 — verify (do not skip this)

Before loading anything, ask `needs_skill` (`noul`): *does this turn actually
need a skill at all?* ≥ 0.5 required. Then one `noul` per finalist: *is
`<name>` the skill this needs?* ≥ 0.5 to load. On the fleet evaluation stage 2
cut spurious picks to zero while keeping every correct pick — a confident
wrong pick (0.94) was caught exactly here.

## Rails

- Nothing passes both gates → load nothing (`skills: []`), proceed normally.
  That is a success, not a failure.
- Engine down → fail-open `skills: []`; never fall back to "load something
  anyway".
- Names must exist: verify the SKILL.md is actually reachable before you
  announce it. An unreachable suggestion is silently worse than silence.
