"""Tests for server/laya_api_sec.py — the question-API security/auth/
validation core + error vocabulary (spec-v2 §1, plan T1.1/T1.6 support).

Offline, stdlib + pytest only: no Engine, no HTTP server — the router
layer is tested separately (T1.1/T1.8)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import naming                      # noqa: E402
from server import laya_api_sec as sec         # noqa: E402

# every env spelling the resolver understands — cleared per test so a test
# writing os.environ directly can never leak into the next module's tests
_ALL_ENV = [f"{ns}{n}" for n in naming.ALIAS_TABLE
            for ns in ("LAYA_", "JEV_", "TYPESAFE_")] + [
    "DASHBOARD_TOKEN", "HERMES_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"]


@pytest.fixture(autouse=True)
def clean_token_env(monkeypatch):
    for v in _ALL_ENV:
        monkeypatch.delenv(v, raising=False)
    naming._reset_state()
    yield
    naming._reset_state()


def q_choice(name="q", opts=("a", "b")):
    return {"type": "choice", "instructions": f"pick one of {name}",
            "criteria": {o: f"meaning {o}" for o in opts}}


def q_noul(name="q"):
    return {"type": "noul", "instructions": f"is {name} yes?"}


def q_score(name="q", levels=("trivial", "expert")):
    return {"type": "score", "instructions": f"rate {name}",
            "criteria": list(levels)}


def req(**over):
    body = {"state": "hello", "model": "jev-latest",
            "questions": {"q": q_noul()}}
    body.update(over)
    return body


# ------------------------------------------------------------ vocabulary ----

def test_error_codes_mirror_jeverror_set():
    assert {"no_key", "state_too_large", "timeout", "network", "malformed",
            "invalid_response", "response_too_large", "auth_failed",
            "credits_exhausted", "rate_limited", "overloaded"} <= sec.ERROR_CODES


def test_retryable_set_matches_client():
    for c in ("rate_limited", "overloaded", "network",
              "http_500", "http_502", "http_503", "http_504"):
        assert sec.is_retryable(c)
    for c in ("auth_failed", "malformed", "invalid_response",
              "state_too_large", "http_404", "no_key"):
        assert not sec.is_retryable(c)


def test_http_status_mapping_and_open_ended_http_codes():
    assert sec.http_status("auth_failed") == 401
    assert sec.http_status("rate_limited") == 429
    assert sec.http_status("overloaded") == 529
    assert sec.http_status("credits_exhausted") == 402
    assert sec.http_status("http_418") == 418       # http_<status> family
    assert sec.LayaApiError("rate_limited").http_status == 429


def test_error_response_body_shape_and_never_secrets():
    err = sec.LayaApiError("invalid_response", invariant="argmax")
    body = err.to_response()
    assert body == {"status": "invalid_request", "error": "invalid_response",
                    "invariant": "argmax"}
    assert json.dumps(body).count("Bearer") == 0


# ------------------------------------------------------------------ auth ----

def test_auth_open_when_no_token_configured():
    assert sec.check_auth(None) is True            # loopback trust
    assert sec.check_auth("Bearer whatever") is True


def test_auth_accepts_configured_token_via_laya_alias():
    naming.os.environ["LAYA_TOKEN"] = "s3cret"
    assert sec.check_auth("Bearer s3cret") is True


def test_auth_token_alias_chain_jev_then_dashboard():
    naming.os.environ["DASHBOARD_TOKEN"] = "dash"
    assert sec.check_auth("Bearer dash") is True
    naming.os.environ["JEV_TOKEN"] = "jevtok"      # JEV beats DASHBOARD
    assert sec.check_auth("Bearer jevtok") is True
    with pytest.raises(sec.LayaApiError) as ei:
        sec.check_auth("Bearer dash")              # old value no longer valid
    assert ei.value.code == "auth_failed"


def test_auth_rejections_and_case_insensitive_scheme():
    naming.os.environ["LAYA_TOKEN"] = "s3cret"
    for bad in (None, "", "Basic s3cret", "Bearer", "Bearer wrong",
                "bearer s3cret "):
        if bad == "bearer s3cret ":
            assert sec.check_auth(bad) is True     # scheme case + strip OK
            continue
        with pytest.raises(sec.LayaApiError) as ei:
            sec.check_auth(bad)
        assert ei.value.code == "auth_failed"


def test_auth_error_body_never_contains_token():
    naming.os.environ["LAYA_TOKEN"] = "s3cret-token"
    with pytest.raises(sec.LayaApiError) as ei:
        sec.check_auth("Bearer wrong-token")
    blob = json.dumps(ei.value.to_response()) + repr(ei.value.detail)
    assert "s3cret-token" not in blob and "wrong-token" not in blob


def test_auth_expected_override_bypasses_env():
    assert sec.check_auth("Bearer x", expected="x") is True


# ------------------------------------------------------- request envelope ----

def test_valid_request_normalizes_and_echoes_model_verbatim():
    out = sec.validate_request(req())
    assert out["model"] == "jev-latest"            # strict clients compare
    assert out["state_chars"] == len(json.dumps("hello"))
    assert "q" in out["questions"]


def test_model_default_when_absent_or_empty():
    body = req()
    del body["model"]
    assert sec.validate_request(body)["model"] == "laya-r15"
    assert sec.validate_request(req(model=""))["model"] == "laya-r15"


def test_model_alias_env_default():
    naming.os.environ["TYPESAFE_MODEL"] = "jev-latest"
    body = req()
    del body["model"]
    assert sec.validate_request(body)["model"] == "jev-latest"


def test_state_type_and_size_cap():
    with pytest.raises(sec.LayaApiError) as ei:
        sec.validate_request(req(state=123))
    assert ei.value.code == "state_type"
    big = "x" * 60_001
    with pytest.raises(sec.LayaApiError) as ei:
        sec.validate_request(req(state=big))
    assert ei.value.code == "state_too_large"
    ok = "x" * 59_998                              # JSON-encoded (quoted) == 60 000
    assert sec.validate_request(req(state=ok))["state_chars"] == 60_000
    obj = ["y" * 59_997]                           # array state, JSON just over
    with pytest.raises(sec.LayaApiError) as ei:
        sec.validate_request(req(state=obj))
    assert ei.value.code == "state_too_large"


def test_state_required_and_envelope_must_be_object():
    body = req()
    del body["state"]
    with pytest.raises(sec.LayaApiError) as ei:
        sec.validate_request(body)
    assert ei.value.code == "state_required"
    with pytest.raises(sec.LayaApiError) as ei:
        sec.validate_request(["not", "an", "object"])
    assert ei.value.code == "envelope_not_object"


def test_questions_mapping_required():
    for bad in ({}, [], "x", None, {"": q_noul()}):
        with pytest.raises(sec.LayaApiError) as ei:
            sec.validate_request(req(questions=bad))
        assert ei.value.code in ("questions_required", "invalid_question_name")


# -------------------------------------------------------- question shapes ----

def validate_q(name, q):
    # deep copy: the mutation tests below reuse the builders, and
    # validate_* must never mutate caller payloads
    return sec.validate_request(req(questions={name: json.loads(json.dumps(q))}))


def test_wire_types_accepted_each_with_own_criteria_rule():
    validate_q("a", q_noul())
    validate_q("b", q_choice())
    validate_q("c", q_score(levels=("trivial", "adept", "expert")))


def test_noul_must_not_carry_criteria():
    q = q_noul()
    q["criteria"] = {"yes": "y", "no": "n"}
    with pytest.raises(sec.LayaApiError) as ei:
        validate_q("q", q)
    assert ei.value.code == "noul_criteria_forbidden"


def test_choice_needs_mapping_with_two_options():
    for bad in (None, {}, {"only": "one"}, ["a", "b"], {"a": "", "": "b"}):
        q = q_choice()
        q["criteria"] = bad
        with pytest.raises(sec.LayaApiError) as ei:
            validate_q("q", q)
        assert ei.value.code in ("choice_needs_two_options",
                                 "choice_option_names_invalid")


def test_score_needs_ordered_list_two_levels_lowest_first_semantics():
    for bad in (None, [], ["one"], {"0": "trivial", "1": "expert"}, ["", "x"]):
        q = q_score()
        q["criteria"] = bad
        with pytest.raises(sec.LayaApiError) as ei:
            validate_q("q", q)
        assert ei.value.code in ("score_needs_two_levels", "score_levels_invalid")


def test_unknown_type_refused():
    q = q_noul()
    q["type"] = "binary"                           # v1's invented vocabulary
    with pytest.raises(sec.LayaApiError) as ei:
        validate_q("q", q)
    assert ei.value.code == "unknown_question_type"


def test_instructions_nonempty_and_not_its_own_name():
    for bad in ("", "   "):
        q = q_noul()
        q["instructions"] = bad
        with pytest.raises(sec.LayaApiError) as ei:
            validate_q("q", q)
        assert ei.value.code == "instructions_required"
    q = {"type": "noul", "instructions": "q"}
    with pytest.raises(sec.LayaApiError) as ei:
        validate_q("q", q)
    assert ei.value.code == "instructions_equals_name"


def test_cli_only_aliases_refused_on_http():
    aliased = {"kind": "noul", "text": "is it yes?"}
    with pytest.raises(sec.LayaApiError) as ei:
        validate_q("q", aliased)
    assert ei.value.code == "cli_alias_on_http"
    half = {"type": "noul", "instructions": "ok?", "text": "alias leak"}
    with pytest.raises(sec.LayaApiError) as ei:
        validate_q("q", half)
    assert ei.value.code == "cli_alias_on_http"


def test_question_must_be_object():
    with pytest.raises(sec.LayaApiError) as ei:
        validate_q("q", "just a string")
    assert ei.value.code == "question_not_object"


# --------------------------------------------------------- invariants ------

def a_noul(p):
    return {"type": "noul", "noul": p}


def a_choice(choice, probs, conf=0.9):
    return {"type": "choice", "choice": choice,
            "probabilities": probs, "confidence": conf}


def a_score(score, probs, spread=True, conf=0.9):
    a = {"type": "score", "score": score, "probabilities": probs,
         "spread_reported": spread, "confidence": conf}
    return a


def check(question, answer):
    sec.validate_answer("q", question, answer)


def test_good_answers_pass():
    check(q_noul(), a_noul(0.42))
    check(q_noul(), a_noul(0.0))
    check(q_noul(), a_noul(1.0))
    check(q_choice(), a_choice("a", {"a": 0.7, "b": 0.3}))
    check(q_score(), a_score(0.25, {"0": 0.75, "1": 0.25}))
    # score without spread_reported: mass invariant not enforced (their rule)
    check(q_score(), a_score(0.0, {"0": 0.5, "1": 0.5}, spread=False))
    # confidence absent → their default 1.0, still valid
    a = a_score(0.25, {"0": 0.75, "1": 0.25})
    del a["confidence"]
    check(q_score(), a)


def test_noul_range_and_type():
    for bad in (-0.01, 1.01, "0.5", None):
        a = {"type": "noul", "noul": bad}
        with pytest.raises(sec.LayaApiError) as ei:
            check(q_noul(), a)
        assert ei.value.code == "invalid_response"
        assert ei.value.invariant == "noul_range"


def test_answer_type_mismatch():
    with pytest.raises(sec.LayaApiError) as ei:
        check(q_noul(), a_choice("a", {"a": 1.0}))
    assert ei.value.invariant == "answer_type_mismatch"


def test_probability_keys_must_equal_options_exactly():
    with pytest.raises(sec.LayaApiError) as ei:
        check(q_choice(), a_choice("a", {"a": 0.7, "c": 0.3}))
    assert ei.value.invariant == "probability_keys"
    with pytest.raises(sec.LayaApiError) as ei:
        check(q_choice(), a_choice("a", {"a": 1.0}))
    assert ei.value.invariant == "probability_keys"


def test_probability_mass_sigma_tolerance():
    # Σp = 0.98 → outside 1 ± 0.01+1e-12 (the floor-slip-adjacent case)
    with pytest.raises(sec.LayaApiError) as ei:
        check(q_choice(), a_choice("a", {"a": 0.68, "b": 0.30}))
    assert ei.value.invariant == "probability_mass"
    # Σp = 1 + 0.005 is inside tolerance
    check(q_choice(), a_choice("a", {"a": 0.705, "b": 0.30}))


def test_argmax_invariant_and_tolerance():
    # choice not the argmax at all
    with pytest.raises(sec.LayaApiError) as ei:
        check(q_choice(), a_choice("b", {"a": 0.7, "b": 0.3}))
    assert ei.value.invariant == "argmax"
    # choice is an option but not on the slate → argmax too
    with pytest.raises(sec.LayaApiError):
        check(q_choice(opts=("a", "b", "c")), a_choice("d", {"a": 0.4, "b": 0.3, "c": 0.3}))
    # within ARGMAX_TOL of the max (exact ties) → passes
    check(q_choice(opts=("a", "b")), a_choice("b", {"a": 0.5, "b": 0.5}))
    # just off the max but inside 1e-9 → passes
    check(q_choice(opts=("a", "b")), a_choice("b", {"a": 0.5 + 5e-10, "b": 0.5 - 5e-10}))
    # outside 1e-9 → fails
    with pytest.raises(sec.LayaApiError) as ei:
        check(q_choice(opts=("a", "b")), a_choice("b", {"a": 0.5 + 1e-6, "b": 0.5 - 1e-6}))
    assert ei.value.invariant == "argmax"


def test_score_band():
    q = q_score(levels=("trivial", "adept", "expert"))   # L=3 -> band [-0.5, 2.5]
    for bad in (-0.6, 2.6):
        with pytest.raises(sec.LayaApiError) as ei:
            check(q, a_score(bad, {"0": 1 / 3, "1": 1 / 3, "2": 1 / 3}))
        assert ei.value.invariant == "score_band"
    # band edges (-0.5 / L-0.5) are fail-open scores: legal only without a
    # reported spread — an integer-level expectation can never equal them
    check(q, a_score(-0.5, {"0": 1.0, "1": 0.0, "2": 0.0}, spread=False))
    check(q, a_score(2.5, {"0": 0.0, "1": 0.0, "2": 1.0}, spread=False))
    # spread-consistent edges: expectation agrees with the score
    check(q, a_score(0.0, {"0": 1.0, "1": 0.0, "2": 0.0}))
    check(q, a_score(2.0, {"0": 0.0, "1": 0.0, "2": 1.0}))


def test_score_matches_its_distribution_only_when_spread_reported():
    probs = {"0": 0.75, "1": 0.25}
    with pytest.raises(sec.LayaApiError) as ei:      # spread reported → checked
        check(q_score(), a_score(0.0, probs, spread=True))
    assert ei.value.invariant == "score_matches_its_distribution"
    check(q_score(), a_score(0.0, probs, spread=False))   # not checked
    # mass checked first when both broken
    with pytest.raises(sec.LayaApiError) as ei:
        check(q_score(), a_score(0.5, {"0": 0.5, "1": 0.1}, spread=True))
    assert ei.value.invariant == "probability_mass"
    # expectation within 0.02+1e-12 → passes (0.25 vs 0.24)
    check(q_score(), a_score(0.24, {"0": 0.76, "1": 0.24}, spread=True))


def test_score_probability_keys_are_index_strings():
    with pytest.raises(sec.LayaApiError) as ei:
        check(q_score(), a_score(0.0, {0: 0.5, 1: 0.5}))      # int keys
    assert ei.value.invariant == "probability_keys"


def test_legend_and_confidence_shape():
    a = a_score(0.25, {"0": 0.75, "1": 0.25})
    a["legend"] = {"0": "trivial", "1": "expert"}
    check(q_score(), a)
    a["legend"] = {"0": 1}
    with pytest.raises(sec.LayaApiError) as ei:
        check(q_score(), a)
    assert ei.value.invariant == "legend_shape"
    a3 = a_choice("a", {"a": 0.7, "b": 0.3})
    a3["confidence"] = "high"
    with pytest.raises(sec.LayaApiError) as ei:
        check(q_choice(), a3)
    assert ei.value.invariant == "confidence_number"


def test_answer_not_object_and_bool_rejected_as_number():
    with pytest.raises(sec.LayaApiError) as ei:
        check(q_noul(), "yes")
    assert ei.value.invariant == "answer_shape"
    with pytest.raises(sec.LayaApiError) as ei:
        check(q_noul(), {"type": "noul", "noul": True})   # bool is not f64
    assert ei.value.invariant == "noul_range"


# ------------------------------------------------------ full envelope ------

def test_validate_answers_matching_and_missing_or_extra():
    qs = {"a": q_noul("a"), "b": q_choice("b")}
    ans = {"a": a_noul(0.2), "b": a_choice("a", {"a": 0.6, "b": 0.4})}
    assert sec.validate_answers(qs, ans) is ans
    with pytest.raises(sec.LayaApiError) as ei:
        sec.validate_answers(qs, {"a": a_noul(0.2)})
    assert ei.value.invariant == "missing_answer"
    with pytest.raises(sec.LayaApiError) as ei:
        sec.validate_answers(qs, {**ans, "z": a_noul(0.5)})
    assert ei.value.invariant == "unexpected_answer"


# ------------------------------------------------------ response cap -------

def test_response_size_cap():
    small = {"answers": {"q": a_noul(0.5)}, "usage": {"requests": 1}}
    assert sec.check_response_size(small) < sec.MAX_RESPONSE_BYTES
    huge = {"answers": {"q": a_noul(0.5)}, "pad": "x" * 1_000_001}
    with pytest.raises(sec.LayaApiError) as ei:
        sec.check_response_size(huge)
    assert ei.value.code == "response_too_large"


def test_check_auth_uses_the_resolver_alias():
    # D6: auth reads TOKEN through naming.env_alias (LAYA_ -> JEV_ ->
    # DASHBOARD_), not a raw os.environ lookup.
    naming.os.environ["LAYA_TOKEN"] = "real"
    assert sec.check_auth("Bearer real") is True
    with pytest.raises(sec.LayaApiError):
        sec.check_auth("Bearer unreal")
