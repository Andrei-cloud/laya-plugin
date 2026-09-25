# Integration — register the Laya decision plugin

Two assets are involved: this **plugin** repo (code) and the **model** repo
[`AndyInQtr/laya-decision-plugin`](https://huggingface.co/AndyInQtr/laya-decision-plugin)
(ships `laya-combined-f16.aimodel/`, `configs/`, `tokenizer/`, per-head temps).
Fetch the asset first:

```bash
hf download AndyInQtr/laya-decision-plugin --local-dir ~/laya-decision
export LAYA_ASSETS=~/laya-decision            # dir with laya-combined-f16.aimodel/
export LAYA_SOURCE=~/laya-decision/configs    # dir with tokenizer/ under it… (see note)
```
`LAYA_SOURCE` must be a *source* dir holding `rl_agent_config.json` +
`tokenizer/`, and head temps are read from `<dirname(source)>/<head>/rl_agent_config.json`.
The model repo keeps them flat under `configs/<head>.rl_agent_config.json`; for a
torch-free plugin the shipped `server/engine.py` falls back to `configs/` names.
Quick local layout that satisfies both (used by this repo's bench):
```
release/                      # the HF model repo download, or laya repo's release/
  laya-combined-f16.aimodel/
  configs/ft_gr14_ppo.rl_agent_config.json …
models/source/                # laya repo's source checkpoint (config + tokenizer)
  rl_agent_config.json  tokenizer/
```

## Python env
```bash
python3.13 -m venv .venv && .venv/bin/pip install coreai-core transformers numpy "mcp>=1.2,<2"
```

## Hermes (MCP) — recommended
```bash
printf '\n' | hermes mcp add laya \
  --command "$PWD/bin/laya-mcp" --connect-timeout 180 \
  --env LAYA_PYTHON=/ABS/venv/bin/python \
  --env LAYA_ASSETS=$LAYA_ASSETS --env LAYA_SOURCE=$LAYA_SOURCE \
  --env LAYA_UNIT=gpu \
  --env LAYA_TOOL_SLATE='{"terminal":"run a shell command, build, install, git, process control","read_file":"open an existing file and show its contents", …}'
hermes mcp test laya      # → Tools discovered: 3
```
- `printf '\n'` answers the interactive "enable all tools?" prompt (no TTY →
  `mcp add` otherwise saves nothing). `--args`/`--env` are Hermes' flags.
- Tool names: `mcp_laya_laya_decide`, `mcp_laya_laya_guard`,
  `mcp_laya_laya_status`. **No hot-reload — start a NEW session.**
- Stdio subprocesses get only a baseline env; pass what the server needs via
  `--env` (done above).

## Hermes native plugin (hermes-laya) — routing, skills, guardrail tools
Copy `hermes/plugin/hermes-laya/` (+ `hermes-laya-handoff/`) into
`<HERMES_HOME>/plugins/` (per-profile) or `~/.hermes/plugins/` (shared).
The vendored `lib/` is self-contained — nothing outside the plugin dir is
imported. The plugin talks to the warm daemon (`RemoteEngine`,
`LAYA_DECISIOND_URL`, default `http://127.0.0.1:11270`); `LAYA_ENGINE=local`
forces the in-process Engine only on boxes that carry the assets.

State and config live in the **profile home** (`$HERMES_HOME`, default
`~/.hermes`) — NOT under XDG:
| File | Purpose |
|---|---|
| `laya/state.json` | switches: `routing` off\|shadow\|on, `skills`, `notice`, `handoff`, `merge_requests` (profile layer beats the shared `~/.hermes/laya/state.json`; `/laya routing on` writes it) |
| `laya/routing.json` | `default_model` + `tiers.{simple,medium,hard}.pool` (first entry = swap target; model ids are `provider:model` and a swap only applies WITHIN the current provider) |
| `laya/ladder.json` | shared ladder state (the `jev/` path is a symlink) |

Legacy XDG locations (`$XDG_CONFIG_HOME/laya|jev/routing.json`) still load
as fallback; within each base `laya/` wins over `jev/` (D6). The ONE chain
is `naming.routing_config_chain()` — CLI and plugin cannot drift.
Verified live (soak): `routing: on` + a real second model in
`simple.pool` → trivial turn swaps (`route_effective.applied=true`),
real work keeps the default, dual decision log byte-identical.

## opencode v2 — option A: MCP (identical server)
`~/.config/opencode/opencode.json` (v2 nests under `mcp.servers`; v1.18 was
flat `mcp.{name}` — wrong nesting loads silently and does nothing):
```jsonc
{
  "mcp": {
    "servers": {
      "laya": {
        "type": "local",
        "command": ["/ABS/laya-plugin/bin/laya-mcp"],
        "environment": {
          "LAYA_PYTHON": "/ABS/venv/bin/python",
          "LAYA_ASSETS": "/ABS/release",
          "LAYA_SOURCE": "/ABS/laya/models/source",
          "LAYA_UNIT": "gpu",
          "LAYA_TOOL_SLATE": "{…your 20-tool slate…}",
          "LAYA_SKILL_SLATE": "{…your skill slate…}"
        },
        "timeout": 180000,
        "enabled": true
      }
    }
  }
}
```
`opencode mcp list` → `laya: ✓ connected`. (timeout is **ms** in opencode,
**seconds** in Hermes.)

## opencode v2 — option B: native plugin (in-process hooks)
Needs the warm daemon running (start once per machine; it serves MCP and
the question API from one Engine):
```bash
LAYA_ASSETS=… LAYA_SOURCE=… python -m server.laya_http_mcp --port 11270 &   # ~6 s load+warmup
mkdir -p ~/.config/opencode/plugins/laya
cp clients/opencode/laya-decision.ts ~/.config/opencode/plugins/laya/index.ts
```
plus a minimal `~/.config/opencode/plugins/laya/package.json`
(`{"name":"laya-decision-opencode","type":"module","exports":{".":"./index.ts"}}`).
v2.0.16 loads plugins by **directory auto-discovery** under
`~/.config/opencode/plugins/` (verified in the serve log: `loading plugin
id=…/plugins/laya`); a bare `index.ts` without package.json is NOT loaded,
and the `"plugin": ["file://…"]` manifest entry was ignored by that build —
keep the package.json, it is the contract that works. Options live at the
top level of `opencode.json`:
```jsonc
{ "laya": { "mode": "advisory", "bridge": "http://127.0.0.1:11270" } }
```
Behaviour: `permission.ask` for `type:"bash"` only — advisory raises
`allow → ask` with the model's reason in the terminal; `mode:"enforce"` maps
`block → deny`. It never downgrades a deny, never touches non-bash
permissions, and no-ops if the bridge is down (3 s abort).
Note: headless `opencode run` auto-approves bash, so the hook is only
consulted in interactive sessions (or with `permission.bash: "ask"`).

## Self-test after wiring
```bash
LAYA_ASSETS=… LAYA_SOURCE=… python tests/acceptance_core.py    # gate semantics, ~16 ms/call
LAYA_ASSETS=… LAYA_SOURCE=… python tests/acceptance_mcp.py     # MCP transport acceptance
```
