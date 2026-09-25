# bench/ — benchmark working material

Outputs here are **never committed** (gitignored): audit logs, run dumps,
exported session DBs. The harness code that *produces* them lives at the
repo root (`scripts/`, `tests/acceptance_*.py`) and **is** part of the
solution — this directory holds only generated data.

Producers:
- `com.laya.decisiond` writes `LAYA_AUDIT` (set in the LaunchAgent plist)
  per decision call.
- `tests/acceptance_*.py` and the warm-daemon runs generate `runs/` dumps
  and `runs_warm.log` (daemon stdout).
- `laya_latency.py` (gitignored probe) reads the audit log / exported DBs
  and prints the before/after latency report.
