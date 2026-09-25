"""Capsule store for hermes-laya-handoff (spec-v2 §2 handoff row).

One capsule per conversation (lane), written by THIS module, consumed once
by the next fresh session. Layout under the profile home:

    <home>/laya/handoffs/handoff-<lane>.md    the capsule (0600)
    <home>/laya/handoffs/pending-<lane>.json  {"lane","created_ts",...}
    <home>/laya/handoffs/CONFIDENTIAL         marker file = authority

``<home>/jev/handoffs/`` is READ as the legacy alias location (D6): a
capsule written by an earlier jev-named install is still delivered;
writes always go to the laya path (one writer).

The capsule is the plain whole-dialogue tail (last ``max_words`` words)
plus the fixed Recovery block — model-free by default (§5: "default
handoff sends Jev nothing"; the model-written digest measured a loss).
Confidentiality: marker file or env alias or plugin config → the capsule
text is redacted through laya_decisions.redact before it hits disk
(a capsule is a written artifact — privacy-before-WRITE, §4 doctrine).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
# Vendored lib first (installed plugin ships its own copy of the module
# closure — Hermes installs ONLY the plugin dir; scripts/sync_plugin_lib.py
# keeps it byte-identical to server/, CI-checked). server/ = dev-tree source.
_SERVER = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "server"))
for _p in (os.path.join(_HERE, "lib"), _SERVER):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import naming
from laya_usecases import handoff_capsule

PENDING_MAX_AGE_S = 36 * 3600          # spec: 36 h max age (§2 handoff row)
TRIGGERS = ("handoff", "jev handoff", "laya handoff")
EXPORT_TIMEOUT = 60                    # one CLI export, not a hang


def home() -> Path:
    """The profile home via naming.hermes_root() (HERMES_HOME env seam —
    NOT env_alias: HERMES_HOME is host-addressing, not an aliasable Laya
    key; env_alias('HERMES_HOME') silently resolves None and would write
    capsules into the REAL home of a multiplexed host). The handoff dir
    is created lazily on WRITE only — questions like confidential_here()
    must not mkdir (dry-run promise)."""
    return naming.hermes_root()


def handoff_dir() -> Path:
    return home() / "laya" / "handoffs"


def legacy_dir() -> Path:
    return home() / "jev" / "handoffs"


CONFIDENTIAL_MARKER = "CONFIDENTIAL"


def confidential_here() -> bool:
    """Whether capsules must carry no sensitive detail. Three switches, on
    purpose (upstream lesson: a host without a plugin-config API returns
    the default SILENTLY — a marker file is one ``ls`` to verify and
    travels with the profile). The marker file is the authority."""
    if str(naming.env_alias("HANDOFF_CONFIDENTIAL", "") or "").strip().lower() \
            in ("1", "true", "yes", "on"):
        return True
    try:
        # NOT handoff_dir() side effects: exists() on a path is pure.
        return (handoff_dir() / CONFIDENTIAL_MARKER).exists() \
            or (legacy_dir() / CONFIDENTIAL_MARKER).exists()
    except OSError:
        return False


def _clean(lane: str) -> str:
    return "".join(c if c.isalnum() or c in "-.:" else "_"
                   for c in str(lane))[:120] or "default"


def lane_key(context: dict) -> str:
    """Stable per-conversation key — one capsule per conversation, not per
    session id. The hook that WRITES a capsule (dispatch: chat_id) and the
    hook that DELIVERS it (pre_llm_call: platform + sender_id, NO chat_id)
    do not receive the same fields — a capsule keyed on one and read with
    the other never matches and the feature fails silently. So:
    platform:chat:thread when chat_id is known; else the session store
    (lane_from_session); else platform:sender; else session_id."""
    platform = str(context.get("platform") or context.get("source") or "")
    parts = [platform, str(context.get("chat_id") or ""),
             str(context.get("thread_id") or "")]
    direct = ":".join(parts).strip(":")
    if not context.get("chat_id"):
        resolved = lane_from_session(str(context.get("session_id") or ""))
        if resolved:
            return resolved
        sender = str(context.get("sender_id") or "")
        if sender:
            direct = ":".join(p for p in (platform, sender) if p)
    key = direct or str(context.get("session_id") or "default")
    return _clean(key)


def lane_from_session(session_id: str) -> str:
    """Look the conversation identity up by session id in the session
    store (read-only sqlite; the store schema is the least-bad bridge
    between the two hooks' field sets). The store calls the platform
    column ``source`` — output uses the SAME part order as lane_key
    (platform:chat:thread) so both hooks compute one key. Empty on any
    failure (permissive fallback then applies in lane_key)."""
    if not session_id:
        return ""
    db = home() / "state.db"
    if not db.is_file():
        return ""
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            columns = {r[1] for r in conn.execute(
                "pragma table_info(sessions)")}
            wanted = [c for c in ("source", "chat_id", "thread_id")
                      if c in columns]
            if not wanted:
                return ""
            row = conn.execute(
                "select " + ", ".join(wanted) + " from sessions where id=?",
                (session_id,)).fetchone()
            if not row:
                return ""
            parts = [str(p or "") for p in row]
            return _clean(":".join(p for p in parts if p))
        finally:
            conn.close()
    except Exception:
        return ""


def capsule_path(lane: str) -> Path:
    return handoff_dir() / f"handoff-{lane}.md"


def pending_path(lane: str) -> Path:
    return handoff_dir() / f"pending-{lane}.json"


def is_trigger(text) -> bool:
    """ONLY an exact, bare word. A sentence mentioning a handoff is a
    normal message."""
    if not isinstance(text, str):
        return False
    return text.strip().strip(".!").lower() in TRIGGERS


# ── reading the conversation (the CLI is the contract, not the store) ───────

def _hermes_bin() -> list:
    override = os.environ.get("HERMES_CLI")
    if override and Path(override).exists():
        return [override]
    return ["hermes"]


def export_messages(session_id: str, runner=None) -> list:
    """The session's user/assistant turns via ``hermes sessions export``
    (stdout "-"). The CLI contract survives upgrades; the sqlite schema
    does not. Returns [] on ANY failure — a handoff never takes a session
    down with it."""
    run = runner or subprocess.run
    try:
        done = run(_hermes_bin() + ["sessions", "export", "--session-id",
                                    str(session_id), "--format", "jsonl",
                                    "-"],
                   capture_output=True, text=True, timeout=EXPORT_TIMEOUT,
                   env={**os.environ, "HERMES_HOME": str(home())})
    except Exception:
        return []
    if getattr(done, "returncode", 1) != 0:
        return []
    messages = []
    for line in (done.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        for m in row.get("messages") or []:
            if not isinstance(m, dict) or m.get("role") not in ("user",
                                                                 "assistant"):
                continue
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(str(p.get("text", ""))
                                   for p in content if isinstance(p, dict))
            if isinstance(content, str) and content.strip():
                messages.append({"role": m["role"],
                                 "content": content.strip()})
    return messages


# ── build / store / deliver ──────────────────────────────────────────────────

def build_capsule(session_id: str, lane: str, messages=None) -> dict:
    """Build the model-free capsule and store it as pending. messages
    defaults to the CLI export. Confidential → redact before WRITE (the
    capsule is disk, not memory — §4). Returns
    {status: ok|no_messages, path, words, model_used}."""
    msgs = messages if messages is not None \
        else export_messages(session_id)
    if not msgs:
        return {"status": "no_messages", "lane": lane}
    select = str(naming.env_alias("HANDOFF_JEV", "") or "").lower() \
        in ("1", "true", "yes", "on")
    out = handoff_capsule(msgs, select=select)   # tail + Recovery block
    text = out["capsule"]
    confidential = confidential_here()
    if confidential:
        from laya_decisions import redact
        text = redact(text)
    d = handoff_dir()
    d.mkdir(parents=True, exist_ok=True)
    cp = capsule_path(lane)
    tmp = cp.with_suffix(".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, cp)
    pp = pending_path(lane)
    pp.write_text(json.dumps({"lane": lane, "created_ts": time.time(),
                              "session_id": session_id,
                              "confidential": confidential}))
    os.chmod(pp, 0o600)
    return {"status": "ok", "path": str(cp), "words": out["words"],
            "model_used": out["model_used"], "lane": lane}


def take_pending(lane: str, max_age_s: float = PENDING_MAX_AGE_S,
                 current_session: str = "") -> str:
    """One-shot delivery: read the pending capsule, DELETE the pending
    marker (the capsule file stays for the audit trail), return the text
    or "" when absent/expired. Expired capsules are dropped unread."""
    pp = pending_path(lane)
    if not pp.is_file():
        return ""
    try:
        meta = json.loads(pp.read_text())
    except (OSError, ValueError):
        try:
            pp.unlink()
        except OSError:
            pass
        return ""
    age = time.time() - float(meta.get("created_ts") or 0)
    if age > max_age_s:
        try:
            pp.unlink()
        except OSError:
            pass
        return ""
    if meta.get("session_id") and meta["session_id"] == current_session:
        # The capsule was written BY this session; don't hand it back to
        # its own author (the upstream guard against self-injection).
        return ""
    try:
        text = capsule_path(lane).read_text()
    except OSError:
        text = ""
    try:
        pp.unlink()
    except OSError:
        pass
    return text
