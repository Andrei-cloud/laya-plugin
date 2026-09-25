"""Offline tests for the question-API router (T1.1 + T1.5): pure
answer_request with a FAKE engine, plus the ASGI plumbing with fake
scope/receive/send. No daemon, no network, no Core AI.

Covers: envelope passthrough + model echo + usage honesty, own-answer
self-validation (an engine that emits a broken distribution must yield a
529/400-class refusal, never a bad answer), refusal body shape
{status:"invalid_request", error} per code, Bearer auth, request-id echo,
batch budget abort (overloaded before minutes of half-answers), 405,
/v1/models, 1 MB cap.
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))

import laya_api_http as api      # noqa: E402
import laya_api_sec as sec       # noqa: E402


class FakeEngine:
    chain_order = ["base"]

    def __init__(self, probs_fn=None):
        self.probs_fn = probs_fn or (lambda names: {
            n: (1.0 if i == 0 else 0.0) for i, n in enumerate(names)})
        self.n = 0

    def question(self, name, q, state):
        self.n += 1
        if q["type"] == "noul":
            names = ["no", "yes"]
        elif q["type"] == "score":
            names = [str(i) for i in range(len(q["criteria"]))]
        else:
            names = list(q["criteria"])
        probs = self.probs_fn(names)
        top = max(names, key=lambda k: probs[k])
        return {"task": "base", "chain": "base", "choice": top,
                "confidence": probs[top], "acted": True, "act_p": 0.5,
                "probs": probs, "latency_ms": 16.0}


def _scope(path="/v1/systemone", method="POST", auth=None, body=b"{}",
           extra=None):
    headers = [(b"content-type", b"application/json")]
    if auth:
        headers.append((b"authorization", auth.encode()))
    if extra:
        headers += extra
    return {"type": "http", "path": path, "method": method,
            "headers": headers, "body": body}


def _transport(scope):
    """(receive, send, sent) for one request; `sent` collects ASGI messages."""
    body = scope.get("body", b"")
    sent = []
    state = {"sent_body": False}

    async def receive():
        if not state["sent_body"]:
            state["sent_body"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)
    return receive, send, sent


def _run_sending(scope, receive, send, engine):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(api.handle(scope, receive, send, engine))
    finally:
        loop.close()


def _decoded(sent):
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    return start["status"], json.loads(body.decode()), start.get("headers", [])


class TestAnswerRequestPure:
    def test_noul_round_trip_with_usage_and_echo(self):
        e = FakeEngine()
        body = api.answer_request(
            {"state": "s", "model": "jev-latest",
             "questions": {"rel_0": {"type": "noul",
                                     "instructions": "relevant?"}}}, e)
        assert body["answers"]["rel_0"] == {"type": "noul", "noul": 0.0}
        assert body["model"] == "jev-latest"          # verbatim echo
        assert body["usage"] == {"requests": 1, "chars_in": 3, "chars_out":
                                 len(json.dumps(body["answers"]))}

    def test_multi_question_one_pass_each(self):
        e = FakeEngine()
        qs = {f"q{i}": {"type": "noul", "instructions": f"q{i}? i?"}
              for i in range(50)}
        body = api.answer_request({"state": "s", "questions": qs}, e)
        assert len(body["answers"]) == 50
        assert e.n == 50                               # one engine pass/question

    def test_broken_engine_answer_is_never_sent(self):
        # probs that cannot be renormalized sanely (all-zero mass) -> the
        # mapper raises ValueError -> surfaces as RuntimeError-ish failure,
        # NOT a wire answer.
        def zero_mass(names):
            return {n: 0.0 for n in names}
        e = FakeEngine(probs_fn=zero_mass)
        with pytest.raises(ValueError):
            api.answer_request({"state": "s", "questions": {
                "q": {"type": "choice", "instructions": "pick?",
                      "criteria": {"a": "a", "b": "b"}}}}, e)

    def test_deadline_aborts_batch_with_overloaded(self):
        e = FakeEngine()
        qs = {f"q{i}": {"type": "noul", "instructions": f"n{i}?"}
              for i in range(20)}
        import time as _t
        with pytest.raises(sec.LayaApiError) as ei:
            api.answer_request({"state": "s", "questions": qs}, e,
                               deadline=_t.perf_counter() - 1)   # already past
        assert ei.value.code == "overloaded"
        assert e.n == 0                                # aborted BEFORE work

    def test_refusal_is_structured_not_traceback(self):
        e = FakeEngine()
        with pytest.raises(sec.LayaApiError) as ei:
            api.answer_request({"state": "s", "questions": {
                "q": {"type": "ranking", "instructions": "?"}}}, e)
        assert ei.value.to_response() == {
            "status": "invalid_request",
            "error": "unknown_question_type"}


class TestASGIPlumbing:
    def _post(self, path, obj, auth=None, extra=None):
        scope = _scope(path=path, auth=auth, extra=extra,
                       body=json.dumps(obj).encode())
        receive, send, sent = _transport(scope)
        assert _run_sending(scope, receive, send, FakeEngine()) is True
        return _decoded(sent)

    def test_systemone_path_serves(self):
        status, body, hdrs = self._post(
            "/v1/systemone",
            {"state": "s", "questions": {"q": {"type": "noul",
                                               "instructions": "ok?"}}})
        assert status == 200
        assert body["answers"]["q"]["type"] == "noul"
        assert any(k == b"x-request-id" for k, v in
                   [(h[0], h[1]) for h in hdrs])

    def test_laya_path_serves_identically(self):
        payload = {"state": "s", "questions": {"q": {"type": "noul",
                                                     "instructions": "ok?"}}}
        a = self._post("/v1/systemone", payload)[1]
        b = self._post("/v1/laya", payload)[1]
        assert a["answers"] == b["answers"]

    def test_get_is_405(self):
        scope = _scope(path="/v1/systemone", method="GET")
        receive, send, sent = _transport(scope)
        _run_sending(scope, receive, send, FakeEngine())
        assert _decoded(sent)[0] == 405

    def test_bad_json_is_malformed_400(self):
        scope = _scope(body=b"{not json")
        receive, send, sent = _transport(scope)
        _run_sending(scope, receive, send, FakeEngine())
        status, body, _ = _decoded(sent)
        assert status == 400
        assert body == {"status": "invalid_request", "error": "malformed"}

    def test_state_too_large_400(self):
        big = "x" * 70_000
        status, body, _ = self._post(
            "/v1/systemone",
            {"state": big, "questions": {"q": {"type": "noul",
                                               "instructions": "?"}}})
        assert status == 400
        assert body["error"] == "state_too_large"

    def test_auth_required_when_token_configured(self, monkeypatch):
        monkeypatch.setenv("LAYA_TOKEN", "sekrit-token")
        sec.naming._reset_state()
        payload = {"state": "s", "questions": {"q": {"type": "noul",
                                                     "instructions": "?"}}}
        # no header -> 401 auth_failed, and the body never carries the token
        status, body, _ = self._post("/v1/systemone", payload)
        assert status == 401 and body["error"] == "auth_failed"
        assert "sekrit-token" not in json.dumps(body)
        # wrong token -> same
        status2, _, _ = self._post("/v1/systemone", payload,
                                   auth="Bearer nope")
        assert status2 == 401
        # right token -> 200
        status3, body3, _ = self._post("/v1/systemone", payload,
                                       auth="Bearer sekrit-token")
        assert status3 == 200 and "answers" in body3
        sec.naming._reset_state()

    def test_request_id_echoed_back(self):
        scope = _scope(body=json.dumps(
            {"state": "s", "questions": {"q": {"type": "noul",
                                               "instructions": "?"}}}).encode(),
            extra=[(b"x-request-id", b"abc123")])
        receive, send, sent = _transport(scope)
        _run_sending(scope, receive, send, FakeEngine())
        _, _, hdrs = _decoded(sent)
        assert (b"x-request-id", b"abc123") in hdrs

    def test_engine_runtime_error_maps_to_529_no_internals(self):
        class Boom(FakeEngine):
            def question(self, name, q, state):
                raise RuntimeError("ANECompiler FAILED secret path detail")
        status, body, _ = None, None, None
        scope = _scope(body=json.dumps(
            {"state": "s", "questions": {"q": {"type": "noul",
                                               "instructions": "?"}}}).encode())
        receive, send, sent = _transport(scope)
        _run_sending(scope, receive, send, Boom())
        status, body, _ = _decoded(sent)
        assert status == 529
        assert body == {"status": "invalid_request", "error": "overloaded"}
        assert "ANE" not in json.dumps(body)           # no internals leak

    def test_models_lists_canonical_id(self, monkeypatch):
        monkeypatch.delenv("LAYA_MODEL", raising=False)
        monkeypatch.delenv("JEV_MODEL", raising=False)
        monkeypatch.delenv("TYPESAFE_MODEL", raising=False)
        sec.naming._reset_state()
        scope = _scope(path="/v1/models", method="GET")
        receive, send, sent = _transport(scope)
        assert _run_sending(scope, receive, send, FakeEngine()) is True
        status, body, _ = _decoded(sent)
        assert status == 200
        assert body["data"][0]["id"] == "laya-r15"
        sec.naming._reset_state()

    def test_unknown_path_returns_false_for_daemon(self):
        scope = _scope(path="/nope")
        receive, send, sent = _transport(scope)
        assert _run_sending(scope, receive, send, FakeEngine()) is False


class _HealthEngine:
    """Engine stub exposing exactly what health_payload reads."""
    chain_order = ["base", "ft_lang", "ft_gr14_ppo"]

    def __init__(self, initialized=True, calls=12, total_ms=626.4):
        self._initialized = initialized
        self.calls = calls
        self.total_ms = total_ms


class TestHealthPayload:
    """GET /health is the daemon's liveness contract (RemoteEngine._chain_for
    and `curl | jq` parse it). The first live build emitted Python's LIST
    REPR ({'chains':['a','b'],...}) — invalid JSON for strict parsers.
    Regression: the payload must serialize to strict JSON and report the
    barrier's honest warm state."""

    def test_serializes_to_strict_json(self):
        doc = json.dumps(api.health_payload(_HealthEngine()))
        back = json.loads(doc)                      # raises on repr-output
        assert "chains" in back and isinstance(back["chains"], list)

    def test_warm_reports_barrier_state_not_constant(self):
        assert api.health_payload(_HealthEngine(initialized=True))["warm"] is True
        assert api.health_payload(_HealthEngine(initialized=False))["warm"] is False

    def test_fields_and_latency_math(self):
        h = api.health_payload(_HealthEngine(calls=12, total_ms=626.4))
        assert h["ok"] is True
        assert h["chains"] == ["base", "ft_lang", "ft_gr14_ppo"]
        assert h["calls"] == 12 and h["mean_latency_ms"] == 52.2

    def test_zero_calls_no_division_error(self):
        h = api.health_payload(_HealthEngine(calls=0, total_ms=0.0))
        assert h["mean_latency_ms"] == 0.0
