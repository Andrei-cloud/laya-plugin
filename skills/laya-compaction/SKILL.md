---
name: laya-compaction
description: Use when compacting a long transcript — per-turn keep/summarize/drop selection with Laya; conservative rails so cutting never loses the thread.
license: Apache-2.0
compatibility: harness-agnostic (Hermes, opencode v2; any MCP or HTTP client)
---

# Compaction selection with Laya

When a transcript must shrink, `compact_select` asks the engine per turn:
`keep`, `summarize`, or `drop`.

## Rails (conservatism by construction)

- **Drop only** when the answer is `drop` *and* confidence ≥ 0.7. Anything
  less is a keep-or-summarize — a wrong drop is unrecoverable, a wrong keep
  costs tokens.
- **Default is summarize** when unsure.
- The last 6 turns and the system prompt are **always kept**, model opinion
  notwithstanding.
- Batches ≤ 40 turns / 40 000 chars per request; overflow turns are kept
  without a model call.
- Engine down → keep everything (`fail_open: true`). A long context is a
  cost; a missing turn is an amnesia.

## What the measurements say (and why the default is humble)

On real sessions, a whole-dialogue capsule at ~1200 words recalled 58.7%
closed-book / 75.0% with one BM25 recovery — better than any model-written
per-turn digest; the model's own compaction digest *lost to a plain tail*
(37.5% closed). So: use selection to cut, but do not trust a generated
summary with the whole story — keep an exact recovery path (transcript file,
searchable) beside whatever you summarize.
