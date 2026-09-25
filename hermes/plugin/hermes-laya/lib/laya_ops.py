"""Wire ⇄ engine mapping layer for the Jev-compatible question API.

Contract: .hermes/plans/2026-09-25-jev-parity-api-spec-v2.md §1 "Wire
protocol" — THE authority; §9 states the acceptance rule (every answer must
survive the consumer's validation invariants — interop is the test). Maps
wire questions (noul|choice|score; instructions/criteria) plus the internal
Engine result ({task, chain, choice, confidence, acted, act_p, probs,
latency_ms}) to wire answers. Pure functions, stdlib only, nothing here can
hang.

Engine semantics relied on (core/laya_port/combined_agent.py):
  - a criteria-less wire `noul` question is encoded internally as the
    2-criterion choice over ["yes", "no"] (yes index 1, no index 0), so the
    wire p_yes is engine_out["probs"]["yes"];
  - an L-level `score` rubric is served as an L-option `choice` over the
    level names "0".."L-1" (callers pass chain="base");
  - engine probs sum to 1, choice = argmax, confidence = top prob.
"""

# Consumer tolerances (spec §1) — stated ONCE as module constants so a
# tolerance change can't silently desync the three mappers (DRY).
EPS_MASS = 0.01 + 1e-12               # consumer: |Σp − 1| ≤ EPS_MASS
ARGMAX_TOLERANCE = 1e-9               # consumer: |p[choice] − max(p)| ≤ this
SCORE_DOT_TOLERANCE = 0.02 + 1e-12    # consumer: |Σ(i·p) − score| when spread_reported
SCORE_BAND_LO = -0.5                  # consumer band: score ∈ [−0.5, L − 0.5]
SCORE_BAND_HI = -0.5                  # upper = L + this

# Our emission rules (tighter than the consumer's so its checks are never
# near-misses): every probability is round(p, ROUND_DECIMALS) and the set is
# pinned to Σ = 1 ± MASS_SLACK; the mass residual is spread so NO single key
# moves more than RESIDUAL_MAX (a real argmax gap can't be crossed; a tie
# within that is inside the consumer's ARGMAX_TOLERANCE anyway).
ROUND_DECIMALS = 10
ROUND_UNIT = 10.0 ** -ROUND_DECIMALS  # quantum of one adjustment step
MASS_SLACK = 1e-12
RESIDUAL_MAX = 1e-9


def _check_question(qtype, qname, q):
    """Shared validation (spec §1 check_question): object shape, expected
    type, non-empty instructions ≠ the question's own name. Field rules that
    differ per type (noul forbids criteria; choice/score require them) are
    enforced per-mapper."""
    if not isinstance(q, dict):
        raise ValueError(f"question {qname!r}: must be an object")
    if q.get("type") != qtype:
        raise ValueError(f"question {qname!r}: type {q.get('type')!r} "
                         f"not accepted by the {qtype} mapper")
    ins = q.get("instructions")
    if not isinstance(ins, str) or not ins.strip():
        raise ValueError(f"question {qname!r}: instructions must be non-empty")
    if ins == qname:
        raise ValueError(f"question {qname!r}: instructions must differ "
                         f"from the question name")


def _probs_of(qname, engine_out):
    if not isinstance(engine_out, dict):
        raise ValueError(f"question {qname!r}: engine_out must be an object")
    probs = engine_out.get("probs")
    if not isinstance(probs, dict) or not probs:
        raise ValueError(f"question {qname!r}: engine_out missing probs")
    return probs


def _renormalize(qname, keys, raw):
    """{key: f64} with keys == criteria keys exactly, Σ == 1 ± MASS_SLACK.
    Round to ROUND_DECIMALS first (wire precision), then spread the rounding
    residual evenly over every key with headroom: |Σerr| ≤ len(keys)·
    ROUND_UNIT/2 (each round loses ≤ half a quantum — engine.decide rounds
    probs to 4 decimals, so on a 120-option slate the residual reaches ~1e-3;
    that is why it may NOT land on one key: a >RESIDUAL_MAX jump could move
    argmax. Evenly spread, each key moves ≤ ROUND_UNIT/2·(share) ≤
    RESIDUAL_MAX). Keys pinned at 0 or 1 have no headroom and are skipped;
    the sum then converges in one pass (leftover < a few ulps lands on the
    top key, ≤ MASS_SLACK-sized)."""
    try:
        vals = {k: round(min(1.0, max(0.0, float(raw[k]))), ROUND_DECIMALS)
                for k in keys}
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"question {qname!r}: engine_out probs missing "
                         f"numeric entries for the question's keys") from None
    # engine.decide rounds probs to 4 decimals, so a real engine_out's mass
    # can sit ~5e-5 off 1: divide by the actual sum (order-preserving, so
    # argmax can't flip), then re-round to wire precision.
    mass = sum(vals.values())
    if mass <= 0.0:
        raise ValueError(f"question {qname!r}: engine_out probs have no "
                         f"mass to renormalize")
    if abs(mass - 1.0) > MASS_SLACK:
        vals = {k: round(v / mass, ROUND_DECIMALS) for k, v in vals.items()}
    for _ in range(8):  # one pass usually converges; loop guards degenerate slates
        err = 1.0 - sum(vals.values())
        if abs(err) <= MASS_SLACK:
            break
        movable = [k for k in keys
                   if (vals[k] < 1.0 if err > 0 else vals[k] > 0.0)]
        if not movable:
            raise ValueError(f"question {qname!r}: probabilities cannot be "
                             f"renormalized to sum 1")
        step = err / len(movable)
        if abs(step) > RESIDUAL_MAX:
            raise ValueError(f"question {qname!r}: engine_out mass too far "
                             f"from 1 to fix without moving argmax "
                             f"(residual {err!r})")
        for k in movable:
            vals[k] += step  # add < 1 quantum; still inside [0,1] (see docstring)
    else:
        raise ValueError(f"question {qname!r}: probabilities cannot be "
                         f"renormalized to sum 1 within {MASS_SLACK}")
    leftover = 1.0 - sum(vals.values())
    if abs(leftover) > MASS_SLACK:
        raise ValueError(f"question {qname!r}: probabilities cannot be "
                         f"renormalized to sum 1 within {MASS_SLACK}")
    if any(v < 0.0 or v > 1.0 for v in vals.values()):  # re-clamp assertion
        raise ValueError(f"question {qname!r}: renormalization left probs "
                         f"outside [0,1]")
    if abs(leftover) > 0.0:  # exact pin (≤ MASS_SLACK, cannot move argmax)
        vals[max(keys, key=lambda k: vals[k])] += leftover
    return vals


def _argmax(keys, vals):
    """First key with the max value — ties broken by criteria insertion order,
    the same rule the engine's argmax uses. Applied to the FINAL reported
    probabilities, so the consumer's `choice == argmax(probabilities)` check
    holds exactly (gap 0, inside ARGMAX_TOLERANCE by construction)."""
    return max(keys, key=lambda k: vals[k])


def answer_from_noul(qname, q, engine_out):
    """Criteria-less noul question → {"type":"noul","noul":p_yes}. noul==1
    means "yes": internally the question is the 2-criterion choice
    ["yes","no"] (yes index 1), so p_yes is engine_out["probs"]["yes"].
    Spec §1: a noul question carrying criteria → ValueError."""
    _check_question("noul", qname, q)
    if q.get("criteria"):
        raise ValueError(f"question {qname!r}: noul questions must have "
                         f"no criteria")
    probs = _probs_of(qname, engine_out)
    if "yes" not in probs:
        raise ValueError(f"question {qname!r}: engine_out probs missing 'yes'")
    return {"type": "noul", "noul": min(1.0, max(0.0, float(probs["yes"])))}


def answer_from_choice(qname, q, engine_out):
    """Choice question → {"type":"choice","choice":str,
    "probabilities":{opt:f64},"confidence":f64}. Invariants held BY
    CONSTRUCTION (spec §1): probability keys == criteria keys exactly;
    Σp = 1 ± MASS_SLACK after rounding (inside EPS_MASS); choice ==
    argmax(probabilities) — ties → first-in-criteria order; confidence ==
    that max prob (hence the consumer's `confidence == p[choice]` check)."""
    _check_question("choice", qname, q)
    crit = q.get("criteria")
    if not isinstance(crit, dict) or len(crit) < 2:
        raise ValueError(f"question {qname!r}: choice questions need a "
                         f"criteria mapping with ≥ 2 options")
    keys = list(crit)  # insertion order == the consumer's option order
    probs = _renormalize(qname, keys, _probs_of(qname, engine_out))
    top = _argmax(keys, probs)
    return {"type": "choice", "choice": top, "probabilities": probs,
            "confidence": probs[top]}


def answer_from_score(qname, q, engine_out):
    """Score question → {"type":"score","score":f64,"probabilities":
    {"<idx>":f64},"spread_reported":true,"confidence":f64,"legend":{...}?}.
    Rubric = criteria list, lowest-first, L ≥ 2; the engine serves it as a
    multi-class choice over level names "0".."L-1", so
    score = Σ level·p(level) marginalizes the distribution (spec §9). score
    is computed FROM the same renormalized probabilities the answer reports,
    so the consumer's paired invariants Σp = 1 ± EPS_MASS and
    Σ(i·p) = score ± SCORE_DOT_TOLERANCE hold together;
    score ∈ [0, L−1] ⊂ the client's [−0.5, L−0.5] band. Documented deviation:
    their client defaults confidence to 1.0 when absent; we always report the
    top-level prob — the spread is the real signal and an inflated default
    hides it."""
    _check_question("score", qname, q)
    crit = q.get("criteria")
    if not isinstance(crit, list) or len(crit) < 2:
        raise ValueError(f"question {qname!r}: score questions need a "
                         f"criteria rubric list with ≥ 2 levels")
    L = len(crit)
    keys = [str(i) for i in range(L)]
    probs = _renormalize(qname, keys, _probs_of(qname, engine_out))
    top = _argmax(keys, probs)
    score = sum(i * probs[str(i)] for i in range(L))
    if not (SCORE_BAND_LO <= score <= L + SCORE_BAND_HI):
        # unreachable for valid probs; guard against silent drift in the mapper
        raise ValueError(f"question {qname!r}: score {score!r} outside the "
                         f"consumer band [{SCORE_BAND_LO}, "
                         f"{L + SCORE_BAND_HI}]")
    ans = {"type": "score", "score": score, "probabilities": probs,
           "spread_reported": True, "confidence": probs[top]}
    if q.get("legend"):  # legend is optional on the wire (spec §1)
        ans["legend"] = {k: crit[i] for i, k in enumerate(keys)}
    return ans
