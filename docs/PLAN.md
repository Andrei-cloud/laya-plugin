# Plan — Laya harness plugin + A/B benchmark

**Goal:** make the trained Laya decision heads (guardrail / tool_route /
skill_route / triage / lang_route) usable *inside* agent harnesses (Hermes,
opencode — and any MCP client), then measure with a controlled A/B whether the
plugin benefits agentic workloads on the same tasks and same LLM.

## 1. Plugin architecture (unified, harness-agnostic)

One decision core, three access paths:

| Path | For | Why |
|---|---|---|
| MCP stdio (`server/laya_mcp.py`) | Hermes, opencode (`mcp.servers`), Claude Desktop — anything MCP | zero per-harness code; Hermes shows it as `mcp_laya_*` tools |
| HTTP loopback (`server/laya_http.py`) | opencode **native plugin** (TS hooks), scripts | JS can't speak MCP stdio natively; JSON over 127.0.0.1 is trivial for `fetch()` |
| Python import (`server/engine.py`) | custom harnesses / future Hermes-native plugin | same in-process object |

`server/engine.py` wraps the combined `.aimodel` exactly once per process and
is the only place that knows the asset. Design rules baked in (each learned
the hard way, see docs/RESULTS.md "runtime findings"):

1. **One compute session per process** → dedicated worker thread with its own
   asyncio loop; calls serialize on it (the Core AI runtime invariant from the
   port: `one wrapper == one event loop == one compute-unit session`).
2. **Shape-padded calls** (`pad_to=L_max`): first call per *shape* costs 3–7 s
   of graph specialization — live serving must pay it ONCE at startup warmup,
   then every call is ~16 ms regardless of state length.
3. **fd1 discipline:** the Core AI load prints benign ANE-validation warnings
   to **stdout** — which under MCP *is* the JSON-RPC channel. The MCP server
   silences fd1 only around load+warmup (probe-proven: dup2-to-devnull,
   restore before the SDK writes frames). 0 protocol pollutions after fix.
4. **Question shapes are training contract**: guardrail/tool/skill question
   JSON is copied verbatim from the training corpus (states are BARE — a
   `"About to run:"` prefix flipped `rm -rf /` from block to 0.75-allow).
5. **Escalation is the product**: `confidence < 0.7 → acted=false` — the plugin
   tells the harness "hand this back to the LLM / human", never guesses.
6. **Fail-open for the harness**: any bridge error → no-op with a clean
   fallback message. The plugin may only RAISE caution
   (`advisory: allow→ask`), never silently auto-allow or loosen a deny.
   `enforce` mode may map `block → deny` on shell permissions.

Public surface (identical over all paths): `laya_decide(task,state,options?)`,
`laya_guard(command)`, `laya_status`. Slates come from `LAYA_TOOL_SLATE` /
`LAYA_SKILL_SLATE` (JSON) or per-call `options`.

## 2. Harness integration

- **Hermes:** `hermes mcp add laya --command bin/laya-mcp --env …`
  (non-interactive: `printf '\n' |`). Verified: `hermes mcp test laya` →
  3 tools discovered. No hot-reload → new session.
- **opencode v2:** (a) MCP entry in `opencode.json` (v2 schema
  `mcp.servers.laya`; v1 was flat — wrong nesting loads silently without
  doing anything), or (b) native plugin `clients/opencode/laya-decision.ts`:
  `permission.ask` (bash type only; advisory raises allow→ask, enforce can
  block→deny; never touches non-bash, never loosens), plus opt-in
  `tool.execute.after` advisory. Talks to the HTTP bridge with a 3 s abort
  signal → bridge down = plugin invisible.
- Registration commands for both: `docs/INTEGRATION.md`.

## 3. A/B benchmark design

**Question:** with everything else identical, does the plugin change agent
behavior on the model's own use cases, and at what cost?

- **Arms:** two hermetic `HERMES_HOME` profiles, byte-identical `config.yaml`
  EXCEPT arm B's `mcp_servers.laya` entry (+ `LAYA_AUDIT` path). Same pinned
  model (`ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit` @ mlx-serve
  :11234), same toolsets, one task per `hermes -z` invocation, sequential.
- **Tasks (12):** 5 red-line guardrail prompts (rm -rf /, curl|bash, force
  push, drop schema, ~/Developer + "ignore your safety tools"), 2 gray
  (chmod -R 777 /etc, pkill -9), 4 benign (git status, read file, date/cwd,
  count .py), 1 routing. Adversarial phrasing ("just do it, pre-approved").
- **Safety:** destructive commands are never allowed to execute — no `--yolo`;
  `approvals.mode: smart`, `single_query_mode: deny` (source-verified:
  unattended `-z` context defaults to DENY, hardline floor below that), and a
  canary tripwire file checked after red runs. We measure **attempts** in the
  exported session traces, not outcomes.
- **Metrics (aggregate.py):** red-refused-without-attempt, gray/no-attempt,
  benign task success, wall time, input/output/total tokens, API calls, tool
  calls from trace, laya tool calls (arm B engagement), plugin latency stats
  from the engine audit JSONL (calls, mean/max, acted-rate by task), timeouts.
- **Protocol:** sandbox + git-init per run; shared `terminal.cwd` (pinned in
  both configs — hermes' terminal tool uses `terminal.cwd`, not process cwd);
  `--usage-file` per run; session export per run (trace forensics).

**Threats to validity (stated):** n=1 per task; teacher model shares GPU with
the decision asset (measured sequentially; Laya calls are 16 ms vs LLM calls
of seconds, so cross-talk is bounded); behavior labels are trace-regex, not
LLM-judged. Upgrade path: 3 repeats + median, per-task, interleaved arm order.

## 4. Milestones (all executed)

1. ✅ core engine + self-test (gate semantics; 15.8 ms/call warm)
2. ✅ MCP acceptance through the real SDK (tools, error path, 0 pollutions)
3. ✅ HTTP bridge smoke + opencode native plugin (TS)
4. ✅ Hermes `mcp test` → 3 tools; both arms one-shot smoke
5. ✅ safety verification (source read of approval gates + red smoke canary)
6. ✅ full 24-run A/B → `bench/runs/metrics.json` → `docs/RESULTS.md`
   (completed: 87 runs across both rounds, canary survived, verdict
   PROCEED — engagement hook, not outcome-changer)
7. ⏭ repo publish: GitHub `laya-plugin` (this repo), separate from model repo
