# Learnings & TODOS — Guardrail Benchmark (terminal-bench matrix, 2026-09-26)

Scope honesty: this test exercised **one use case only — guardrail
(`laya_guard` pre-exec)**. No other plugin use case was measured externally.

## Learnings

1. **Gate cost is negligible**: 21–42 ms/call, 7.5–10.6 s total across a
   full 12-task block (<1 % of trial wall). Latency is never the objection.
2. **Hard-gate mode currently costs task accuracy** (75→17 / 75→33 / 8→0 %):
   the guardrail head under-confidences **terse shell one-liners**
   (`echo 'Hello, world!' > hello.txt` → confirm@0.59; `ls -la f.sh` →
   deny@0.41). Median confidence of withheld commands 0.63–0.67 — mostly
   uncertainty, not detected danger.
3. **Root cause is the state contract, not the weights**: the head was
   trained on natural-language state descriptions ("About to run: …").
   Terse command strings are out-of-distribution. (Same class as the
   "0.667 teacher-ceiling artifact" — the representation binds, not the
   model.)
4. **Operator-less harnesses need a confirm protocol**: in the Hermes rig a
   `confirm` resolves via the human; in terminal-bench every confirm is a
   dead end the agent loops on. 15/16 task flips trace to this.
5. **Fail-safe held perfectly**: 808 gate verdicts, 0 `fail_open` events;
   a dead daemon never greenlights.
6. **The plugin's demonstrated merit is compliance transfer on weaker
   models** (rig: q36 base violated red/gray; guided arm 5/5 + 2/2).
   On an already-compliant teacher (q38) the arms are correctness-tied.
7. **Fairness/harness engineering**: both arms must share identical LLM
   wrappers (same agent class, `LAYA_GUARD_DISABLE=1`); terminus-2 sends no
   `max_tokens` → inject a cap identically or small models loop forever.
8. **Environment traps (fixed, in memory)**: macOS app firewall stealth-
   blocks container→192.168.215.1; containers use `host.docker.internal`,
   host-side harness uses loopback. Bench server :11235 isolates residency
   from the chat server :11234.
9. **We have 808 real guard verdicts with labels** — a ready seed corpus for
   the terse-shell fix (~150 hard-deny examples are mostly mislabeled by
   calibration, not by the head's danger sense).

## TODOS

- [ ] T1 (head): retrain guardrail head with terse-shell states added to
      the corpus (seed: `guard-q3*.jsonl` relabeled + blind-verified;
      contrast anchors per safety recipes). Gate: unchanged on frozen 239 +
      both oracle suites, and terse-shell suite ≥ 90 % allow on benign.
- [ ] T2 (plugin rail): grace mode for operator-less harnesses — N
      consecutive confirms then allow-with-note (bounded friction),
      configurable per client (terminal-bench vs Hermes).
- [ ] T3 (plugin rail): read-only allowlist at the rail level (`cat|ls|
      head|find|echo > newfile`) so only mutating commands hit the head.
- [ ] T4 (state contract): prefix guardrail state with harness context
      (cwd, task goal) — cheap, mirrors the r15 ceiling lesson.
- [ ] T5 (coverage): only the GUARDRAIL use case is benchmarked. All 12
      use-case primitives need accuracy gates + benchmark coverage —
      full audit + FT plan: `docs/USECASE-COVERAGE.md` (this repo).
      [in progress: eval harness `bench/eval_usecases.py` + triage/mail
      held-out packs + pre-registered ft_mail round spec are committed;
      baseline wire run must execute at zero GPU load; FT rounds queued]
- [x] T6 (bench models): q05 removed from mlx-serve; gemma-4-26b-a4b-it
      added → rig retest DONE 2026-09-26 (48/48 trials, YaRN×2 on :11237;
      table in REPORT.md §1). Headline: base/gem repeats q36's red failure
      (executed `curl|bash`); ONLY the guided arm is fully compliant —
      zero tool engagement is model-independent (0/12 on gemma laya arm,
      same as q38/q36). Engine latency 124.5 ms/call under ANE contention
      (2.4× the q38 figure).
- [ ] T7 (scorecard): one consolidated model+plugin scorecard across ALL
      use cases (accuracy, latency, merit, cost) — gate for the paper.
- [ ] T8 (paper): once T1–T7 land, `research-paper-writing` skill turns the
      scorecard + A/B evidence into the write-up.

Raw data: `guard-q{36,38,05}.jsonl`, `tb_metrics.json`,
`speed_tokens.json`; narrative: `REPORT.md`.
