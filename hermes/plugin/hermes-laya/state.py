"""Plugin switch state — profile layer over shared layer (D6 dual-named).

State file:  <layer>/laya/state.json   (canonical)
             <layer>/jev/state.json    (alias path — same FILE via the
             resolver below; never two divergent states)

Precedence (spec-v2 §3: "profile > shared"):
  <profile>/laya/state.json  >  <hermes_root>/laya/state.json  >  defaults
Defaults are OFF/OFF/OFF with merge_requests ON (the upstream safe default —
a plugin install changes nothing until someone flips a switch).

`/laya routing on [all]` writes the shared layer; without `all` it writes the
profile layer. The state module is the ONLY writer (DRY; the slash command
and the CLI both call set_switch).
"""
import json
import os
import threading
from pathlib import Path

from naming import env_alias, hermes_root

_lock = threading.Lock()
DEFAULTS = {"routing": "off", "skills": "off", "notice": "off",
            "merge_requests": "on", "handoff": "off"}

OWN_NAME = "laya"           # namespace this plugin writes under
ALIAS_NAME = "jev"          # read as alias for pre-existing setups
_owner_note = None          # set when an upstream co-installed plugin owns a name


def _profile_dir():
    return Path(env_alias("PROFILE_DIR") or os.path.expanduser("~/.hermes"))


def _layer_path(shared: bool):
    base = hermes_root() if shared else _profile_dir()
    # canonical path; if ONLY the alias path exists (pre-existing setup),
    # keep using it so cooldowns/switches never fork (D6 rule).
    canon = base / OWN_NAME / "state.json"
    alias = base / ALIAS_NAME / "state.json"
    return canon if canon.exists() or not alias.exists() else alias


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def setting(key: str, default=None):
    """Profile layer wins over shared layer wins over DEFAULTS."""
    if default is None:
        default = DEFAULTS.get(key, "off")
    with _lock:
        for shared in (False, True):
            v = _read(_layer_path(shared)).get(key)
            if v is not None:
                return v
    return default


def set_switch(key: str, value: str, shared: bool = False) -> str:
    """The ONLY state mutation path (slash command + CLI both call this).
    Returns a human line naming the layer written (auditability)."""
    with _lock:
        path = _layer_path(shared)
        state = _read(path)
        state[key] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    scope = ("the default for EVERY profile (a profile's own setting still "
             "wins)" if shared else "set for this profile")
    return f"Laya {key} = {value}, {scope}."


def state_overview() -> dict:
    """Safe summary for /laya and doctor: effective switches with the layer
    each came from. Never any secret."""
    out = {}
    for key in DEFAULTS:
        layer = "default"
        val = DEFAULTS[key]
        for shared, name in ((False, "profile"), (True, "shared")):
            v = _read(_layer_path(shared)).get(key)
            if v is not None:
                val, layer = v, name
                break
        out[key] = {"value": val, "layer": layer}
    return out


def ownership_note(note: str = None):
    """Remember (and surface) that a name collided with a co-installed
    upstream plugin — never silently shadow it."""
    global _owner_note
    if note is not None:
        _owner_note = note
    return _owner_note
