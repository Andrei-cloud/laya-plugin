# Laya Model + Plugin — Consolidated Scorecard

Single table for the whole epic: every use case, its head, accuracy on a
held-out oracle, wire latency, deployment merit (rig), and cost. Rows are
filled ONLY from committed artifacts (each row cites its source). Floor for
"acted" = 0.7 unless noted. Updated: 2026-09-26.

## Headline (per use case)

| # | use case | head (chain) | accuracy (oracle, n) | coverage | wire p50/p95 ms | rig merit | status |
|---|----------|--------------|----------------------|----------|-----------------|-----------|--------|
| 1 | guardrail `guard_command` | ft_gr14_ppo (2) | 0.315 exact-disposition (adversarial 1+2, n=184) — red-line at floor **1/91** | — | 24/26 | 5/5 red + 2/2 gray hold (q38 guided); restores compliance on q36/gemma base failures | ✅ shipped |
| 2 | lang `lang_route` | ft_lang (1) | **0.829** (open-massive, n=2606) | — | 26/31 | not benched in rig (no lang scenario) | ✅ shipped |
| 3 | skill `skill_select` | ft_skill4_ppo (4) | 0.416 wire (synth_skill_v2 gold+none, n=161); head offline 0.615@100% cov (GO_GATE r15) | ack-gate + stage-2 rails | 35/172 | routing task; rig tool-route scenario passes | ✅ shipped (wire gap = rails, not head — see note) |
| 4 | triage `triage_message` | ft_triage2 (champion) | 0.375 wire → **0.500 head-level** (authored pack, n=40) | — | — | — | ⚠️ FT in progress (r1 0.450 → r2 0.500; gate ≥0.85; round 3 = product-rubric relabel + volume) |
| 5 | mail_sort `mail_sort` | ft_mail8 (champion) | 0.70 acc / 0.95 cov (analyst pack n=20; teacher ceiling 0.95; trajectory .45→.55→.65→.60→.50→.60→.70→.65→.60) | 0.95 | pending G5 | pending | 🔁 loop closed at champion (r17 register law x3); pack expansion vs ship = user decision |
| 6 | supervise_run | — | — | — | — | — | 🔜 FT planned (USECASE-RESEARCH) |
| 7 | rerank_noul | — | — | — | — | — | 🔜 FT planned |
| 8 | compact_context | — | — | — | — | — | 🔜 FT planned |
| 9 | choose_action | — | — | — | — | — | 🔜 FT planned |
| 10 | ladder_plan | — | — | — | — | — | 🔜 FT planned |
| 11 | plan_request | none by design | n/a (rails + TEXT_MODEL) | n/a | n/a | n/a | rails-only; e2e bench planned |
| 12 | ack_gate (local) | none by design | local rule, no engine call | n/a | 0 | free tier proven (r15) | ✅ by-design |

## Plugin-level merit (rig, correctness-gated, 4 arms × 12 scenarios)

| model | base | guided (engine notes) | warm | laya (tool+engine) |
|-------|------|----------------------|------|--------------------|
| q38 | 5/5 red, 2/2 gray | 5/5, 2/2 — engine engaged 7/7, 40 ms/call | 5/5, 2/2, fastest (13.3 s med) | 5/5, 2/2 |
| q36 | **4/5, 0/2** | **5/5, 2/2 (restores compliance)** | — | — |
| gemma-4-26b | **4/5, 2/2** | **5/5, 2/2*** | 5/5, 1/2 | **4/5, 1/2 — zero engagement 0/12** |

Verdict: merit is real but ONLY where engagement is guaranteed (guided arm);
tool-exposed engagement is model-dependent and zero on all three models'
laya arms → plugin TODO T2/T3 (grace mode, read-only allowlist) + prompt
steering experiment. External bench (terminal-bench): hard-withhold gate
costs task completion (75%→17–33%) → T1 (terse-shell corpus retrain) + T3.

## Model-level cost (engine)

- Wire latency: p50 16–158 ms by head (zero-load baseline, usecase_eval.json).
- Combined asset: one Core AI .aimodel, 5 chains, one encoder (f16).
- Rig overhead: gate wall cost 7.5–10.6 s per 12-task block (21–37 ms/call),
  zero fail-open events across 72 external trials.

## Evidence index

- Wire baselines: `bench/reports/external-20260926/usecase_eval_baseline.json`
- Rig: `bench/reports/external-20260926/REPORT.md` (+ guard verdict JSONLs)
- Mail rounds ledger: `laya/docs/FINETUNE.md` r16; gates: `docs/USECASE-COVERAGE.md`
- Teacher ceilings: `laya/eval/results/mail_teacher_ceiling.json` (0.95), `teacher_ceiling_tool.json` (0.667 — superseded by r15 state-contract lesson)
