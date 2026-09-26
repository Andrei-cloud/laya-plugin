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

## Public mail-corpus survey (round-9 lever; licenses checked 2026-09-26)

Searched HF/Kaggle for real-mail datasets to buy the authentic human register
that synthetic faces could not produce (r1-r8 evidence: register transfer
dominates volume). Decided intake rule: source golds are NEVER copied into
training — lanes are always teacher-dual-blind relabeled under the plugin's
product definitions; pack-collision filter stays on; only permissive licenses
enter.

| dataset | size | license | verdict |
|---|---|---|---|
| corbt/enron-emails (raw parquet) | 493k rows / 493 MB | pdcl (public domain) | **IN** — raw human-typed bodies; the register source (172k after 1-40-word terse filter) |
| SetFit/enron_spam | 11k | apache-2.0 | **IN** — spam/ham pairs, same corpus family |
| imnim/multiclass-email-classification | 14k | mit | **IN** — sales/support/general/updates labels ≈ our lanes |
| ucirvine/sms_spam | 5.6k | CC-BY-NC-4.0 | **OUT** — non-commercial; shipped product model can't train on it |
| spamassassin public corpora | ~1.4k raw .eml | no license stated | **OUT** — unclear license; not worth the risk at n=1.4k |
| enronarchive/mail | parquet-only, uncounted | pdcl | deferred (same family as corbt; no extra yield) |
| kaggle phish sets (PhishIQ, spam-dataset-2022, phish-lmd) | various | mostly CC-BY/CC0 but URL-scraped, not prose | **OUT** — register is subject-line bait, not message bodies; wrong lever |

Harvest yield (round 9): 1421 candidates -> 995 kept after dual-blind
(426 disagreements dropped — the update<->needs_reply boundary is genuinely
hard even for the teacher; those disagreements are the hard cases, and their
survivors carry the signal). Lanes: needs_reply 473 / updates 403 / spam 111 /
promotional 8. No sales class exists in Enron-era business mail — the
sales<->spam edge must keep coming from synthetic faces.

## Public triage-corpus survey (triage FT round; licenses checked 2026-09-26)

Same intake rule as mail: source golds NEVER copied — every label is
teacher-dual-blind under the plugin's product definitions, pack rows excluded,
permissive licenses only.

| dataset | size | license | verdict |
|---|---|---|---|
| Dipe00/…enron…-llama2-7b_finetuning | 1,234 mail w/ Subject+Body | apache-2.0 | **IN** — real mail prose on the urgency axis; its 0-5 Urgency column is NOT copied, only used to seed sampling |
| mr-kush/misclassified-urgency | 1,940 | apache-2.0 | **OUT** — Nepali grievance text, wrong language/register for the pack |
| Tobi-Bueck/customer-support-tickets | 20k+ | cc-by-nc-4.0 | **OUT** — non-commercial |
| bitext customer-support chatbot | 27k | cdla-sharing-1.0 | **OUT** — bot-side utterances (replies), not inbound messages; wrong side of the wire |
| aadilsayad/email-intent-classification | small | NO license | **OUT** — unlicensed |
| faraon314/tech-support | README only | mit | **OUT** — no data files |
| WOMAD reaction-prediction (needs_reply ≈ needs_human noul) | — | — | **OUT** — not on HF under any findable org; would need direct Zimring download, deferred |

Rubric bug caught pre-training (documented in the generator): the first
triage corpus draft taught an invented urgency scale
(later|today|tonight|right-now|overdue) instead of the wire's
URGENCY_LEVELS = none|later|today|right-now|deadline — and additionally
stored the question as type "score" while the engine sends the head a
"choice" rendering ("i of N-1"), which build_sequence bakes into the prompt
string. Both fixed: corpus now stores the exact rendered choice; corpus
purged and re-harvested. This is the mail program's register law applied to
the question level: the head must train on the string the daemon sends.

## Method notes carried from the model repo (do not relearn the hard way)
- Iron gates in `head-finetune-pipeline` skill apply verbatim: warm-start
  continuation, schema contract diff before scaling, per-row unique session
  ids, oracle-string exclusion by construction, two oracle suites verdict,
  dry-run the trainer row-builder, one trainer per MPS device serialized.
- SFT volume HURTS guardrail-shaped heads; harvest's role for them = better
  episodes + balanced selection. Routing-shaped heads (mail/supervise/
  compact/choose) are the opposite: harvest helps SFT directly.
- PPO-after-warm for anything with an act/escalate product shape.
