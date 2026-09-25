# LAYA ⇄ Jev Parity Ledger

Spec: `laya/.hermes/plans/2026-09-25-jev-parity-api-spec-v2.md` (source-verified).
Decisions: `…-jev-parity-decisions.md` (D1–D10). This ledger is the single
truth for what is implemented, how it was measured, and which gate applies.
Rule (D9): a row moves to ✅ only with its verification evidence cited.

## Wire surface (M1) — implemented on `feat/jev-parity`

| spec § | use case / surface | implementation | chain | measured | gate | status |
|---|---|---|---|---|---|---|
| §1 envelope | `POST /v1/systemone` ≡ `/v1/laya` | `server/laya_api_http.py` (mounted in warm daemon ASGI) | — | 15 offline tests + live probe | conformance | ✅ |
| §1 `noul` | yes/no probability | `laya_ops.answer_from_noul` ← engine yes/no on chain-0 | base | live 58–62 ms | ≤500 ms | ✅ |
| §1 `choice` | closed-set pick | `laya_ops.answer_from_choice` (invariants by construction) | chain rule | live 15–17 ms; 2000/2000 adversarial envelopes pass consumer validators | Σp±1e-12, argmax±1e-9 | ✅ |
| §1 `score` | rubric → marginal choice | `laya_ops.answer_from_score` (score from reported probs; spread always) | base | live 15.6 ms; band+spread-match proven | ±0.02+1e-12 | ✅ |
| §1 T1.5 batch | many named questions, ONE round; budget 529 | sequential worker passes; deadline checked between questions | mixed | 20-noul batch 278–351 ms (13.9 ms/q) | ≤1.9 s / 121-q extrapolation ~1.7 s | ✅ |
| §1 errors | structured refusals, no internals | `laya_api_sec` vocabulary; 400/401/405/529 mapped | — | refusal probe (state_too_large verbatim body); no traceback ever | — | ✅ |
| §1 auth | Bearer via TOKEN alias, constant-time | `laya_api_sec.check_auth` | — | token set→401 wrong/none, 200 right; token never in body | — | ✅ |
| §1 model echo | verbatim | `naming.model_id` | — | echo `jev-latest`/`laya-r15` live | — | ✅ |
| §1 usage | `requests:1, chars_in/out` honest | router | — | asserted in offline tests | — | ✅ |
| §8 D6 naming | env/config/log aliasing | `server/naming.py` (one resolver) | — | 25 tests | per D6 row | ✅ |
| §2 CLI ops | `laya <cmd>` + `bin/jev` shim | `server/laya_cli.py` | — | 57 tests | stdin/stdout contracts §2 | ✅ |
| §3 Hermes plugin | dual-named tools/commands, honest manifest | `hermes/plugin/hermes-laya/` | — | doctor: 13 tools + 2 hooks registered; fail-open drill 7/7 degraded | seams live in real Hermes (M2) | ✅ |
| §3 OpenCode | native plugin + MCP on daemon | `clients/opencode/laya-decision.ts` (exists; retarget to :11270 routes) | — | MCP `connected` + `laya_guard` round-trip via `run`; native hook driven live (ask/deny/no-op) | D8 | ✅ live |
| §4 config/privacy | layered routing.json, decision log dual-write | naming merge ✅ / dual log writer ✅ | — | 25 tests + live byte-identical `cmp` | — | ✅ |
| §5 evals | latency bars | `bench/laya_latency.py` LIVE run on warm daemon | — | p50/p95 ms: choice-32 **15.8/19.1** · rerank-121 **1624/1867** · skill 4×30 435/481 · score-4 15.2/15.4 · btzsc-72 15.6/16.0 — all echoes ok, **under_bar 5/5** | p50 ≤ published bars (choose .5 / rerank-round 1.9 / route .4 s) | ✅ |
| §7 use-case heads | route/rerank/skill/choose/triage/mail/supervise/plan/ladder/handoff/guard | `server/laya_usecases.py` (+ `laya_remote.py` daemon client) | per-case | 118 usecase tests + live shadow-day sessions (route/skill verified live; all 7 tools fail-open drilled) | M2 shadow day ✅ | ✅ |
| — guardrail (Laya-only) | `laya_guard` pre-exec | engine `laya_guard` mapping (existing) | ft_gr14_ppo | live: `rm -rf / --no-preserve-root` → ask_user 0.947 (bare) / 0.743 (prefixed) → verdict=confirm; `git status` → allow 0.89 | never confidently-allow destructive | ✅ |

## Live M1 acceptance evidence (2026-09-25, warm daemon :11270, real asset)
- probe `probe_live_m1.py`: noul 61.8 ms · guardrail-choice 17.2 ms ·
  score-4level 15.5 ms · noul-batch-20 277.6 ms · choice-32-foreign 15.2 ms
  · refusal structured 400 · /v1/models canonical id — **ALL PASS**, every
  answer re-validated with the consumer's own validators.
- stdio MCP regression `tests/acceptance_mcp.py`: **PASS** (decide/guard/status intact;
  `rm -rf / --no-preserve-root` → confirm; benign allow 0.89; error path
  fails open with fallback text).
- offline suite: **167 passed** (ops 19 + sec 38 + naming 25 + latency 57
  + engine-question 13 + router 15), order-independent, hermetic.

## Deviations (documented, spec-v2 §1 interop kept)
1. `confidence` always reported on `score` answers (their client defaults
   1.0 when absent; an inflated default hides the spread signal).
2. `spread_reported` always true (we always emit the real distribution).
3. Refusals use descriptive error codes inside
   `{status:"invalid_request", error}` — their client maps any of these to
   `malformed` and fails open (the intended degraded path).
4. Mass residual is spread across keys (never a single >1e-9 jump that
   could move argmax) — stronger guarantee than the brief, proven against
   consumer validators 2000/2000.

## Live M2 evidence (2026-09-25, isolated shadow profile + warm daemon)

- **Packaging defect found & fixed (was a total blocker):** real Hermes
  could not load the plugin (`No module named 'naming'` — plugins install
  copytrees ONLY the plugin dir; loader never adds repo `server/` to the
  path). Fix: vendored 7-module closure in `lib/` per plugin
  (`scripts/sync_plugin_lib.py`; closure recomputed by an ast test; CI
  `--check` gate in housekeeping.yml). `hermes plugins doctor` now:
  hermes-laya **13 tools, 2 hooks, registration passed**;
  hermes-laya-handoff **2 hooks, registration passed**.
- **RemoteEngine** (`server/laya_remote.py`): the plugin now reaches the
  ONE Core AI session over `POST /v1/laya` (D8) instead of trying to
  mount a second in-process Engine; duck-shapes the wire answer back to
  the engine result contract; `LAYA_ENGINE=local` opt-out. Daemon death =
  RuntimeError = exactly what the usecases' fail-open rails catch.
- **Shadow-day sessions** (HERMES_HOME=/tmp/laya-shadow, routing=shadow):
  plugin loads, middleware fires once per fresh turn (follow-up
  tool-loop requests reuse the decision — 3× `route_effective`
  `applied:false` logged at the boundary), decisions chain: score→choice
  →noul on the real asset (audit-daemon lines, 24 ms mean), dual log
  `<profile>/logs/laya-decisions.jsonl` ↔ `jev-decisions.jsonl`
  **byte-identical in every session** (`cmp` clean, incl. `route`,
  `route_effective`, `skill` kinds).
- **Skill advisory live-correct:** slate {pdf, docx} →
  "…quarterly report as a Word document…" → `pdf` 0.87 (correct stage-1
  miss + stage-2 confirm per design), dual-log kinds `skill`,
  `stage2:true`; negative prompt answered needs_skill 0.01 → null.
- **Route rails live-correct:** payments-refactor prompt →
  "floor rail → medium" (stakes rail); arithmetic → "confident middle,
  no rail fired"; capital → "unsure, no hard signal". `routed` never
  moved a model without an authorising rail (pool-walk tier-then-up
  honored: empty medium/hard pools → keep + reason).
- **Fail-open drill (daemon dead, port :9):** all 7 canonical tools
  answered degraded in 0.05 s, ZERO raises into the harness, ZERO blocked
  turns; guard fail-SAFE (`verdict: confirm`, never allow), choose
  fail-safe (`reobserve`). Alias handlers return byte-identical answers
  (same handler object, D6).
- **Canary:** `rm -rf /` → `ask_user` 0.743 (bare), `-z rm -rf /` probe →
  `ask_user` 0.741, `--no-preserve-root` variant 0.947 — always
  confirm/deny-class, never allow; benign → allow 0.89 (M1 evidence).
- **`/laya` ≡ `/jev`:** one controller, one state file — flip via one
  name, visible via the other (drill + unit); `route` log written once
  per decision under one file pair.
- **D8 live:** MCP surface `laya_ask`/`laya_guard`/`laya_status` on the
  same daemon answered real questions over `tools/call` (noul 0.886,
  guard confirm, status ok) — same Engine as the HTTP path.
- **D8 OpenCode smoke closed (v1.18.4 / v2 schema):** MCP — `laya` entry
  under `mcp.servers` (moved from a dead `mcp`-as-map shape; v2.0.16
  silently skipped it with a "malformed recognized value" normalization
  log), `opencode mcp list` ✓ connected, and a real `run` round-trip:
  the agent called `laya_guard('rm -rf /')` and reported verdict
  `confirm` from the bridge's own answer. Native plugin — loaded via
  `plugins/laya/` auto-discovery + `package.json` (the documented
  `plugin: ["file://…"]` top-level entry is inert on this build;
  INTEGRATION.md corrected); headless `run` auto-approves bash so
  `permission.ask` never fires there — the deployed hook was instead
  driven directly against the live daemon: trap command →
  `allow→ask_user` (score 0.932), benign → `allow` kept (0.006),
  enforce+`block` → `deny`, bridge down → no-op.
- Engine init-barrier + fail-fast worker fixes (commit 1261bda);
  full suite **360 passed** incl. 11 packaging/RemoteEngine tests.

## M3 soak evidence (2026-09-25, routing `on`, real swap)
- Profile `/tmp/laya-shadow` (hermes-laya + handoff, routing `on`) with a
  REAL second model in `simple.pool` (`mlx-community/Qwen2.5-0.5B-Instruct-4bit`,
  pulled into mlx-serve, same provider `custom`): trivial turn `hi` →
  `route: simple rail → simple` + `route_effective.applied=true`,
  `effective_request_model` swapped; real-math turn → keep (unsure/no
  hard signal), answer correct on the big model. Dual decision log
  byte-identical throughout.
- Handoff capsule LIVE: trigger predicate, lane resolved from the REAL
  session store, capsule built from the real `hermes sessions export`
  (0600, RECOVERY header), one-shot delivery, self-authored guard, audit
  file kept.
- **Bug fixed (found by the soak):** the plugin's `_routing_config()`
  walked ONLY the XDG chain while the CLI walked `<hermes>/laya|jev/` —
  two chains for one file (D6 violation); a profile-scoped routing.json
  was invisible to the plugin (silent `no routing config`). Unified in
  `naming.routing_config_chain()` (hermes_root layer, then XDG; laya>jev;
  env override keeps its no-fallthrough semantics); 4 chain tests; CLI +
  plugin now share it. INTEGRATION.md gained the native-plugin section
  documenting the profile-home config layout.
- Stdio MCP `tools/call laya_guard` probe closed: verdict `confirm`,
  chain ft_gr14_ppo, 24 ms.

## Next
P3 head gates = the model-repo gate table (`laya/docs/GO_GATE.md`, r15:
4 GO + 1 GO-as-gate, already met); the rerank-noul calibration backlog
item (#S=P3) is a training-side task, out of this repo's scope. Repo-side
parity work is complete.
