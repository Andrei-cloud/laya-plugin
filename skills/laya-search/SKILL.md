---
name: laya-search
description: Use during iterative search — decide with Laya whether what you have retrieved is enough to answer or you must search more.
license: Apache-2.0
compatibility: harness-agnostic (Hermes, opencode v2; any MCP or HTTP client)
---

# Search sufficiency with Laya

Each search round: rerank the retrieved passages (see the laya-memory skill),
then ask two more questions before deciding anything:

1. **`enough`** (`noul`, threshold 0.5) — can the query be answered from
   what passed screening *right now*?
2. **`next_query`** (`choice` over `q0…qN` reformulations + `none`) — if not
   enough, which new query adds the most, while `round < max_rounds`.

The decision is exactly one of: `answer`, `search_more`,
`propose_queries`, `answer_from_what_we_have`, `unknown`.

## Rails

- `enough` needs confidence past the acting rail (0.7 deployed) to stop
  searching on the model's word alone; below that, one more round is cheap,
  a hallucinated answer is not.
- Never answer `enough` when the top passages conflict with each other —
  conflict means search more or say unknown.
- Engine down → `decision: "unknown"`, `sufficiency: null` (fail-open).
  Treat unknown as: finish the round by your own judgement and say that
  Laya was unavailable.
- One search that actually retrieves beats three that repeat themselves:
  drop a reformulation a previous round already tried.
