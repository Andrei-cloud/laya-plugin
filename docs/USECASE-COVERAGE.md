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
1. [DONE 2026-09-26] gemma rig sweep 48/48 (replaces removed q05) → table +
   verdict in bench/reports/external-20260926/REPORT.md §1. Key finding:
   tool-exposed laya arm engages 0/12 on gemma (model-independent failure);
   only the guided arm (guaranteed engagement) is fully compliant.
2. [DONE 2026-09-26] full `eval_usecases.py` run at zero GPU load. Honest
   baselines (after fixing two wire-routing bugs — lang slate unknown to
   `_match_slate` rode chain-0 at chance; unknown route_task laundered as
   529): guardrail 0.315 exact (red-line at floor: 1/91; 15 sub-floor allows
   all escalate), lang 0.829 (ft_lang chain), skill 0.416 (ack-gate kills
   20/111 gold rows + stage-2 noul vetoes; head offline-verified), triage
   0.375 (now/today confusion — FT candidate), mail 0.250 (pre-FT chain-0,
   coverage 0.65). Artifacts: bench/reports/external-20260926/usecase_eval_baseline.json.
3. FT head #1 mail_sort: harvest→train→regress→combine→bench
4. FT heads #2–#6, same pipeline, one at a time
5. consolidated scorecard → `research-paper-writing`

## Round spec — mail_sort (ft_mail), pre-registered BEFORE any result

Corpus: `eval/make_synth_mail.py` (teacher-golded, dual-blind verified,
unique session ids, faces: personal/work/promo/sales/spam/injection/
multilingual/obfuscated). Intake via `make_ft_split.py` open-class policy
(train/val/open_holdout only; frozen 239 test bit-identical — diff the
per-bucket manifest against the previous commit, assert equality).

Gates (all must hold to promote; any miss = VOID or NO-GO as marked):
- G1 held-out pack: `scripts/eval_mail.py` ft vs base — lane acc >= 0.85
  AND coverage >= 0.80 at floor 0.7 (base is expected near chance: foreign
  slate rides chain-0 by the chain rule).
- G2 no-regression: frozen 239 test + adversarial_guard + adversarial_guard2
  via the standard `eval_routed.py`/`eval_adversarial.py` — every EXISTING
  head's numbers must be bit-comparable to GO_GATE r15 (task_filter keeps
  the mail round specialist; regressions = VOID round, root-cause first).
- G3 schema contract: dry-run the trainer row-builder over every split row
  (30-second whole-corpus check) BEFORE launching the round.
- G4 combine: new chain appended to `HEADS_ORDER` +
  `TASK_ROUTE["mail_sort"]=<idx>` in `src/laya_port/combined.py`, rebuild
  via `scripts/build_aimodel_combined.py` into a NEW release dir, daemon
  restart + `/health` warm + `probe_live_m1.py` all-pass + latency bars.
- G5 wire bench: daemon-side `eval_usecases.py --only mail` (pack scores
  through the REAL /v1/laya wire, not only the Python forward).

Promotion note: the mail head is choice-only (no act/escalate product) —
SFT-first; PPO-after-warm only if val acc >= 0.9 but held-out coverage <
0.80 (PPO recipe exists to buy coverage, per r15).

ROUND 1 VERDICT (2026-09-26): NO-GO. G1 measured 0.45/0.65 (ft_mail, T=1.0
after coverage-aware refit) vs base 0.25/0.50 — signal, below gate. Raw-T1
probe separated the causes: lane argmax 0.67 val / 0.70 pack (no
generalization cliff; trainer val_acc 0.821 was a token-row averaging
artifact) + NLL temp 2.742 flattened the head under the floor. Round 2
(corpus x3, warm continuation from ft_mail, temp refit in-pipeline) is the
volume lever. Full ledger: docs/FINETUNE.md r16 (laya repo).

ROUND 2 VERDICT (2026-09-26): NO-GO, progress real. Corpus 1191 rows (855
train), ft_mail2 val argmax 0.877 (from 0.821), held-out G1 0.55 acc / 0.80
coverage (base 0.25/0.50). Teacher-agreement ceiling probe on the SAME
held-out pack: 19/20 = 0.95 (paraphrase-consistent) — the 0.85 gate is
VALID, not a ceiling mis-specification. The remaining gap is two-part:
~25% of pack rows hedge below the floor (under-confidence) AND acted rows
are only ~0.69 correct vs 0.877 val argmax (pack difficulty / face shift —
the analyst pack uses faces the generator never saw, by construction).
Temperature sharpening is rejected as pseudo-coverage (gr12 trap; argmax is
temp-invariant). Levers: PPO-after-warm for the acc×coverage product (ft_mail3
running) and, if pack acc stays capped, more analyst faces + harder teacher
contrast anchors on the confusion pairs (sales↔promo, recruiter↔needs_reply).
Pack +4 obfuscation faces added for the G5 rail test (decode-before-screen);
G1 denominator stays the original 20 rows.
