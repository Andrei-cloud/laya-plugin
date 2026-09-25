"""Security, auth, validation core + error vocabulary for the Laya question
API router (spec-v2 §1/§8; plan T1.1/T1.6 support).

Self-contained and importable on its own (no Engine, no HTTP framework):
the router (`server/laya_api_http.py`, T1.1) calls into these functions;
tests drive them directly. Stdlib only.

Responsibilities:
  * the error vocabulary (``LayaApiError.code`` mirrors the client's
    ``JevError.code`` set + ``_RETRYABLE``),
  * Bearer auth (optional ``LAYA_TOKEN`` via the ONE alias resolver —
    server does NOT validate key content; loopback trust),
  * request-envelope validation (state cap, question shapes, the three
    wire types ``noul|choice|score`` with their criteria rules),
  * answer-invariant validation (what the caller refuses to act on:
    contradiction -> ``invalid_response`` + invariant, fail open),
  * response size cap.

SECURITY RULES (binding): secret values (API key, token) are NEVER logged,
never echoed in error bodies, never compared with ``==`` on raw bytes —
``hmac.compare_digest`` only. Env reads go through ``naming.env_alias``
(D6: nothing else reads env for the Laya surface).

Wire names are NOT ours to rename (spec-v2 §8): question types and
envelope/answer fields stay exactly Jev-compat so their client validates
our replies unchanged.
"""
import hmac
import json

try:  # normal package import
    from . import naming
except ImportError:  # imported as a top-level module (self-contained use)
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import naming

# --------------------------------------------------------------------------
# error vocabulary (JevError.code mirror, spec-v2 §1)
# --------------------------------------------------------------------------

QUESTION_TYPES = ("choice", "score", "noul")  # wire literals, never renamed

MAX_STATE_CHARS = 60_000        # JSON-encoded state cap
MAX_RESPONSE_BYTES = 1_000_000  # response envelope cap

# Tolerances the caller's validators enforce — we satisfy them by construction.
SIGMA_TOL = 0.01 + 1e-12        # probability mass around 1
ARGMAX_TOL = 1e-9               # choice == argmax(probabilities)
SCORE_MATCH_TOL = 0.02 + 1e-12  # sum(level*p) == score when spread_reported

# Client-side codes (what a caller of OUR service may observe / mirror):
ERROR_CODES = frozenset({
    "no_key", "state_too_large", "timeout", "network", "malformed",
    "invalid_response", "response_too_large", "auth_failed",
    "credits_exhausted", "rate_limited", "overloaded",
})

# http_<status> family is open-ended; these are the retried ones (spec §1):
RETRYABLE = frozenset({"rate_limited", "overloaded", "network",
                       "http_500", "http_502", "http_503", "http_504"})

# HTTP status per code (how the router must answer; 3xx never followed).
HTTP_STATUS = {
    "no_key": 401,
    "auth_failed": 401,           # client also maps 403 to auth_failed
    "credits_exhausted": 402,
    "rate_limited": 429,
    "overloaded": 529,
    "state_too_large": 400,
    "invalid_response": 400,
    "response_too_large": 500,
    "malformed": 400,
    "timeout": 504,
}


def is_retryable(code):
    """True for the client's retry set (rate_limited/overloaded/network/5xx)."""
    return code in RETRYABLE


def http_status(code):
    """HTTP status for a code; unknown ``http_<n>`` codes carry their own."""
    if code in HTTP_STATUS:
        return HTTP_STATUS[code]
    if code.startswith("http_"):
        try:
            return int(code[5:])
        except ValueError:
            pass
    return 400


class LayaApiError(Exception):
    """One error vocabulary for the whole question API. ``code`` is a
    JevError-compatible code (``invalid_response`` always names the broken
    ``invariant``); server-side envelope refusals use descriptive codes the
    caller's client maps to its own ``malformed`` raise -> fail-open path
    (the intended degraded behavior, spec-v2 §1)."""

    def __init__(self, code, detail=None, invariant=None):
        self.code = code
        self.detail = detail
        self.invariant = invariant
        super().__init__(code if not invariant else f"{code}: {invariant}")

    def to_response(self):
        """JSON-able body. Refusals per spec-v2 T1.1:
        ``{"status": "invalid_request", "error": ...}`` — never any secret,
        never prompt text."""
        body = {"status": "invalid_request", "error": self.code}
        if self.invariant:
            body["invariant"] = self.invariant
        return body

    @property
    def http_status(self):
        return http_status(self.code)


# --------------------------------------------------------------------------
# auth (optional LAYA_TOKEN via the ONE resolver; key content never validated
# nor logged — loopback trust per D6 table)
# --------------------------------------------------------------------------

def check_auth(authorization=None, expected=None):
    """Validate the ``Authorization: Bearer *** header against the
    TOKEN alias (LAYA_TOKEN -> JEV_TOKEN -> DASHBOARD_TOKEN). When no token
    is configured, any request passes (loopback trust; the server does NOT
    validate key content). Raises LayaApiError('auth_failed') on mismatch —
    the raised error carries NO token material in code, detail, or body."""
    if expected is None:
        expected = naming.env_alias("TOKEN")
    if not expected:
        return True  # no token configured: loopback trust
    if not isinstance(authorization, str):
        raise LayaApiError("auth_failed", "missing Authorization header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise LayaApiError("auth_failed", "Authorization header must be Bearer")
    supplied = parts[1].strip()
    # constant-time compare; NEVER ==, NEVER logged
    if not hmac.compare_digest(supplied.encode(), str(expected).encode()):
        raise LayaApiError("auth_failed", "bearer token mismatch")
    return True


# --------------------------------------------------------------------------
# request-envelope validation (spec-v2 §1 + T1.1)
# --------------------------------------------------------------------------

def _json_char_len(value):
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        raise LayaApiError("malformed", "state is not JSON-encodable")


def validate_question(name, q):
    """Per-question checks; raises LayaApiError with a descriptive code the
    caller's client surfaces as ``malformed``. Rules (spec-v2 §1):
    type ∈ noul|choice|score; noul carries NO criteria; choice criteria is a
    mapping ≥2 options; score criteria is an ordered list ≥2 levels
    (lowest first — semantics, not machine-checkable); instructions
    non-empty and ≠ its own question name; CLI-only aliases ``kind``→
    ``type`` / ``text``→``instructions`` are REFUSED on the wire (accepted
    only at our CLI)."""
    if not isinstance(name, str) or not name:
        raise LayaApiError("invalid_question_name", "question name must be a non-empty string")
    if not isinstance(q, dict):
        raise LayaApiError("question_not_object", f"question {name!r} must be an object")
    if "kind" in q or "text" in q:
        raise LayaApiError("cli_alias_on_http",
                           f"question {name!r}: kind/text aliases are CLI-only, "
                           "the wire requires type/instructions")
    qtype = q.get("type")
    if qtype not in QUESTION_TYPES:
        raise LayaApiError("unknown_question_type",
                           f"question {name!r}: type {qtype!r} not in {QUESTION_TYPES}")
    instructions = q.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise LayaApiError("instructions_required",
                           f"question {name!r}: instructions must be a non-empty string")
    if instructions == name:
        raise LayaApiError("instructions_equals_name",
                           f"question {name!r}: instructions must differ from its name")
    criteria = q.get("criteria")
    if qtype == "noul":
        if criteria not in (None, {}, []):
            raise LayaApiError("noul_criteria_forbidden",
                               f"question {name!r}: noul must not carry criteria")
    elif qtype == "choice":
        if not isinstance(criteria, dict) or len(criteria) < 2:
            raise LayaApiError("choice_needs_two_options",
                               f"question {name!r}: choice needs a criteria mapping with >=2 options")
        if not all(isinstance(k, str) and k for k in criteria):
            raise LayaApiError("choice_option_names_invalid",
                               f"question {name!r}: choice option names must be non-empty strings")
    elif qtype == "score":
        if not isinstance(criteria, list) or len(criteria) < 2:
            raise LayaApiError("score_needs_two_levels",
                               f"question {name!r}: score needs an ordered criteria list with >=2 levels")
        if not all(isinstance(level, str) and level for level in criteria):
            raise LayaApiError("score_levels_invalid",
                               f"question {name!r}: score levels must be non-empty strings")
    return q


def validate_request(payload):
    """Validate + normalize one question-API request body. Returns
    ``{"state", "state_chars", "model", "questions"}`` with ``model`` set
    to the value to echo back VERBATIM (naming.model_id: strict clients
    compare the echo). Raises LayaApiError on any refusal — the router
    turns it into ``{status: \"invalid_request\", error}`` (their client
    then raises ``malformed`` and fails open: the intended degraded path)."""
    if not isinstance(payload, dict):
        raise LayaApiError("envelope_not_object", "request body must be a JSON object")
    # --- state: string | object | array, <= 60 000 JSON-encoded chars ---
    if "state" not in payload:
        raise LayaApiError("state_required", "state is required")
    state = payload["state"]
    if not isinstance(state, (str, dict, list)):
        raise LayaApiError("state_type", "state must be a string, object, or array")
    state_chars = _json_char_len(state)
    if state_chars > MAX_STATE_CHARS:
        # same code the client raises pre-send (state_too_large)
        raise LayaApiError("state_too_large",
                           f"state is {state_chars} JSON chars (cap {MAX_STATE_CHARS})")
    # --- model: accepted/echoed verbatim ---
    requested_model = payload.get("model")
    if requested_model is not None and not isinstance(requested_model, str):
        raise LayaApiError("model_type", "model must be a string when present")
    model = naming.model_id(requested_model)
    # --- questions: mapping with >=1 named question ---
    questions = payload.get("questions")
    if not isinstance(questions, dict) or len(questions) < 1:
        raise LayaApiError("questions_required",
                           "questions must be a mapping with at least one entry")
    for name, q in questions.items():
        validate_question(name, q)
    return {"state": state, "state_chars": state_chars,
            "model": model, "questions": questions}


# --------------------------------------------------------------------------
# answer-invariant validation (spec-v2 §1: contradiction -> invalid_response
# + invariant; the caller fails open and NEVER acts on it)
# --------------------------------------------------------------------------

def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _require_number(obj, key, invariant):
    v = obj.get(key)
    if not _is_number(v):
        raise LayaApiError("invalid_response",
                           f"{key} must be a number", invariant=invariant)
    return float(v)


def validate_answer(name, question, answer):
    """Validate one answer against its question; raises
    LayaApiError('invalid_response', invariant=...) on contradiction.
    Invariant names mirror the caller's validators (probability_keys,
    probability_mass, argmax, score_band, score_matches_its_distribution)."""
    if not isinstance(answer, dict):
        raise LayaApiError("invalid_response", f"answer {name!r} must be an object",
                           invariant="answer_shape")
    qtype = question["type"]
    if answer.get("type") != qtype:
        raise LayaApiError("invalid_response",
                           f"answer {name!r}: type {answer.get('type')!r} != question type {qtype!r}",
                           invariant="answer_type_mismatch")
    if qtype == "noul":
        p = _require_number(answer, "noul", "noul_range")
        if not (0.0 <= p <= 1.0):
            raise LayaApiError("invalid_response",
                               f"answer {name!r}: noul {p} outside [0,1]",
                               invariant="noul_range")
    elif qtype == "choice":
        options = set(question["criteria"])
        probs = answer.get("probabilities")
        if not isinstance(probs, dict) or set(probs) != options:
            raise LayaApiError("invalid_response",
                               f"answer {name!r}: probability keys must equal the options exactly",
                               invariant="probability_keys")
        if not all(_is_number(v) for v in probs.values()):
            raise LayaApiError("invalid_response",
                               f"answer {name!r}: probabilities must all be numbers",
                               invariant="probability_numbers")
        total = sum(probs.values())
        if abs(total - 1.0) > SIGMA_TOL:
            raise LayaApiError("invalid_response",
                               f"answer {name!r}: Σp={total} != 1 ± {SIGMA_TOL}",
                               invariant="probability_mass")
        chosen = answer.get("choice")
        if chosen not in options:
            raise LayaApiError("invalid_response",
                               f"answer {name!r}: choice {chosen!r} is not an option",
                               invariant="argmax")
        best = max(probs.values())
        if probs[chosen] < best - ARGMAX_TOL:
            raise LayaApiError("invalid_response",
                               f"answer {name!r}: choice {chosen!r} is not the argmax",
                               invariant="argmax")
        _require_number(answer, "confidence", "confidence_number")
    elif qtype == "score":
        levels = question["criteria"]
        L = len(levels)
        score = _require_number(answer, "score", "score_band")
        if not (-0.5 <= score <= L - 0.5):
            raise LayaApiError("invalid_response",
                               f"answer {name!r}: score {score} outside [-0.5, {L - 0.5}]",
                               invariant="score_band")
        probs = answer.get("probabilities")
        if not isinstance(probs, dict) \
                or set(probs) != {str(i) for i in range(L)}:
            raise LayaApiError("invalid_response",
                               f"answer {name!r}: probability keys must be \"0\"..\"{L - 1}\"",
                               invariant="probability_keys")
        if not all(_is_number(v) for v in probs.values()):
            raise LayaApiError("invalid_response",
                               f"answer {name!r}: probabilities must all be numbers",
                               invariant="probability_numbers")
        if answer.get("spread_reported") is True:
            # the spread is the real signal: mass + expectation must agree
            # (their score_distribution_mass + score_matches_its_distribution)
            total = sum(probs.values())
            if abs(total - 1.0) > SIGMA_TOL:
                raise LayaApiError("invalid_response",
                                   f"answer {name!r}: Σp={total} != 1 ± {SIGMA_TOL}",
                                   invariant="probability_mass")
            expectation = sum(int(i) * p for i, p in probs.items())
            if abs(expectation - score) > SCORE_MATCH_TOL:
                raise LayaApiError("invalid_response",
                                   f"answer {name!r}: Σ(level·p)={expectation} != score {score} "
                                   f"± {SCORE_MATCH_TOL}",
                                   invariant="score_matches_its_distribution")
        # confidence defaults to 1.0 when absent (their client's rule)
        if "confidence" in answer and not _is_number(answer["confidence"]):
            raise LayaApiError("invalid_response",
                               f"answer {name!r}: confidence must be a number",
                               invariant="confidence_number")
        legend = answer.get("legend")
        if legend is not None and (not isinstance(legend, dict)
                                   or not all(isinstance(k, str) and isinstance(v, str)
                                              for k, v in legend.items())):
            raise LayaApiError("invalid_response",
                               f"answer {name!r}: legend must be an idx->str mapping",
                               invariant="legend_shape")
    return answer


def validate_answers(questions, answers):
    """Full-envelope check: every question has exactly one answer, each
    validated against its question."""
    if not isinstance(answers, dict):
        raise LayaApiError("invalid_response", "answers must be a mapping",
                           invariant="answers_shape")
    missing = set(questions) - set(answers)
    extra = set(answers) - set(questions)
    if missing:
        raise LayaApiError("invalid_response",
                           f"missing answers for {sorted(missing)}",
                           invariant="missing_answer")
    if extra:
        raise LayaApiError("invalid_response",
                           f"answers for unknown questions {sorted(extra)}",
                           invariant="unexpected_answer")
    for name in questions:
        validate_answer(name, questions[name], answers[name])
    return answers


# --------------------------------------------------------------------------
# response size cap
# --------------------------------------------------------------------------

def check_response_size(payload):
    """Raise response_too_large when the JSON response exceeds
    MAX_RESPONSE_BYTES (client raises the same code reading us)."""
    try:
        n = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as e:
        raise LayaApiError("malformed", f"response not JSON-encodable: {e}")
    if n > MAX_RESPONSE_BYTES:
        raise LayaApiError("response_too_large",
                           f"response is {n} bytes (cap {MAX_RESPONSE_BYTES})")
    return n
