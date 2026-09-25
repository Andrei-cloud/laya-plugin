# Housekeeping — Code of Conduct (binding for this project)

Standing user rules, stated 2026-09-22, restated 2026-09-25 as project law:

1. **No `.out` / `.log` / `.err` files ever enter either repository**
   (`laya`, `laya-plugin`). All gitignores carry `*.log *.out *.err`;
   CI fails on any tracked match. Redirected command output (`cmd > x.out`)
   is forbidden in the repo tree — use the scratch dir
   (`~/.hermes/profiles/developer/cache/scratch/`) or pipe through
   `grep`/head filters; the harness already persists full output
   out-of-band, so dumps add nothing.
2. **Always review delegation/dispatch/bench logs and capture every
   possible error** before declaring a task done. "Ran fine" without a log
   read is not evidence. Named cautionary example: Round-1 was reported as
   "zero engagement" while the bench log actually showed 3× `TIMEOUT rc=-9`
   and a daemon `CompilationFailure` — a measurement-blind aggregator
   nearly shipped a wrong conclusion.
3. **Always solve the bugs first before proceeding with the next tasks.**
   A bug surfaced in a log blocks the queue: fix → regression-test →
   commit → only then the next task. Silent workarounds (retry harder,
   longer timeout, ignore the warning) are not fixes. Round-1's daemon
   stall and stdout warning-leak are the named examples: both were fixed
   before Round-2 ran.
4. **Delegation batches get an error sweep on completion** — scan each
   subagent result for `error`/`blocked`/`failed`/anomalies and reconcile
   numbers against source files before accepting the batch.
5. **Repository structure stays neat, logical, and segregated** (user
   standing rule): `src/laya_port` (library) / `scripts` / `eval` / `report`
   in the model repo; `server` / `clients` / `hermes` / `skills` / `tests` /
   `bench` / `docs` in the plugin repo. No scratch/probe files at repo root.

## Mechanization (not trust)
- `.gitignore` in both repos: `*.log`, `*.out`, `*.err` (+ specific names).
- CI `.github/workflows/housekeeping.yml`: tracked `*.log|*.out|*.err` fails
  the build; root-level `probe_*/diag_*/test_warm_*` fails the build. (A
  `test` job comes with plan Phase P2's `tests/` suite; it must carry the
  same two checks so a future reorganization can't silently drop them.)
- Pre-acceptance ritual per task: read the task's logs → list captured
  errors → fix or file each before the next task starts.
