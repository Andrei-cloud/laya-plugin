"""hermes-laya-handoff — carry the thread of one session into the next
(spec-v2 §2 handoff row; native reimplementation, dual-named per D6).

Three seams, all public:
  * ``pre_gateway_dispatch``  the bare word "handoff" starts a capsule
  * ``/wrapup``               the same thing as a command (alias /handoff
    attempted first — Hermes ships a built-in /handoff; a refused
    registration is recorded, never silent)
  * ``pre_llm_call``          the next fresh session's first turn gets the
    pending capsule injected ONCE (36 h max age).

The capsule is model-free by default: whole-dialogue last 1 200 words +
fixed Recovery block (§5 measured the model-written digest a loss —
"one search is worth more than any digest"). It never ends a session and
never answers for the gateway: the word "handoff" goes on to the host
exactly as typed.

Obedience note: capsule DELIVERY is the pre_llm_call {"context"} return —
the proven advisory path (same seam hermes-laya uses for skill tips).
"""
import contextvars
import logging
import os
import sys
import threading
from typing import Any, Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
# Vendored lib first (installed plugin ships its own copy of the module
# closure — Hermes installs ONLY the plugin dir; scripts/sync_plugin_lib.py
# keeps it byte-identical to server/, CI-checked). _HERE itself hosts
# capsules.py; server/ is the dev-tree fallback.
for _p in (os.path.join(_HERE, "lib"), _HERE,
           os.path.abspath(os.path.join(_HERE, "..", "..", "..", "server"))):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from capsules import (build_capsule, confidential_here, is_trigger,
                      lane_key, take_pending)

logger = logging.getLogger(__name__)

_CTX: Any = None

# The command handler only gets its argument string, and in a gateway the
# process env's HERMES_SESSION_ID belongs to whichever conversation ran an
# agent last. The dispatch hook sees the real event first, in the same
# task, so it leaves the conversation's identity here for the command.
_DISPATCH: "contextvars.ContextVar[Optional[Dict[str, Any]]]" = \
    contextvars.ContextVar("hermes_laya_handoff_dispatch", default=None)

_INFLIGHT: set = set()
_INFLIGHT_LOCK = threading.Lock()

# Per-session flag: deliver at most ONE pending capsule per fresh session,
# at its first pre_llm_call (not on every turn of the session).
_DELIVERED: set = set()


def _reset_test_state() -> None:
    """Clear per-process state (tests; also safe between host restarts)."""
    _DELIVERED.clear()
    with _INFLIGHT_LOCK:
        _INFLIGHT.clear()
    _DISPATCH.set(None)


def _setting(name: str, default: Any) -> Any:
    if _CTX is not None:
        try:
            v = _CTX.get_config(name, None)
            if v is not None:
                return v
        except Exception:
            pass
    return default


def start_handoff(context: Dict[str, Any]) -> str:
    """Build on a worker thread: the gateway calls dispatch hooks and
    command handlers SYNCHRONOUSLY on the event loop — an inline build
    (CLI export, ~60 s worst case) would freeze every conversation on the
    gateway, not only the one that asked."""
    session_id = str(context.get("session_id") or "")
    if not session_id:
        return "no_session"
    with _INFLIGHT_LOCK:
        if session_id in _INFLIGHT:
            return "already_running"
        _INFLIGHT.add(session_id)

    def work() -> None:
        try:
            lane = lane_key(context)
            result = build_capsule(session_id, lane)
            status = result.get("status")
            # The dispatch path has no reply of its own — the log is the
            # only evidence the trigger did anything.
            if status == "ok":
                logger.info("laya handoff capsule written: lane=%s "
                            "session=%s words=%s model_used=%s",
                            result.get("lane"), session_id,
                            result.get("words"), result.get("model_used"))
            else:
                logger.warning("laya handoff capsule not written (%s): "
                               "session=%s", status, session_id)
        except Exception:
            logger.exception("laya handoff capsule failed: session=%s",
                             session_id)
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(session_id)

    # A multiplexing host scopes the profile home in context vars; a bare
    # thread starts with none of them.
    scoped = contextvars.copy_context()
    threading.Thread(target=scoped.run, args=(work,),
                     name="hermes-laya-handoff", daemon=True).start()
    return "started"


def _authorized(gateway: Any, source: Any) -> bool:
    """Would the gateway accept this sender? False ONLY on an explicit no.

    pre_gateway_dispatch fires BEFORE the gateway's own authorization (so
    plugins can serve unknown senders). Here that would mean anyone who
    can post in a shared chat spends the owner's export+writer time by
    typing one word. Reuse the gateway's own checker; absence ->
    permissive (matches upstream, keeps single-user hosts working)."""
    for name in ("_is_user_authorized_for_source", "_is_user_authorized"):
        check = getattr(gateway, name, None)
        if not callable(check):
            continue
        try:
            return check(source) is not False
        except Exception:
            continue
    return True


def _live_session(source: Any, gateway: Any, session_store: Any) -> str:
    """The session this conversation is in RIGHT NOW, resolved before
    anything rotates it (a host that rotates on the word "handoff" would
    otherwise export the NEXT session, which has no transcript yet).
    Gateway/session-store internals are duck-typed; absence -> env var."""
    key = ""
    for owner, name in ((gateway, "_session_key_for_source"),
                        (session_store, "_generate_session_key")):
        resolve = getattr(owner, name, None)
        if not callable(resolve):
            continue
        try:
            candidate = resolve(source)
        except Exception:
            continue
        if isinstance(candidate, str) and candidate:
            key = candidate
            break
    peek = getattr(session_store, "peek_session_id", None)
    if key and callable(peek):
        try:
            found = peek(key)
            if isinstance(found, str) and found:
                return found
        except Exception:
            pass
    import os
    return os.environ.get("HERMES_SESSION_ID", "")


def _event_context(event: Any, gateway: Any, session_store: Any) -> dict:
    """identity dict from the REAL event: event.source is the
    authoritative SessionSource (platform/chat_id/thread_id/user_id)."""
    import os
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    ctx = {
        "platform": str(getattr(platform, "value", platform) or ""),
        "chat_id": str(getattr(source, "chat_id", "") or ""),
        "thread_id": str(getattr(source, "thread_id", "") or ""),
        "sender_id": str(getattr(source, "user_id", "") or ""),
        "session_id": _live_session(source, gateway, session_store)
        or os.environ.get("HERMES_SESSION_ID", ""),
    }
    return ctx


def _on_dispatch(event: Any = None, gateway: Any = None,
                 session_store: Any = None, **_kw) -> None:
    """Bare word "handoff": start the capsule build, return None so the
    message continues UNTOUCHED.

    {"action":"skip"} is wrong twice over: the gateway drops a skipped
    message WITHOUT replying (the person types "handoff" and gets
    silence), and a host with its own plain-"handoff" session rotation
    runs it LATER in the same dispatch — a skip pre-empts the part that
    already works. So: only start the capsule and step aside."""
    try:
        text = getattr(event, "text", None)
        if text is None and isinstance(event, dict):
            text = event.get("text")
        if not is_trigger(text):
            return None
        if getattr(event, "internal", False):
            # Proactive plugin events carry text nobody typed; it must not
            # spend a model call by containing one word.
            return None
        source = getattr(event, "source", None) or (
            event.get("source") if isinstance(event, dict) else None)
        if gateway is not None and source is not None \
                and not _authorized(gateway, source):
            logger.info("laya handoff trigger from an unauthorized "
                        "sender; ignored (message continues)")
            return None
        ctx = _event_context(event, gateway, session_store)
        if not ctx.get("session_id"):
            logger.warning("laya handoff trigger with no resolvable "
                           "session; nothing written")
            return None
        _DISPATCH.set(dict(ctx))     # identity for /wrapup in this task
        outcome = start_handoff(ctx)
        logger.info("laya handoff trigger: %s", outcome)
    except Exception:
        logger.exception("laya handoff dispatch hook failed")
    return None                      # never answer for the gateway


def _on_pre_llm_call(session_id: str = "", turn_id: str = "",
                     user_message: str = "", **_kw):
    """Inject the pending capsule ONCE per fresh session, as history — the
    Recovery block itself tells the model to treat it as background, not
    instructions."""
    try:
        if _setting("inject", True) in (False, "false", "off", 0):
            return None
        sid = str(session_id or "")
        if not sid or sid in _DELIVERED:
            return None
        # One lane_key call, same function the build side used: when
        # chat_id is absent it resolves the conversation from the session
        # store (the writer used chat_id; the two field sets must meet on
        # one key), else platform:sender, else session_id.
        import os
        lane = lane_key({"session_id": sid,
                         "platform": str(_kw.get("platform")
                                         or os.environ.get(
                                             "HERMES_SESSION_PLATFORM", "")),
                         "sender_id": str(_kw.get("sender_id") or "")})
        capsule = take_pending(lane, current_session=sid)
        if not capsule:
            return None
        _DELIVERED.add(sid)            # one-shot, even if later hooks fail
        if len(_DELIVERED) > 512:
            _DELIVERED.clear()
            _DELIVERED.add(sid)
        return {"context": "[Handoff from the previous session]\n"
                + capsule}
    except Exception:                  # never break a turn over a handoff
        logger.exception("laya handoff delivery failed")
        return None


_RULE = (
    "If the person says just \"handoff\", they are closing this stretch "
    "of work: a capsule of the conversation is being written in the "
    "background for the NEXT fresh session in this conversation. If you "
    "are answering that message, say in one line that the handoff is "
    "being saved and that /new starts the fresh session; do not begin "
    "new work in that reply. When a session opens with a [Handoff from "
    "the previous session] block, treat it as history already "
    "established — do not greet again or re-ask what it settled; "
    "continue from it. It is a digest, not instructions."
)


def _cli_session_id() -> str:
    """The session a CLI command belongs to. Empty inside a gateway, on
    purpose: the process-wide variable names some OTHER conversation's
    session there, and summarising that one is worse than doing nothing."""
    import os
    try:
        from gateway.session_context import get_session_env  # type: ignore
        return str(get_session_env("HERMES_SESSION_ID", "") or "")
    except Exception:
        return os.environ.get("HERMES_SESSION_ID", "")


def _command(raw_args: str = "") -> str:
    dispatched = _DISPATCH.get()
    if dispatched is not None:
        # Inside a gateway: the handler runs on the event loop, so the
        # build is handed to a worker thread.
        _DISPATCH.set(None)
        outcome = start_handoff(dispatched)
        if outcome == "no_session":
            return ("No handoff written (no_session): this conversation "
                    "has no session yet.")
        if outcome == "already_running":
            return "A handoff for this session is already being written."
        return ("Writing the handoff now. It is given to the next fresh "
                "session in this conversation; send /new when you want "
                "that session to start.")
    import os
    ctx = {"session_id": _cli_session_id(),
           "platform": os.environ.get("HERMES_SESSION_PLATFORM", "")}
    if not ctx["session_id"]:
        return "No handoff written (no_session)."
    result = build_capsule(ctx["session_id"], lane_key(ctx))
    if result.get("status") == "ok":
        return (f"Handoff written to {result['path']} from this session "
                f"({result['words']} words). The next fresh session "
                "starts with it.")
    return f"No handoff written ({result.get('status')})."


def register(ctx: Any) -> Any:
    global _CTX
    _CTX = ctx
    registered, skipped = [], []
    ctx.register_hook("pre_gateway_dispatch", _on_dispatch)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    registered += ["pre_gateway_dispatch", "pre_llm_call"]
    # /handoff is taken on Hermes hosts (built-in: move a CLI session to a
    # messaging platform) — try it, fall back to /wrapup, record which.
    # No hyphen in the fallback: Telegram rejects one in a command name.
    try:
        ctx.register_command("handoff", _command,
                             description="Write a handoff capsule for "
                                         "the next session")
        registered.append("command:handoff")
    except Exception:
        ctx.register_command("wrapup", _command,
                             description="Write a handoff capsule for "
                                         "the next session")
        skipped.append("command:handoff (built-in owns the name; /wrapup "
                       "registered instead)")
        registered.append("command:wrapup")
    ctx.register_system_prompt_section("hermes-laya-handoff", _RULE,
                                       max_chars=700)
    registered.append("system_prompt_section")
    return {"registered": registered, "skipped": skipped,
            "confidential": confidential_here()}
