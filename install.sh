#!/bin/sh
# Laya decision plugin — one-click install (macOS 26.5+, Apple Silicon).
#
#   curl -fsSL https://raw.githubusercontent.com/Andrei-cloud/laya-plugin/master/install.sh | sh
#   # or from a clone:  sh install.sh
#
# What it does:
#   1. clones/pulls the MODEL repo (AndyInQtr/laya-decision-plugin) into
#      ~/.laya/model   — asset + configs + tokenizer, nothing else
#   2. creates ~/.laya/venv (coreai-core, transformers, numpy, mcp)
#   3. prints the exact registration line for your agent (Hermes/opencode)
# It never installs anything into your agent automatically.
#
# Env overrides: LAYA_MODEL_DIR (default ~/.laya/model), LAYA_VENV
# (default ~/.laya/venv), LAYA_MODEL_REPO (HF repo id), PYTHON (default
# python3.13, falls back to python3).
set -eu
MODEL_DIR="${LAYA_MODEL_DIR:-$HOME/.laya/model}"
VENV="${LAYA_VENV:-$HOME/.laya/venv}"
REPO="${LAYA_MODEL_REPO:-AndyInQtr/laya-decision-plugin}"
REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ -f "$REPO_DIR/server/engine.py" ] || REPO_DIR=$(pwd)

[ "$(uname -s)" = Darwin ] || { echo "Laya is macOS-only (Core AI / Apple Silicon)" >&2; exit 1; }
command -v hf >/dev/null 2>&1 || { echo "need the huggingface CLI: pipx install huggingface_hub" >&2; exit 1; }

echo "== [1/3] model -> $MODEL_DIR"
if [ -d "$MODEL_DIR/.git" ] || [ -f "$MODEL_DIR/sha256manifest.json" ]; then
  echo "already present; re-run with LAYA_MODEL_DIR set elsewhere to refresh"
else
  mkdir -p "$MODEL_DIR"
  hf download "$REPO" --local-dir "$MODEL_DIR"
fi
[ -e "$MODEL_DIR/laya-combined-f16.aimodel" ] || { echo "model download incomplete" >&2; exit 1; }

echo "== [2/3] venv -> $VENV"
PY="${PYTHON:-}"
[ -n "$PY" ] || PY=$(command -v python3.13 || command -v python3)
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip >/dev/null
"$VENV/bin/pip" install -q coreai-core transformers numpy "mcp>=1.2,<2"

SLATE='{"terminal":"run a shell command, build, install, git, process control","read_file":"open an existing file and show its contents","write_file":"create or overwrite a file","patch":"targeted find-and-replace edit","search_files":"search file contents or find files by name","browser_navigate":"open a URL in the browser","browser_click":"click an element on the page","browser_type":"type into a page field","web_search":"search the web","web_extract":"fetch a page as text","delegate_task":"spawn a subagent","execute_code":"run python","vision_analyze":"inspect an image","text_to_speech":"synthesize speech","cronjob":"schedule a job","memory":"persist a fact","skills_list":"list skills","skill_view":"load a skill","todo":"track a task list","clarify":"ask the user a question"}'

echo "== [3/3] register with your agent (copy-paste, nothing auto-installed)"
cat <<EOF

# --- Hermes (registers the warm daemon; starts it if you ran it) ---
LAYA_ASSETS=$MODEL_DIR LAYA_SOURCE=$MODEL_DIR/configs LAYA_UNIT=gpu \\
  nohup $VENV/bin/python -m server.laya_http_mcp --port 11270 >/tmp/laya-daemon.log 2>&1 &
# (run from $REPO_DIR; wait: curl -s http://127.0.0.1:11270/health -> "warm": true)
printf '\n' | hermes mcp add laya --url http://127.0.0.1:11270/mcp
hermes mcp test laya          # -> Tools discovered: 3

# --- Hermes native plugin (hooks: routing, guard, skills) ---
cp -R $REPO_DIR/hermes/plugin/hermes-laya ~/.hermes/plugins/
cp -R $REPO_DIR/hermes/plugin/hermes-laya-handoff ~/.hermes/plugins/ 2>/dev/null || true

# --- opencode v2 (MCP, stdio; no daemon needed) ---
# merge into ~/.config/opencode/opencode.json:
# { "mcp": { "servers": { "laya": { "type": "local",
#     "command": ["$REPO_DIR/bin/laya-mcp"],
#     "environment": { "LAYA_PYTHON": "$VENV/bin/python",
#                      "LAYA_ASSETS": "$MODEL_DIR",
#                      "LAYA_SOURCE": "$MODEL_DIR/configs",
#                      "LAYA_UNIT": "gpu",
#                      "LAYA_TOOL_SLATE": '$SLATE' },
#     "timeout": 180000, "enabled": true } } } }

# --- self-test ---
LAYA_ASSETS=$MODEL_DIR LAYA_SOURCE=$MODEL_DIR/configs $VENV/bin/python tests/acceptance_core.py
EOF
