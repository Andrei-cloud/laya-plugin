---
name: laya-frontier-work
description: Use when supervising delegated/background runs — Laya supervise polling with fact-override rails; never escalate blind.
license: Apache-2.0
compatibility: harness-agnostic (Hermes, opencode v2; any MCP or HTTP client)
---

# Supervising delegated runs with Laya

A delegated run needs a supervisor that does not hallucinate progress.
`supervise` asks four `noul` questions (progressing / needs_input / blocked /
done) plus one `choice` action (keep_waiting / answer_question / nudge /
escalate / collect) in one batch.

## Rails

- `done` needs confidence ≥ 0.8; `needs_input` ≥ 0.7; `blocked` a strong
  reading (≥ 0.7) **and** overall confidence ≥ 0.6.
- **Facts outrank the model.** No new output for ≥ 180 s is a *fact* — it
  overrides any model disagreement (recorded as `overridden_by_fact`).
  Silence is not progress, however the batch reads.
- Alert after a streak: the same problem twice (`alert_after = 2`) before
  waking a human. One hiccup is noise.
- **Never escalate blind:** `escalate` only with a strong, consistent
  reading (and a pending question or a blocked fact). Escalating on a wobble
  trains the human to ignore the alert.
- Engine down → `keep_waiting` (fail-open) with the fact check still
  applied: if 180 s of silence says alert, the rail alerts anyway.

## Acting on the verdict

`keep_waiting` → do nothing, poll later. `answer_question` → the delegate
asked something; answer it. `nudge` → one prompt, count the streak.
`collect` → done: gather output. `escalate` → wake the human with the
model's reason and the facts side by side.
