# Laya Plugin — Use-Case Coverage Audit & Fine-Tune Plan (2026-09-26)

Spec contract: `laya/.hermes/plans/2026-09-25-jev-parity-api-spec-v2.md`.
Model gates: `laya/docs/GO_GATE.md` (r15). Plugin rails:
`server/laya_usecases.py` (12 primitives). This audit answers: **which use
cases are covered by a trained head + accuracy gate + a benchmark, and what
must be fine-tuned and harvested to close the rest.**

## Legend
- **head** — dedicated fine-tuned head in the combined `.aimodel`
- **gate** — pre-registered accuracy gate (GO_GATE.md table)
- **bench** — measured in an agent-harness benchmark (rig and/or external)

## Coverage matrix (12 spec use cases)

| # | use case (`laya_usecases.py`) | head | gate | rig bench | external bench | status |
|---|---|---|---|---|---|---|
| 0 | `guard_command` (guardrail) | ft_gr14_ppo | ✅ red≤2 | ✅ 12-task guardrail suite | ✅ terminal-bench matrix | **covered** |
| 1 | `route_turn` (lang_route + rails) | ft_lang | ✅ ≥0.90 | ◐ routing rows (1/task) | ❌ | head ✅, bench thin |
| 2 | `rerank_passages` | base chain-0 | ❌ none | ❌ | ❌ | **no head, no gate, no bench** |
| 3 | `search_decide` (rerank + enough-noul) | base | ❌ | ❌ | ❌ | **no head, no gate, no bench** |
| 4 | `compact_select` | base | ❌ | ❌ | ❌ | **no head, no gate, no bench** |
| 5 | `skill_select` | ft_skill4_ppo | ✅ ≥0.60 | ◐ tool-route row | ❌ | head ✅, bench thin |
| 6 | `choose_action` | base | ❌ (floor 0.65 exists, untested) | ❌ | ❌ | **no head, no gate, no bench** |
| 7 | `triage_message` | base head (triage) | ✅ ≥0.85 | ❌ | ❌ | head ✅, no harness bench |
| 8 | `mail_sort` | base (regex screens + noul) | ❌ | ❌ | ❌ | **no head, no gate, no bench** |
| 9 | `supervise_run` | base | ❌ | ❌ | ❌ | **no head, no gate, no bench** |
| 10 | `plan_request` | none by design (builder + TEXT_MODEL) | n/a | ❌ | ❌ | rails-only, needs e2e bench |
| 11 | `ladder_status/choose` | state machine + base noul | ❌ | ❌ | ❌ | noul gate missing |
| 12 | `handoff_capsule` | none by design (pure text) | n/a | ❌ | ❌ | rails-only, needs e2e bench |

Trained heads in the combined asset today: **base, ft_lang, ft_tool5v2_ppo,
ft_skill4_ppo, ft_gr14_ppo (+triage-on-base)** — 6 gate rows in GO_GATE.md.
`tool_route` (ft_tool5v2_ppo) is a CLI/wire surface, not one of the 12 §2
use cases, but shares the harness bench below.

## Verdicts

**Needs additional fine-tuning (new heads), ranked by merit:**

| rank | use case | new head | why FT pays | data source plan |
|---|---|---|---|---|
| 1 | `mail_sort` | ft_mail (5-lane choice) | 5-class lane decision is exactly the trained-head shape; regex screens stay as rails | synthetic teacher corpus (mail bodies, fictitious names) 4k rows, dual-blind verified; public mail corpora DATA-ONLY intake |
| 2 | `supervise_run` | ft_supervise (5-action) | facts→action = triage-family; today base zero-shot | harvest real run-facts from Hermes `state.db` + opencode sessions (existing harvest plumbing); teacher-labeled synth scenarios 3k |
| 3 | `rerank_passages` + `search_decide` | ft_rerank-noul calibration (noul) | named backlog item #S=P3; `enough` noul is a binary head | MS-MARCO-style public passages (data-only); per-doc yes/no from teacher with blind verify |
| 4 | `compact_select` | ft_compact (keep/drop per turn) | session-disjoint real data already harvested; retention = binary head on (state, turn) | real sessions: teacher decides keep/drop with context; 3k pairs |
| 5 | `choose_action` | ft_choose (action slate choice) | same recipe as tool_route PPO (bandit); slate = generic actions | extend existing tool-call harvest with action slates; PPO-after-warm recipe |
| 6 | `ladder` (noul) | reuse ft_lang-style noul calibration | single noul, cheap | synth readiness states 1k |

**No FT needed by design (rails/pure-text), but need an e2e benchmark:**
`plan_request`, `handoff_capsule`, `route_turn` rails (rail-correctness
suite, deterministic assertions — no model).

**Gate + bench work with EXISTING heads (no training):**
`triage_message`, `route_turn` (lang), `skill_select`, tool/skill-route in
the harness sense → build per-use-case eval packs and wire them into the rig
so each has a harness-measured accuracy, not only offline-holdout accuracy.

## Research & harvest procedure (per new head — iron gates from
head-finetune-pipeline apply verbatim)

1. **Research**: arXiv scan per head (mail triage; agent-supervision
   policies; retrieval-noul/"enough" judging; context-compaction retention;
   action-selection bandits) → cite into `docs/USECASE-RESEARCH.md`.
2. **Harvest**: teacher = Qwen3.8 on :11234 (cap 4 workers); real-log
   harvesters reuse `laya/eval/make_dataset_all.py` plumbing; public corpora
   data-only via file API; per-row unique session ids; held-out oracle
   strings excluded by construction.
3. **Train**: warm-start from current combined asset (`--init-from`),
   checkpoint on in-dist val, PPO-after-warm for act/escalate-shaped heads.
4. **Regress**: frozen 239-row test bit-identical + BOTH adversarial suites
   + all existing head gates must not move (any flip = VOID, root-cause).
5. **Combine**: heads → `release/laya-combined-f16.aimodel`
   (`build_aimodel_combined.py`), daemon warm, latency bars re-probed
   (≤500 ms noul/choice, per §5).
6. **Benchmark the NEW use case**: rig eval-pack per use case (accuracy vs
   base arm) + terminal-bench integration where the use case can affect an
   agent loop (mail/supervise/compact → harness tasks that generate them).
7. **Scorecard**: every use case row gets accuracy, Δ vs base arm, latency,
   FT gate verdict → single table (model + plugin) — the paper's Table 1.

## Execution order (GPU-fair: one device user at a time)
1. [running] gemma rig sweep (replaces removed q05) → rig retest complete
2. eval-pack generator for the 4 existing-head use cases (CPU) → rig bench
   extension runs (GPU)
3. FT head #1 mail_sort: harvest→train→regress→combine→bench
4. FT heads #2–#6, same pipeline, one at a time
5. consolidated scorecard → `research-paper-writing`
