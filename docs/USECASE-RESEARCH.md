# Use-Case Head Research Notes (anchors, 2026-09-26)

arXiv anchors per planned head (titles verified live via export.arxiv.org;
full reading list to expand during each head's round). Every head round must
cite at least these into its corpus design (what the literature treats as
the decision classes, label taxonomies, calibration practices).

## mail_sort (5-lane choice: needs_reply/updates/promotional/sales/spam)
- "A Content-Based Approach to Email Triage Action Prediction: Exploration
  and Evaluation" — action-prediction taxonomy on email, feature faces.
- "Multilingual Email Zoning" — lane zoning across languages (we are
  multilingual by encoder design).
- Prompt-injection literature for the agent-targeted screen (our regex
  rails: "ignore previous instructions" classes) — keep rails CODE-side,
  head learns lanes only.

## supervise_run (keep_waiting/answer_question/nudge/escalate/collect)
- "Knowing When to Ask for Help: Bayesian Self-Escalation in Hierarchical
  LLM Agents" — escalation policy under uncertainty; maps directly to
  escalate vs keep_waiting thresholds.
- Focus Agent / plan-reuse work — stuck-run detection signals.

## rerank-noul / search_decide (enough? noul + per-doc relevance)
- "Parallel Context Compaction for Long-Horizon LLM Agent Serving" (also
  relevant to compact_select) — sufficiency judging.
- Retrieval-feedback / multi-query rewriting papers (ReFeed et al.) —
  enough/rewrite loops.

## compact_select (keep/drop per turn)
- "Parallel Context Compaction for Long-Horizon LLM Agent Serving".
- "Conversation Tree Architecture" — what survives compaction structurally.

## choose_action (generic action slate choice)
- "When Lower Privileges Suffice: Investigating Over-Privileged Tool
  Selection in LLM Agents" — least-privilege prior fits our PPO reward
  shaping (same family as ft_tool5v2_ppo recipe).
- tool_route corpus lineage in this repo (toolace/xlam/bfcl/pi) — reuse
  plumbing verbatim.

## ladder (readiness noul)
- Self-escalation/Bayesian threshold work (same anchor as supervise).

## Method notes carried from the model repo (do not relearn the hard way)
- Iron gates in `head-finetune-pipeline` skill apply verbatim: warm-start
  continuation, schema contract diff before scaling, per-row unique session
  ids, oracle-string exclusion by construction, two oracle suites verdict,
  dry-run the trainer row-builder, one trainer per MPS device serialized.
- SFT volume HURTS guardrail-shaped heads; harvest's role for them = better
  episodes + balanced selection. Routing-shaped heads (mail/supervise/
  compact/choose) are the opposite: harvest helps SFT directly.
- PPO-after-warm for anything with an act/escalate product shape.
