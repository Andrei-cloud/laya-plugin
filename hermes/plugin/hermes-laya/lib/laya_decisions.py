"""Decision log — the local bookkeeping trail (spec-v2 §4).

Rules (binding):
  * DECISIONS ONLY, never prompt text: every record passes redaction and
    carries only structured fields + the model's verdict/confidence.
  * DUAL-WRITE (D6): every line is appended BYTE-IDENTICALLY to the Laya
    path and the Jev alias path, so the upstream router-dashboard reads our
    decisions with zero fork:
      <profile>/logs/laya-decisions.jsonl   (canonical)
      <profile>/logs/jev-decisions.jsonl    (alias mirror)
    where <profile> = LAYA_LOG_DIR / HERMES_HOME /logs / ~/.hermes/logs.
  * files are 0600; a failing log NEVER breaks a decision path (fail open,
    counted in `log_failures` for doctor).

Wire-invisible: nothing here touches the network.
"""
import json
import os
import threading
import time
from pathlib import Path

import laya_constants as K
import naming
from naming import env_alias

_lock = threading.Lock()
_failures = []          # (path, error) — never raised, surfaced by doctor()

KINDS = K.LOG_KINDS     # route / route_effective / skill / merged /
                        # skill_unreachable — keep vocabulary locked


def redact(text):
    """Redact emails/phones/tokens/keyvals/hex BEFORE anything is stored
    (spec §4 privacy-before-send; we redact even local writes — defense in
    depth: a log file must never hold PII or secret-shaped material)."""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, sort_keys=True)
    out = text
    for pat in K.REDACT_PATTERNS.values():
        out = pat.sub("[redacted]", out)
    return out


def log_dir():
    """<profile>/logs via the ONE resolver (D6): LAYA_LOG_DIR alias, then
    naming.hermes_root()/logs (HERMES_HOME env), then ~/.hermes/logs.
    NOTE: naming.hermes_root() reads os.environ DIRECTLY — it must not go
    through env_alias: HERMES_HOME is the host-addressing seam, not a
    Laya-surface alias key, so the alias table never carries it
    (env_alias("HERMES_HOME") resolves None and silently falls back to
    the real home — a multiplexed host would log into the wrong profile).
    Locked by test_log_dir_honours_hermes_home."""
    v = env_alias("LOG_DIR")
    if v:
        return Path(v)
    return naming.hermes_root() / "logs"


def _targets():
    d = log_dir()
    return (d / "laya-decisions.jsonl", d / "jev-decisions.jsonl")


def _write_line(path, line):
    path.parent.mkdir(parents=True, exist_ok=True)
    # create with 0600 semantics: open append, then fchmod (umask-proof)
    with open(path, "a") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(line)
        f.flush()


def log_decision(kind, record):
    """Append one decision record (dict) byte-identically under BOTH names.
    Returns True on full success; False (and records the failure) if either
    write failed — the caller NEVER sees an exception (robustness default).
    `kind` must be one of KINDS (their dashboard filters on it)."""
    if kind not in KINDS:
        return False
    rec = {"ts": round(time.time(), 3), "kind": kind}
    for key, val in record.items():
        rec[key] = redact(val) if isinstance(val, str) else val
    line = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
    ok = True
    with _lock:
        for path in _targets():
            try:
                _write_line(path, line)
            except OSError as e:
                _failures.append((str(path), f"{type(e).__name__}: {e}"))
                ok = False
    return ok


def tail(n=20):
    """Last n records from the canonical file (list of dicts); [] when
    missing/unreadable. Used by `laya status`/doctor and the bench rig."""
    path = _targets()[0]
    try:
        with open(path) as f:
            lines = f.readlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except (OSError, ValueError):
        return []


def doctor():
    """Safe-to-print health of the logging surface: paths, existence,
    divergence of the two files (must be byte-identical), write failures.
    Never the record contents."""
    canon, alias = _targets()

    def _stat(p):
        try:
            st = p.stat()
            return {"path": str(p), "exists": True, "bytes": st.st_size,
                    "mode": oct(st.st_mode & 0o777)}
        except OSError:
            return {"path": str(p), "exists": False}

    c, a = _stat(canon), _stat(alias)
    identical = None
    try:
        identical = (canon.read_bytes() == alias.read_bytes())
    except OSError:
        pass
    return {"canonical": c, "alias": a, "byte_identical": identical,
            "write_failures": len(_failures),
            "last_failure": _failures[-1] if _failures else None}
