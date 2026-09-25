#!/bin/sh
# Install the warm decision daemon as a macOS LaunchAgent from the
# committed template — substitutes this machine's paths so the repository
# never carries absolute user paths. Idempotent: boots out the old job
# first. macOS only (Core AI / Apple Silicon). Usage (env override any
# default; the three ?-marked ones are required):
#   LAYA_PYTHON=… LAYA_ASSETS=… LAYA_SOURCE=… [LAYA_PORT=11270] \
#     sh scripts/install_daemon.sh
set -eu
REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PY="${LAYA_PYTHON:?LAYA_PYTHON required (python with coreai-core; see requirements-build.txt)}"
ASSETS="${LAYA_ASSETS:?LAYA_ASSETS required (dir with laya-combined-f16.aimodel/ + combined_provenance.json)}"
SOURCE="${LAYA_SOURCE:?LAYA_SOURCE required (dir with rl_agent_config.json + tokenizer/)}"
PORT="${LAYA_PORT:-11270}"
LOG="${LAYA_LOG:-$HOME/Library/Logs/laya-daemon.log}"
[ "$(uname -s)" = Darwin ] || { echo "Laya is macOS-only (Core AI / Apple Silicon)" >&2; exit 1; }
[ -x "$PY" ] || { echo "not executable: $PY" >&2; exit 1; }
[ -e "$ASSETS/laya-combined-f16.aimodel" ] || { echo "missing $ASSETS/laya-combined-f16.aimodel — run install.sh (model download) first" >&2; exit 1; }

DST="$HOME/Library/LaunchAgents/com.laya.decisiond.plist"
mkdir -p "$(dirname "$DST")" "$(dirname "$LOG")" "$(dirname "$REPO")/bench" 2>/dev/null || true
mkdir -p "$REPO/bench" 2>/dev/null || true
TMP=$(mktemp)
sed -e "s|__LAYA_PYTHON__|$PY|g" \
    -e "s|__LAYA_PLUGIN_REPO__|$REPO|g" \
    -e "s|__LAYA_ASSETS__|$ASSETS|g" \
    -e "s|__LAYA_SOURCE__|$SOURCE|g" \
    -e "s|__LAYA_PORT__|$PORT|g" \
    -e "s|__LAYA_LOG__|$LOG|g" \
    "$REPO/deploy/com.laya.decisiond.plist" > "$TMP"
plutil -lint "$TMP" >/dev/null
launchctl bootout "gui/$(id -u)/com.laya.decisiond" 2>/dev/null || true
mv "$TMP" "$DST"
launchctl bootstrap "gui/$(id -u)" "$DST"
echo "installed: $DST (port $PORT, log $LOG)"
echo "health:    curl -s http://127.0.0.1:$PORT/health"
