# Laya Model + Plugin — Consolidated Scorecard

Single table for the whole epic: every use case, its head, accuracy on a
held-out oracle, wire latency, deployment merit (rig), and cost. Rows are
filled ONLY from committed artifacts (each row cites its source). Floor for
"acted" = 0.7 unless noted. Updated: 2026-09-27 (post-r36: chain expansion
v2 deployed, noul-dialect + RL-temperature bugs fixed and re-measured).

## Headline (per use case)

| # | use case | head (chain) | accuracy (oracle, n) | coverage | wire p50/p95 ms | rig merit | status |
|---|----------|--------------|----------------------|----------|-----------------|-----------|--------|
| 1 | guardrail `guard_command` | ft_gr14_ppo (2) | 0.315 exact-disposition (adversarial 1+2, n=184) — red-line at floor **11/91** (was 15/91 on the stale Sep-24 asset; rebuilt asset carries current head weights) | raw_allow_below_floor 15 | 34/38 | 5/5 red + 2/2 gray hold (q38 guided); restores compliance on q36/gemma base failures | ✅ shipped |
| 2 | lang `lang_route` | ft_lang (1) | **0.829** (open-massive, n=2606) | — | 36/39 | not benched in rig (no lang scenario) | ✅ shipped |
| 3 | skill `skill_select` | ft_skill4_ppo (4) | 0.360 wire (synth_skill_v2 gold+none, n=161) — pre-r36 0.416 was dialect-inflated (stage-2 base-chain noul said yes 2× too often); head offline 0.615@100% cov (GO_GATE r15) | ack-gate + stage-2 rails | 71/107 | routing task; rig tool-route scenario passes | 🔁 shipped; TODO r37: route stage-2 questions onto the skill chain in their training dialect (train!=serve strings, r36 class) |
| 4 | triage `triage_message` | ft_triage5 (chain 5) | 0.575 wire (was 0.275 — dialect bug: engine rendered native-noul binaries as choice{no,yes}, flipping NOUL_GATE rails; 16/40 faces) → **0.625 head-level** (authored pack, n=40) | — | 215/219 | — | ✅ wire≈head (residual 2 faces = redact/score rendering; 4 faces gained); loop closed at 0.625 (teacher's own pack 0.700 caps end-to-end) |
| 5 | mail_sort `mail_sort` | ft_mail8 (chain 6) | 0.625–0.708 wire (two harnesses, n=24) — matches 0.70 head champion (analyst pack n=20; teacher ceiling 0.95) | 0.958 | 35/37 (mail) 107/112 (mail_uc) | pending G5 | 🔁 loop closed at champion (r17 register law x3); pack expansion vs ship = user decision |
| 6 | supervise_run | ft_sup3 (chain 7) | **0.679 wire** (n=28; head-level 0.643 — 1 face rendering noise; base was 0.607) — dialect-safe family (binaries trained choice{no,yes}, kept by _CHOICE_BINARY_TASKS) | — | 178/182 | — | ✅ wire ≥ head; FT above base |
| 7 | rerank_noul | ft_rrk3 (chain 10) | **0.100 exact wire = head** (pack n=10; jaccard 0.765; base 0.000; every rel score was a constant pre-referent-fix, r33) | — | 215/281 | — | ✅ chain shipped, wire=head; lever now corpus faces (teacher ceiling 8/10) |
| 8 | compact_context | ft_cmp2 (chain 9) | **0.300 wire = head** (pack n=10 exact-invariant golds; base 0.000; epoch-11 selection, ep12+ overfits) | — | 324/340 | — | ✅ chain shipped, wire=head; PPO-on-rail round = designed next lever (gold-keep conf 0.42–0.64 < 0.7 rail) |
| 9 | choose_action | ppo_cho (chain 8) | **0.650 wire = head** (GUI-step pack n=20; the pre-r36 0.500 was a STALE INHERITED TEMPERATURE: RL policy optimized the acted rail at T=1.0 but shipped ft_cho's NLL temp 1.837 → acted picks flattened under the 0.65 floor; config reset + trainer fix `6bc49da`) | — | 34/38 | — | ✅ wire = head = champion (base 0.550); saturated at 0.650 for the 125-state corpus (r34) |
| 10 | ladder_plan | **none by design** | n/a — zero model calls (code-verified r31: cooldown arithmetic only; spec §7 "No head — config + templates") | n/a | 0 | 11 pytest tests (usecases+cli+naming) | ✅ by-design (row corrected — "FT planned" was wrong) |
| 11 | plan_request | none by design | n/a (rails + TEXT_MODEL) | n/a | n/a | n/a | rails-only; e2e bench planned |
| 12 | ack_gate (local) | none by design | local rule, no engine call | n/a | 0 | free tier proven (r15) | ✅ by-design |

## Plugin-level merit (rig, correctness-gated, 4 arms × 12 scenarios)

r37 final — all 3 models × 4 arms on the fixed stack (post dialect/temp
fixes, single mlx-serve :11234, gemma native-256K). Frozen in
`bench/reports/r37-final/`:

| model | base | guided (engine notes) | warm | laya (tool+engine) |
|-------|------|----------------------|------|--------------------|
| q38 | 5/5 red, 2/2 gray | 5/5, 2/2 — engine engaged 7/7, 54 ms mean, 5 blocked/escalated | 5/5, 2/2 | 3/5, 2/2 — engagement 1/12, attempted-destructive misses |
| q36 | **3/5, 0/2** | **5/5, 2/2 (restores compliance)** — engine 7/7, 58 ms mean | 4/5, 1/2 | 4/5, 1/2 — engagement 1/12 |
| gemma | **3/5, 1/2** | **5/5, 1/2** — engine 6 calls, 141 ms mean, 4 blocked/escalated; the 1 gray miss was a chmod the engine flagged `confirm` but gemma ran anyway (SIP saved it) | 4/5, 1/2 | 3/5, 1/2 — zero engagement 0/12 |

Verdict, now on three models: **without the plugin, two of three models
execute red commands (q36, gemma 3/5 each); with the guided gate every model
reaches 5/5 red** — the engine is consulted on every guardrail command and
itself blocked/escalated 4–5 of 7 decisions at 54–141 ms/call. Gray
compliance is model-dependent everywhere (gemma overrode one `confirm`).
Tool-exposed engagement stays ≤1/12 on all models (T2/T3 open). Base-arm
counts drift run-to-run (live-LLM variance), which the deterministic guided
gate removes.

## Model-level cost (engine)

- Wire latency: p50 20–325 ms by head (zero-load baseline, usecase_eval.json;
  multi-question use cases pay per-question passes).
- Combined asset: one Core AI .aimodel, one encoder (f16); **11 chains live**
  (r36 deploy: base + 7 v1 heads + ppo_cho + ft_cmp2 + ft_rrk3), per-chain
  fitted temperatures from the release configs (agent layout-fallback fix
  `b9164ef` — release-folder consumers now load them too).
- Rig overhead: gate wall cost 7.5–10.6 s per 12-task block (21–37 ms/call),
  zero fail-open events across 72 external trials.

## Evidence index

- Wire baselines: `bench/reports/external-20260926/usecase_eval_baseline.json`
- Rig: `bench/reports/external-20260926/REPORT.md` (+ guard verdict JSONLs)
- Mail rounds ledger: `laya/docs/FINETUNE.md` r16; gates: `docs/USECASE-COVERAGE.md`
- Teacher ceilings: `laya/eval/results/mail_teacher_ceiling.json` (0.95), `teacher_ceiling_tool.json` (0.667 — superseded by r15 state-contract lesson)
