"""Unit tests for server/laya_ops.py — the wire⇄engine mappers (plan
T1.2/T1.3/T1.4). No engine, no I/O: engine_out dicts are hand-built with the
shape engine.decide returns ({task, chain, choice, confidence, acted,
act_p, probs, latency_ms}). Every assertion is the CONSUMER's check from
spec §1 (EPS_MASS / ARGMAX_TOLERANCE / score band / Σ(i·p)=score), imported
from the module itself so a tolerance drift can't quietly weaken the tests.
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from server import laya_ops as ops


def eng(probs, choice=None):
    """Hand-built engine_out (engine.decide's result shape; probs rounded to
    4 decimals exactly like the engine does)."""
    rounded = {k: round(v, 4) for k, v in probs.items()}
    top = choice if choice is not None else max(rounded, key=lambda k: rounded[k])
    return {"task": "t", "chain": "base", "choice": top,
            "confidence": rounded[top], "acted": True, "act_p": 0.9,
            "probs": rounded, "latency_ms": 16.2}


def check_choice(q, ans):
    """The consumer's choice-answer validation (spec §1), verbatim."""
    crit = list(q["criteria"])
    p = ans["probabilities"]
    assert set(p) == set(crit)                      # keys == options exactly
    assert abs(sum(p.values()) - 1.0) <= ops.EPS_MASS
    best = max(p.values())
    assert abs(p[ans["choice"]] - best) <= ops.ARGMAX_TOLERANCE
    assert abs(ans["confidence"] - p[ans["choice"]]) < 1e-15
    assert all(isinstance(v, float) and 0.0 <= v <= 1.0 for v in p.values())


def check_score(q, ans):
    L = len(q["criteria"])
    p = ans["probabilities"]
    assert set(p) == {str(i) for i in range(L)}
    assert abs(sum(p.values()) - 1.0) <= ops.EPS_MASS
    assert ans["spread_reported"] is True
    assert -0.5 <= ans["score"] <= L - 0.5          # client band
    dot = sum(i * p[str(i)] for i in range(L))
    assert abs(dot - ans["score"]) <= ops.SCORE_DOT_TOLERANCE
    assert isinstance(ans["confidence"], float)


# ---------------------------------------------------------------- noul

def test_noul_happy_path():
    q = {"type": "noul", "instructions": "Is this a costly mistake?"}
    ans = ops.answer_from_noul("cm", q, eng({"yes": 0.73, "no": 0.27}))
    assert ans == {"type": "noul", "noul": 0.73}


def test_noul_rejects_criteria():
    q = {"type": "noul", "instructions": "pick one",
         "criteria": {"a": "one", "b": "two"}}
    with pytest.raises(ValueError):
        ops.answer_from_noul("x", q, eng({"yes": 0.5, "no": 0.5}))


def test_noul_missing_yes_prob():
    q = {"type": "noul", "instructions": "sure?"}
    with pytest.raises(ValueError):
        ops.answer_from_noul("x", q, {"probs": {"no": 1.0}})


# ---------------------------------------------------------------- choice

def test_choice_key_set_equality_and_mass():
    q = {"type": "choice", "instructions": "which tool?",
         "criteria": {"terminal": "run shell", "read": "read file",
                      "write": "write file"}}
    ans = ops.answer_from_choice("tool", q, eng({"terminal": 0.1,
                                                 "read": 0.7, "write": 0.2}))
    check_choice(q, ans)
    assert ans["choice"] == "read"
    assert ans["type"] == "choice"
    # our own tighter pin, not just the consumer's loose EPS_MASS
    assert abs(sum(ans["probabilities"].values()) - 1.0) <= 1e-12


def test_choice_tie_breaks_first_in_criteria_order():
    q = {"type": "choice", "instructions": "coin",
         "criteria": {"heads": "h", "tails": "t"}}
    ans = ops.answer_from_choice("c", q, eng({"heads": 0.5, "tails": 0.5}))
    check_choice(q, ans)
    assert ans["choice"] == "heads"  # first-in-criteria order


def test_choice_32_option_slate():
    keys = [f"t{i:02d}" for i in range(32)]
    q = {"type": "choice", "instructions": "32 tools",
         "criteria": {k: f"tool {k}" for k in keys}}
    raw = {k: (i + 1) / 100.0 for i, k in enumerate(keys)}  # in [0,1], mass ≠ 1 on purpose
    ans = ops.answer_from_choice("t", q, {"probs": raw, "choice": keys[-1]})
    check_choice(q, ans)
    assert ans["choice"] == "t31"


def test_choice_120_option_slate():
    # pick-skill stage-1 shape; engine's 4-decimal rounding makes the raw sum
    # drift ~1e-3 here — the mass fix must hold Σp = 1 ± EPS_MASS anyway.
    keys = [f"S{i}" for i in range(120)]
    q = {"type": "choice", "instructions": "120 skills",
         "criteria": {k: f"skill {k}" for k in keys}}
    raw = {k: 1.0 / 120 for k in keys}
    assert abs(sum(round(v, 4) for v in raw.values()) - 1.0) > 1e-6  # real drift
    ans = ops.answer_from_choice("skill", q, eng(raw))
    check_choice(q, ans)
    assert abs(sum(ans["probabilities"].values()) - 1.0) <= 1e-12


def test_choice_refuses_wrong_qtype():
    q = {"type": "score", "instructions": "wrong type",
         "criteria": ["a", "b"]}
    with pytest.raises(ValueError):
        ops.answer_from_choice("x", q, eng({"a": 0.5, "b": 0.5}))


def test_choice_refuses_single_option():
    q = {"type": "choice", "instructions": "one option",
         "criteria": {"only": "just one"}}
    with pytest.raises(ValueError):
        ops.answer_from_choice("x", q, eng({"only": 1.0}))


# ---------------------------------------------------------------- score

RUBRIC4 = ["Trivial", "Short", "Medium", "Hard"]
RUBRIC5 = ["none", "low", "medium", "high", "right-now"]


def test_score_L4():
    q = {"type": "score", "instructions": "difficulty?", "criteria": RUBRIC4}
    ans = ops.answer_from_score("difficulty", q,
                                eng({"0": 0.1, "1": 0.2, "2": 0.5, "3": 0.2}))
    check_score(q, ans)
    assert ans["score"] == pytest.approx(0 * .1 + 1 * .2 + 2 * .5 + 3 * .2)
    assert ans["confidence"] == 0.5  # top-level prob, not the client's 1.0 default
    assert "legend" not in ans


def test_score_L5():
    q = {"type": "score", "instructions": "urgency?", "criteria": RUBRIC5}
    ans = ops.answer_from_score("urgency", q,
                                eng({"0": 0.05, "1": 0.15, "2": 0.30,
                                     "3": 0.35, "4": 0.15}))
    check_score(q, ans)
    assert ans["spread_reported"] is True


def test_score_dot_matches_score_tightly():
    # consumer checks Σ(i·p) == score ± 0.02; ours matches to float noise
    q = {"type": "score", "instructions": "difficulty?", "criteria": RUBRIC4}
    ans = ops.answer_from_score("d", q, eng({"0": 0.25, "1": 0.25,
                                             "2": 0.25, "3": 0.25}))
    dot = sum(i * ans["probabilities"][str(i)] for i in range(4))
    assert abs(dot - ans["score"]) <= 1e-12
    assert abs(sum(ans["probabilities"].values()) - 1.0) <= 1e-12
    assert -0.5 <= ans["score"] <= 4 - 0.5


def test_score_legend_on_and_off():
    q_off = {"type": "score", "instructions": "difficulty?",
             "criteria": RUBRIC4}
    q_on = dict(q_off, legend=True)
    e = eng({"0": 0.7, "1": 0.1, "2": 0.1, "3": 0.1})
    assert "legend" not in ops.answer_from_score("d", q_off, e)
    on = ops.answer_from_score("d", q_on, e)
    assert on["legend"] == {"0": "Trivial", "1": "Short",
                            "2": "Medium", "3": "Hard"}
    check_score(q_on, on)


def test_score_refuses_wrong_qtype_and_short_rubric():
    e = eng({"0": 0.5, "1": 0.5})
    with pytest.raises(ValueError):  # choice question into the score mapper
        ops.answer_from_score("x", {"type": "choice", "instructions": "no",
                                    "criteria": {"0": "a", "1": "b"}}, e)
    with pytest.raises(ValueError):  # rubric with 1 level
        ops.answer_from_score("x", {"type": "score", "instructions": "no",
                                    "criteria": ["only"]}, e)


# ------------------------------------------------- round-trip property test

def test_random_probability_vectors_invariants():
    """200 random normalized vectors through both mappers; every consumer
    invariant from spec §1 must hold for every one."""
    rng = random.Random(20260925)  # fixed seed: reproducible, not flaky
    for trial in range(200):
        n = rng.randint(2, 40)
        raw = [rng.random() for _ in range(n)]
        s = sum(raw)
        probs = {f"k{i}": v / s for i, v in enumerate(raw)}
        assert abs(sum(probs.values()) - 1.0) <= 1e-12  # input sane

        q = {"type": "choice", "instructions": f"pick {trial}",
             "criteria": {k: f"meaning {k}" for k in probs}}
        check_choice(q, ops.answer_from_choice("q", q, eng(probs)))

        L = n
        keys = [str(i) for i in range(L)]
        qs = {"type": "score", "instructions": f"rate {trial}",
              "criteria": [f"level {i}" for i in range(L)]}
        e = eng({k: probs[f"k{i}"] for i, k in enumerate(keys)})
        check_score(qs, ops.answer_from_score("s", qs, e))


# ------------------------------------------------------------- refuse paths

def test_refuses_empty_instructions():
    for qtype, q in [("noul", {"type": "noul", "instructions": "  "}),
                     ("choice", {"type": "choice", "instructions": "",
                                 "criteria": {"a": "a", "b": "b"}}),
                     ("score", {"type": "score", "instructions": "",
                                "criteria": ["a", "b"]})]:
        with pytest.raises(ValueError):
            getattr(ops, f"answer_from_{qtype}")("x", q,
                                                 eng({"a": 0.5, "b": 0.5}))


def test_refuses_unknown_qtype():
    for fn in (ops.answer_from_noul, ops.answer_from_choice,
               ops.answer_from_score):
        with pytest.raises(ValueError):
            fn("x", {"type": "ranking", "instructions": "what"},
               eng({"a": 0.5, "b": 0.5}))


def test_refuses_missing_probs():
    q = {"type": "noul", "instructions": "sure?"}
    for bad in ({}, {"probs": {}}, {"probs": None}, {"probs": [1.0]}, None):
        with pytest.raises(ValueError):
            ops.answer_from_noul("x", q, bad)
    qc = {"type": "choice", "instructions": "pick",
          "criteria": {"a": "a", "b": "b"}}
    with pytest.raises(ValueError):
        ops.answer_from_choice("x", qc, {})


def test_engine_probs_missing_key_raises():
    q = {"type": "choice", "instructions": "pick",
         "criteria": {"a": "a", "b": "b", "c": "c"}}
    with pytest.raises(ValueError):  # engine_out lacks "c"
        ops.answer_from_choice("x", q, eng({"a": 0.5, "b": 0.5}))
