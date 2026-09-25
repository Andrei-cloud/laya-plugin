---
name: laya-guard
description: Use before running any destructive or irreversible command — ask laya_guard for allow/confirm/deny first. Mandatory pre-exec safety gate.
license: Apache-2.0
compatibility: harness-agnostic (Hermes, opencode v2; any MCP or HTTP client)
---

# Guard before you run

Destructive commands are cheap to regret. Before executing a shell command
that can delete, overwrite, spend, or send, call **`laya_guard`** with the
exact command string.

## Verdicts

- **`allow`** — safe to run as-is.
- **`confirm`** — possible risk: stop and get explicit human confirmation.
- **`deny`** — refuse: destructive or policy-violating.

Act only on `allow`. `confirm`/`deny` are not suggestions — ask the human,
with the model's stated reason and confidence.

## Rules

- Pass the **exact** command you are about to run, not a paraphrase. The
  model was trained on "About to run: <command>" states; paraphrase changes
  the distribution it sees.
- One rail: `allow` requires confidence ≥ 0.7 (deployed). Below it the tool
  already returns `confirm` — do not second-guess it back to allow.
- The gate can only RAISE caution. It never downgrades a deny that your
  harness already made, and it must never be used to auto-allow anything.
- If the guard call fails or times out, that is NOT an allow. Fall back to
  your harness's normal approval flow.
- `rm -rf /`-class commands must always end in `confirm`/`deny` regardless
  of model confidence — if you ever see them allowed, stop and report it.

## When the model disagrees with the rails

Code rails (red-line lists, harness approvals, canaries) outrank the model.
The model adds calibration, not permission.
