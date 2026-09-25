"""Question API router — the Jev-compatible wire surface (T1.1 + T1.5).

Jev-compatible `systemone` question endpoint, Laya-native behind it:
``POST /v1/systemone`` (their path) and ``POST /v1/laya`` (ours) take
{state, model?, questions:{name:q}} and answer {answers:{name:a}, usage}
with the invariants of spec-v2 §1 held BY CONSTRUCTION (laya_ops) and the
envelope validated by laya_api_sec. Answers ride the Engine's chain rule:
trained slates hit their FT head, foreign slates ride chain-0.

Design rules:
  * ONE Engine singleton, ONE process: this module mounts INSIDE the warm
    daemon's ASGI app (laya_http_mcp) — same Core AI session as MCP.
  * T1.5 batching: multi-question requests run sequential per-question
    passes on the serialized engine worker over ONE HTTP round. A per-request
    wall budget (LAYA_REQUEST_BUDGET_S, default 90) aborts with a RETRYABLE
    overloaded 529 — nothing may hang (robustness by default).
  * API security: Bearer check via the TOKEN alias (constant-time compare,
    no secret in any body); loopback-only bind is the daemon's job; the
    60 000-char state cap and 1 MB response cap are enforced here; error
    bodies carry codes only — never prompt text, never secrets.
  * model is echoed VERBATIM (strict clients compare it); refusal bodies
    are {status:"invalid_request", error} — their client then raises
    ``malformed`` and fails open: the intended degraded path.
"""
import json
import os
import sys
import time
import uuid

import laya_api_sec as sec
from laya_ops import answer_from_choice, answer_from_noul, answer_from_score

PATHS = ("/v1/systemone", "/v1/laya")   # both namespaces hit the same handler
MODELS_PATH = "/v1/models"
DEFAULT_REQUEST_BUDGET_S = 90.0

_ANSWERERS = {"noul": answer_from_noul, "choice": answer_from_choice,
              "score": answer_from_score}


def _request_budget_s():
    try:
        return float(os.environ.get("LAYA_REQUEST_BUDGET_S")
                     or sec.naming.env_alias("REQUEST_BUDGET_S")
                     or DEFAULT_REQUEST_BUDGET_S)
    except ValueError:
        return DEFAULT_REQUEST_BUDGET_S


def health_payload(engine):
    """The daemon's GET /health document (laya_http_mcp serializes it).
    Built as a dict + json.dumps by the caller — never f-string repr:
    Python's list repr uses single quotes and breaks every strict JSON
    parser (RemoteEngine._chain_for, `curl | jq`). `warm` reports the
    barrier's honest state (_initialized flips only after the warmup
    probe answered), not a hardcoded true."""
    return {
        "ok": True,
        "warm": bool(getattr(engine, "_initialized", False)),
        "chains": list(engine.chain_order),
        "calls": engine.calls,
        "mean_latency_ms": round(engine.total_ms / max(1, engine.calls), 2),
    }


def answer_request(payload, engine, deadline=None):
    """Validate → answer every question → validate our own answers → build
    the response envelope. Pure-ish (takes the engine, no I/O): unit-testable
    with a fake engine. Raises LayaApiError (refusal) or RuntimeError from
    the engine (overloaded).

    T1.5: multi-question requests run sequential per-question passes on the
    serialized worker over ONE HTTP round. ``deadline`` (perf_counter
    timestamp) is checked BETWEEN questions — a batch that would overrun
    aborts with retryable overloaded BEFORE half-answering for minutes."""
    req = sec.validate_request(payload)          # 60k cap, shapes, model echo
    answers = {}
    for name, q in req["questions"].items():     # one HTTP round, serial passes
        if deadline is not None and time.perf_counter() > deadline:
            raise sec.LayaApiError(
                "overloaded", f"request budget exceeded after {len(answers)}"
                              f" of {len(req['questions'])} questions")
        raw = engine.question(name, q, req["state"])
        answers[name] = _ANSWERERS[q["type"]](name, q, raw)
    # We validate our OWN answers with the consumer's validators before
    # sending — an invariant break never leaves the process (interop is the
    # acceptance test, spec-v2 §9).
    sec.validate_answers(req["questions"], answers)
    body = {"answers": answers, "model": req["model"],
            "usage": {"requests": 1,
                      "chars_in": req["state_chars"],
                      "chars_out": len(json.dumps(answers,
                                                   ensure_ascii=False))}}
    sec.check_response_size(body)                # ≤ 1 MB (their client's cap)
    return body


# --------------------------------------------------------------------------
# minimal ASGI plumbing (same style as the daemon's _asgi; stdlib only)
# --------------------------------------------------------------------------

async def _read_json(receive):
    """Collect the request body and json-decode it. Raises LayaApiError on
    non-JSON or an over-cap body (cap + 64 KiB headroom so a slightly-over
    payload still gets the proper structured state refusal)."""
    chunks = []
    size = 0
    cap = sec.MAX_STATE_CHARS + 65_536
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            raise LayaStopped()
        chunks.append(msg.get("body", b""))
        size += len(chunks[-1])
        if size > cap:
            raise sec.LayaApiError("state_too_large",
                                   f"request body over {cap} bytes")
        if not msg.get("more_body"):
            break
    raw = b"".join(chunks)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise sec.LayaApiError("malformed", f"body is not JSON: {type(e).__name__}")


class LayaStopped(Exception):
    """Client disconnected mid-read — abandon without response."""


async def _send_json(send, status, obj, extra_headers=None):
    body = json.dumps(obj, sort_keys=False).encode("utf-8")
    headers = [(b"content-type", b"application/json"),
               (b"content-length", str(len(body)).encode())]
    headers += extra_headers or []
    await send({"type": "http.response.start", "status": status,
                "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _log_exc(code, exc):
    """One-line traceback note to stderr (the daemon's log). Response
    bodies stay code-only — but silence here would hide real bugs from
    the housekeeping sweep ('review the daemon logs, fix bugs first').
    Never raises: logging must not mask the refusal."""
    try:
        line = " ".join(str(exc).split())[:300]
        sys.stderr.write(f"laya-http: refused {code}: {type(exc).__name__}: {line}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _header(scope, name):
    want = name.lower().encode()
    for k, v in scope.get("headers", []):
        if k.lower() == want:
            return v.decode("latin-1")
    return None


async def handle(scope, receive, send, engine):
    """Route one HTTP scope. Returns True if this module answered.
    ``engine`` is the daemon's Engine singleton (question() path)."""
    path = scope.get("path", "")
    method = scope.get("method", "GET")

    if path == MODELS_PATH and method == "GET":
        # local model list (their /v1/models analogue; no account needed)
        mid = sec.naming.model_id(None)
        await _send_json(send, 200, {
            "object": "list",
            "data": [{"id": mid, "object": "model", "owned_by": "laya",
                      "chains": list(engine.chain_order)}]})
        return True

    if path not in PATHS:
        return False
    if method != "POST":
        await _send_json(send, 405, {"status": "invalid_request",
                                     "error": "POST only"})
        return True

    request_id = _header(scope, "x-request-id") or uuid.uuid4().hex[:16]
    req_hdr = [(b"x-request-id", request_id.encode())]
    try:
        sec.check_auth(_header(scope, "authorization"))   # Bearer, TOKEN alias
        payload = await _read_json(receive)
        import asyncio
        loop = asyncio.get_event_loop()
        budget = _request_budget_s()
        deadline = time.perf_counter() + budget

        def _run():
            return answer_request(payload, engine, deadline=deadline)

        # engine.question serializes on the worker; run the whole batch in
        # ONE executor thread so the event loop stays alive for /health.
        body = await loop.run_in_executor(None, _run)
        await _send_json(send, 200, body, req_hdr)
    except LayaStopped:
        return True                                # no response possible
    except sec.LayaApiError as e:
        await _send_json(send, e.http_status, e.to_response(), req_hdr)
    except RuntimeError as e:
        # Engine surfaced a worker error (ANE failure, bounded-submit
        # timeout): mirror the RETRYABLE overloaded semantics — their client
        # retries once and otherwise fails open. No internals in the body.
        _log_exc("overloaded", e)
        await _send_json(send, 529, {"status": "invalid_request",
                                     "error": "overloaded"}, req_hdr)
    except Exception as e:                          # never leak a traceback
        _log_exc("internal", e)                     # …but DO log it: daemon
        await _send_json(send, 500, {"status": "invalid_request",     # stderr,
                                     "error": "internal"}, req_hdr)   # housekeeping
    return True
