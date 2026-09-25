"""The ONE env/config alias resolver (decision D6).

Every read of the Laya external surface goes through ``env_alias``:
canonical ``LAYA_<NAME>`` first, then the ``JEV_<NAME>`` alias, then the
explicitly-listed third alias (legacy spellings like ``TYPESAFE_*`` keep
working). When two namespaces are BOTH set and the values DIFFER, the
canonical ``LAYA_*`` wins, the divergence is recorded ONCE per name in the
module-level ``divergences`` list and warned via the ``laya.naming`` logger
— never raised. Empty-string env values count as unset.

Nothing else reads env for the Laya surface (plan §"Naming & aliasing
contract"). Wire names are OUT of scope here: question types
(``noul|choice|score``) and envelope fields stay exactly Jev-compat —
interop requires them literal (spec-v2 §8).

SECURITY RULE binding on this module: secret values (``SECRET_KEYS``) are
NEVER logged, warned, or reported — presence/source/length only
(``describe()`` shape per spec-v2 §1).
"""
import copy
import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger("laya.naming")

# Canonical name -> ordered env spellings, first hit wins (D6 table).
# Names outside this table get the generic passthrough chain
# LAYA_<NAME> -> JEV_<NAME> -> TYPESAFE_<NAME>.
ALIAS_TABLE = {
    "ROUTING_CONFIG":   ["LAYA_ROUTING_CONFIG", "JEV_ROUTING_CONFIG"],
    "LADDER_STATE":     ["LAYA_LADDER_STATE", "JEV_LADDER_STATE"],
    "MEMO":             ["LAYA_MEMO", "JEV_MEMO"],
    "MIN_CONFIDENCE":   ["LAYA_MIN_CONFIDENCE", "JEV_MIN_CONFIDENCE"],
    # TYPESAFE_MODEL is THEIR spelling; it keeps working as an alias (D6).
    "MODEL":            ["LAYA_MODEL", "JEV_MODEL", "TYPESAFE_MODEL"],
    "API_KEY":          ["LAYA_API_KEY", "JEV_API_KEY", "TYPESAFE_API_KEY"],
    "PROBE_ALLOWLIST":  ["LAYA_PROBE_ALLOWLIST", "JEV_PROBE_ALLOWLIST"],
    "PLAN_MODEL":       ["LAYA_PLAN_MODEL", "JEV_PLAN_MODEL"],
    "HANDOFF_JEV":      ["LAYA_HANDOFF_JEV", "JEV_HANDOFF_JEV"],
    # DASHBOARD_TOKEN is the shared unbranded spelling their dashboard reads.
    "TOKEN":            ["LAYA_TOKEN", "JEV_TOKEN", "DASHBOARD_TOKEN"],
}

# Keys whose values are secrets: never logged, never in doctor reports —
# {present, provider, source, length} only (spec-v2 §1 describe()).
SECRET_KEYS = frozenset({"API_KEY", "TOKEN"})

# Echoed when a client sends no model (strict clients compare the echo
# verbatim, so anything non-empty is echoed back untouched).
DEFAULT_MODEL = "laya-r15"

# Module state (tests reset via _reset_state()).
divergences = []            # one record per name that ever diverged
_resolved = {}              # name -> {"source": env, "value": str} (never secrets)
_warned = set()             # names already warned about (warn ONCE)
_perm_warned = set()        # credentials files with loose mode, warned once


def _reset_state():
    """Clear module memoization (tests; also usable by long-lived daemons
    that reconfigure env at runtime)."""
    divergences.clear()
    _resolved.clear()
    _warned.clear()
    _perm_warned.clear()


def _chain(name):
    """Ordered env spellings for a canonical name."""
    if name in ALIAS_TABLE:
        return ALIAS_TABLE[name]
    # generic passthrough: any other LAYA_*/JEV_* pair keeps working
    return [f"LAYA_{name}", f"JEV_{name}", f"TYPESAFE_{name}"]


def _env_set(env_name):
    """Env value with empty-string treated as UNSET (D6 rule)."""
    v = os.environ.get(env_name)
    return v if v != "" and v is not None else None


def resolve(name):
    """First-hit resolution: returns ``(value, source_env)`` or
    ``(default_or_None, None)``. Records divergences; never raises."""
    chain = _chain(name)
    found = {e: v for e in chain if (v := _env_set(e)) is not None}
    if found:
        winner_env = next(e for e in chain if e in found)
        winner = found[winner_env]
        diverged = [e for e, v in found.items() if v != winner]
        if diverged and name not in _warned:
            # values are NEVER included: divergence records must be safe to
            # print even for SECRET_KEYS.
            rec = {"name": name, "source": winner_env, "diverged_sources": diverged}
            divergences.append(rec)
            _warned.add(name)
            log.warning(
                "laya.naming: %s set in %s and %s with different values; "
                "canonical %s wins (divergence recorded once, doctor reports it)",
                name, winner_env, ", ".join(diverged), winner_env)
        _resolved[name] = {"source": winner_env,
                           # secrets: presence only, value never memoized
                           "value": None if name in SECRET_KEYS else winner}
        return winner, winner_env
    return None, None


def env_alias(name, default=None):
    """THE env read for the Laya surface. Canonical LAYA_* wins on
    divergence (warned once); empty strings count as unset."""
    value, _ = resolve(name)
    return default if value is None else value


# --------------------------------------------------------------------------
# model id (echo rule: strict clients compare what we send back)
# --------------------------------------------------------------------------

def model_id(requested):
    """Echo non-empty strings VERBATIM (their client compares the returned
    model against what it sent); canonical default when None/empty is
    env alias MODEL else ``laya-r15``."""
    if isinstance(requested, str) and requested:
        return requested
    return env_alias("MODEL") or DEFAULT_MODEL


# --------------------------------------------------------------------------
# layered config dirs (Laya first, Jev fallback; per-tier merge)
# --------------------------------------------------------------------------

def _xdg_base(kind):
    if kind == "cache":
        env = os.environ.get("XDG_CACHE_HOME")
        return Path(env) if env else Path.home() / ".cache"
    env = os.environ.get("XDG_CONFIG_HOME")
    return Path(env) if env else Path.home() / ".config"


def config_dir_chain(subpath, kind="config"):
    """Ordered candidate FILE paths for ``subpath`` (e.g. ``routing.json``):
    ``<xdg>/laya/`` first, ``<xdg>/jev/`` as fallback (existing Jev configs
    keep working unchanged). ``kind``: "config" (XDG_CONFIG_HOME) or
    "cache" (XDG_CACHE_HOME, e.g. memo/)."""
    base = _xdg_base(kind)
    return [base / ns / subpath for ns in ("laya", "jev")]


def routing_config_chain(subpath="routing.json"):
    """THE routing.json candidate chain — one answer for CLI, plugin and
    anything else that reads it (D6: one resolver, no per-surface chains).
    Order, first existing file wins:
      1. <hermes_root>/laya|jev/  — profile-scoped, Hermes doctrine:
         profiles are independent islands, so the profile's own config
         beats any machine-wide layer (this is where a per-profile soak
         puts it; the plugin once read ONLY the XDG chain and silently
         answered "no routing config" to a profile that had one);
      2. <xdg_config>/laya|jev/   — pre-profile Jev-era location, kept
         working unchanged (D6).
    Within each base, laya wins over jev. NOTE: the
    LAY(A|_ROUTING_CONFIG) env path is NOT part of this chain — it is an
    override with its own semantics (set-but-unreadable must fail open to
    {}, never fall through to a different file; a bad operator pointer
    must not silently swap in some other config). Consumers check it
    first, then walk this chain."""
    root = hermes_root()
    out = [root / ns / subpath for ns in ("laya", "jev")]
    out.extend(_xdg_base("config") / ns / subpath for ns in ("laya", "jev"))
    return out


def _deep_merge_fill(base, fallback):
    """``base`` (Laya, earlier layer) WINS conflicts; ``fallback`` only
    fills missing keys. Dicts recurse; anything else is atomic."""
    for k, v in fallback.items():
        if k not in base:
            base[k] = copy.deepcopy(v)
        elif isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge_fill(base[k], v)
        elif base[k] != v:
            rec = {"name": f"config:{k}", "source": "laya-layer",
                   "diverged_sources": ["jev-layer"]}
            if rec["name"] not in {d["name"] for d in divergences}:
                divergences.append(rec)
                log.warning("laya.naming: config key %s differs between "
                            "laya and jev layers; laya layer wins", k)
    return base


def merge_json_layers(subpath, kind="config"):
    """Deep-merge the layered JSON files found along ``config_dir_chain``:
    Laya layer wins conflicts, the Jev fallback fills missing keys,
    divergences recorded (once per key, values never). Unparseable layer:
    logged and skipped (fail-open, same spirit as the use-case CLIs)."""
    merged = None
    for path in config_dir_chain(subpath, kind=kind):
        try:
            raw = path.read_text()
        except OSError:
            continue
        try:
            layer = json.loads(raw)
        except ValueError:
            log.warning("laya.naming: skipping unparseable config layer %s", path)
            continue
        if not isinstance(layer, dict):
            log.warning("laya.naming: skipping non-object config layer %s", path)
            continue
        # first good layer is the base; later layers only fill (base wins)
        merged = copy.deepcopy(layer) if merged is None else _deep_merge_fill(merged, layer)
    return merged if merged is not None else {}


# --------------------------------------------------------------------------
# ladder state (ONE shared file — cooldowns must never fork under two names)
# --------------------------------------------------------------------------

def hermes_root():
    """<hermes_root> = env HERMES_HOME or ~/.hermes."""
    h = os.environ.get("HERMES_HOME")
    return Path(h) if h else Path.home() / ".hermes"


def ladder_state_path():
    """The single shared ladder state. Resolves env alias LADDER_STATE
    (LAYA_ -> JEV_ order); default ``<hermes_root>/laya/ladder.json``.
    NOTE: ``<hermes_root>/jev/ladder.json`` is a SYMLINK to this one file
    (D6) — ladder cooldowns must never fork under two names."""
    v, _ = resolve("LADDER_STATE")
    if v:
        return Path(v)
    return hermes_root() / "laya" / "ladder.json"


# --------------------------------------------------------------------------
# key resolution: env (3 spellings) -> keystore (2 services) -> files (2 dirs)
# --------------------------------------------------------------------------

# Documented resolution order (spec-v2 §1 Key resolution, Laya-prepended):
#   env LAYA_API_KEY -> env JEV_API_KEY -> env TYPESAFE_API_KEY
#   -> keystore service "Hermes Laya API"      acct LAYA_API_KEY
#   -> keystore service "Hermes TypeSafe API"  acct TYPESAFE_API_KEY
#   -> file ~/.config/laya/credentials (0600)
#   -> file ~/.config/jev/credentials
# The value is NEVER logged or stored anywhere; only lengths/presence reach
# doctor_alias_report().

_KEYSTORE_CHAIN = [("Hermes Laya API", "LAYA_API_KEY"),
                   ("Hermes TypeSafe API", "TYPESAFE_API_KEY")]


def keychain_secret(service, account):
    """macOS keystore read via ``security find-generic-password -w`` with a
    BOUNDED 5 s timeout; None on ANY failure (not macOS, binary missing,
    no item, lock denied, timeout). Used by resolve_api_key()."""
    import shutil
    if os.uname().sysname != "Darwin":
        return None
    exe = shutil.which("security") or "/usr/bin/security"
    try:
        r = subprocess.run([exe, "find-generic-password", "-s", service,
                            "-a", account, "-w"],
                           capture_output=True, text=True, timeout=5)
    except Exception:  # timeout, OSError, anything — fail-open to None
        return None
    if r.returncode != 0:
        return None
    v = (r.stdout or "").strip()
    return v or None


def _read_credentials_file(path):
    """Read one credentials file (expected mode 0600). Accepts bare key,
    ``NAME=value`` lines, or a JSON object; loose mode warns once
    (permission only — never the value)."""
    try:
        if not path.is_file():
            return None
        st = path.stat()
        if st.st_mode & 0o077 and path not in _perm_warned:
            _perm_warned.add(path)
            log.warning("laya.naming: credentials file %s is group/world-"
                        "readable (expected 0600)", path)
        text = path.read_text()
    except OSError:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except ValueError:
            return None
        if isinstance(obj, dict):
            for k in ("LAYA_API_KEY", "JEV_API_KEY", "TYPESAFE_API_KEY", "API_KEY"):
                v = obj.get(k)
                if isinstance(v, str) and v:
                    return v
        return None
    for line in stripped.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            if k.strip() in ("LAYA_API_KEY", "JEV_API_KEY", "TYPESAFE_API_KEY", "API_KEY"):
                return v.strip() or None
            continue
        return line
    return None


def resolve_api_key():
    """Full key resolution chain (see table above this section). First hit
    wins; None when nothing found. Value never logged, never memoized."""
    v, _ = resolve("API_KEY")  # env chain: LAYA_ -> JEV_ -> TYPESAFE_
    if v:
        return v
    for service, account in _KEYSTORE_CHAIN:
        v = keychain_secret(service, account)
        if v:
            return v
    for path in config_dir_chain("credentials"):
        v = _read_credentials_file(path)
        if v:
            return v
    return None


def _key_source():
    """Where resolve_api_key() would find the key, WITHOUT returning it."""
    for env in _chain("API_KEY"):
        if _env_set(env):
            return env
    for service, account in _KEYSTORE_CHAIN:
        if keychain_secret(service, account):
            return f"keystore:{service}"
    for path in config_dir_chain("credentials"):
        if _read_credentials_file(path):
            return str(path)
    return None


def describe_api_key():
    """describe() per spec-v2 §1: {present, provider, source, length} —
    length only, NEVER the key."""
    src = _key_source()
    if src is None:
        return {"present": False, "provider": None, "source": None, "length": 0}
    key = resolve_api_key() or ""
    provider = ("typesafe" if ("TYPESAFE" in src or "TypeSafe" in src)
                else "laya")
    return {"present": True, "provider": provider, "source": src,
            "length": len(key)}


# --------------------------------------------------------------------------
# doctor alias report (safe to print; secrets reduced to presence metadata)
# --------------------------------------------------------------------------

def doctor_alias_report():
    """{divergences: [...], resolved: {name: {source, value}}} for every
    D6-table name (plus anything else ever resolved). SECRET_KEYS never
    carry a value: TOKEN reports {present, source}; API_KEY reports the
    full describe() shape {present, provider, source, length}."""
    resolved = {}
    for name in list(ALIAS_TABLE) + [n for n in _resolved if n not in ALIAS_TABLE]:
        if name == "API_KEY":
            resolved[name] = describe_api_key()
            continue
        if name in SECRET_KEYS:
            v, src = resolve(name)
            resolved[name] = {"present": bool(v), "source": src}
            continue
        v, src = resolve(name)
        resolved[name] = {"source": src, "value": v}
    return {"divergences": list(divergences), "resolved": resolved}
