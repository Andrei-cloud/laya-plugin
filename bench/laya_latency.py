#!/usr/bin/env python3
"""T1.9 — p50/p95 latency harness against the Laya HTTP bridge daemon.

Contract: laya/.hermes/plans/2026-09-25-jev-parity-api-spec-v2.md §1 (jev wire
shapes: noul/choice/score, many named questions per request, model echo) and
the published Jev bars (route 0.4 s, rerank round 1.9 s, choose 0.5 s).

Five shapes, each timed client-side in wall-clock ms via urllib.request
(stdlib only — no new deps):

  1. guardrail-choice-32   one choice question with a 32-option slate
                           (choose-style; bar: choose 0.5 s)
  2. rerank-noul-121       ONE request carrying 121 noul questions named
                           rel_0..rel_120 — proxy for a full 121-passage
                           rerank round (bar: rerank round 1.9 s)
  3. skillpick-choice-120  skill-style batch split into 4 requests of 30
                           choice questions each (bar: choose 0.5 s)
  4. route-score-4         score question, 4-level rubric, lowest first
                           (Trivial→Expert; bar: route 0.4 s)
  5. btzsc-choice-72       BTZSC-style choice over all 72 labels
                           (bar: choose 0.5 s)

Per shape: 1 warm call (untimed), then --repeats timed calls; report
p50/p95/min/max/count (nearest-rank percentiles) + shape meta + model echo
check; optional --bars JSON file {shape: seconds} adds per-shape under_bar
booleans. Writes JSON report (pretty, sorted keys) to --out and NOTHING
else. The protocol is the same one the jev client uses (POST
{state, model, questions} → {answers, usage, model?}); against a Laya
daemon the endpoint is the bridge base-url.

Live mode is exercised by the parent bench runner — do NOT start a daemon
here. Offline it is unit-tested via tests/test_laya_latency_stats.py.

Usage:
  python bench/laya_latency.py [--base-url http://127.0.0.1:11270]
      [--repeats 7] [--out bench/runs/laya_latency.json] [--bars bars.json]
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_BASE_URL = "http://127.0.0.1:11270"
DEFAULT_REPEATS = 7
DEFAULT_OUT = "bench/runs/laya_latency.json"
DEFAULT_MODEL = "laya-combined-f16"
HTTP_TIMEOUT_S = 30.0
REPORT_SCHEMA = "laya.latency.report.v1"
STATE_CHAR_CAP = 60_000  # §1: state ≤ 60,000 JSON-encoded chars
# The wire path (§1 interop constant — literal, not ours to rename).
QUESTION_API_PATH = "/v1/systemone"

# Published Jev bars (spec §5 acceptance numbers), in seconds.
DEFAULT_BARS = {
    "route": 0.4,
    "rerank-round": 1.9,
    "choose": 0.5,
}

# Shape name → bar key in the bars map.
SHAPE_BAR_KEYS = {
    "guardrail-choice-32": "choose",
    "rerank-noul-121": "rerank-round",
    "skillpick-choice-120": "choose",
    "route-score-4": "route",
    "btzsc-choice-72": "choose",
}

# Shared constants live in server/engine.py; bootstrap the import path but
# stay standalone if server/ isn't importable (harness must run from anywhere).
_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "server")


def _try_shared_constants():
    try:
        if _SERVER_DIR not in sys.path:
            sys.path.insert(0, _SERVER_DIR)
        import engine  # noqa: F401  (shared constants live there)
        return True
    except Exception:
        return False


_SHARED_CONSTANTS = _try_shared_constants()

# 72-label set (BTZSC-style): 6 groups x 12, deterministic, used by shape 5.
_BTZSC_GROUPS = {
    "emotion": ["joy", "grief", "anger", "fear", "love", "shame",
                "pride", "hope", "envy", "calm", "surprise", "disgust"],
    "sentiment": ["very-negative", "negative", "slightly-negative", "neutral",
                  "slightly-positive", "positive", "very-positive", "mixed",
                  "sarcastic", "ambivalent", "indifferent", "passionate"],
    "intent": ["ask-fact", "ask-opinion", "request-action", "apologize",
               "thank", "complain", "negotiate", "warn", "invite",
               "refuse", "confirm", "inform"],
    "topic": ["health", "money", "work", "family", "travel", "food",
              "technology", "sports", "art", "politics", "education",
              "nature"],
    "tone": ["formal", "casual", "hostile", "warm", "clinical", "playful",
             "urgent", "measured", "ironic", "sincere", "curt", "flowery"],
    "urgency": ["right-now", "today", "this-week", "this-month", "eventual",
                "when-possible", "scheduled", "deadline-driven", "patient",
                "immediate-followup", "blocked", "waiting"],
}


def btzsc_labels():
    """The 72-label choice slate, ordered (groups above, 12 labels each)."""
    labels = []
    for group in _BTZSC_GROUPS.values():
        labels.extend(group)
    assert len(labels) == 72 and len(set(labels)) == 72
    return labels


# --------------------------------------------------------------------------
# Statistics (pure, unit-tested)
# --------------------------------------------------------------------------

def percentile_nearest_rank(samples, p):
    """Nearest-rank percentile: sort, rank = ceil(p/100 * n) clamped to
    [1, n], return sorted[rank-1]. For [1..100]: p50 -> 50, p95 -> 95.
    The eps guards float wobble (0.95*100 == 94.99999999999999 in IEEE754)."""
    if not samples:
        raise ValueError("percentile of empty sample set")
    if not (0.0 <= p <= 100.0):
        raise ValueError(f"percentile out of range: {p}")
    s = sorted(samples)
    n = len(s)
    rank = math.ceil(p / 100.0 * n - 1e-9)
    rank = max(1, min(n, rank))
    return s[rank - 1]


def build_stats(samples):
    """p50/p95/min/max/count from a list of wall-ms values."""
    if not samples:
        raise ValueError("no samples")
    return {
        "count": len(samples),
        "min": min(samples),
        "max": max(samples),
        "p50": percentile_nearest_rank(samples, 50),
        "p95": percentile_nearest_rank(samples, 95),
    }


# --------------------------------------------------------------------------
# Question / request builders (pure, unit-tested)
# --------------------------------------------------------------------------

def guardrail_choice_request():
    """Shape 1: guardrail-style choice question with a 32-option slate
    (choose-style max: 30 candidate actions + reobserve + abstain, spec §2
    choose: candidates 2-32 must include reobserve + abstain)."""
    actions = [f"action-{i:02d}" for i in range(30)]
    criteria = {o: f"take candidate action {i}" for i, o in enumerate(actions)}
    criteria["abstain"] = "decline: none of the candidates fits"
    criteria["reobserve"] = "gather more information before acting"
    assert len(criteria) == 32
    questions = {
        "next_action": {
            "type": "choice",
            "instructions": ("Given the on-screen state, which single action "
                             "should the agent take next?"),
            "criteria": criteria,
        }
    }
    return {"state": "Screen shows a save dialog over unsaved work.",
            "questions": questions,
            "meta": {"n_options": len(criteria), "n_questions": 1,
                     "bar_scope": "single choose call"}}


def rerank_noul_request(n=121):
    """Shape 2: ONE request carrying n noul questions named rel_0..rel_{n-1}
    — proxy for a full 121-passage rerank round (jev caps 60/batch; this is
    the round-sized worst case)."""
    questions = {
        f"rel_{i}": {
            "type": "noul",
            "instructions": (f"Is passage P{i} relevant to the user's "
                             "question in the state?"),
        }
        for i in range(n)
    }
    return {"state": ("User question: how do I rotate the API key without "
                      "dropping the session? P0..P%d passages indexed." % (n - 1)),
            "questions": questions,
            "meta": {"n_options": 0, "n_questions": len(questions),
                     "bar_scope": "full rerank round (121 passages)"}}


def skillpick_choice_requests(n_skills=120, batch=30):
    """Shape 3: skill-style batch of n_skills choice questions (S<i> with
    keep-style options + none), split into requests of `batch` each —
    4 requests of 30 for the default. Each request also carries `none` as an
    option on every question (jev pick-skill stage-1 shape)."""
    if n_skills % batch != 0:
        raise ValueError("n_skills must divide evenly into batch")
    options = ["select", "shortlist", "skip", "none"]
    reqs = []
    for start in range(0, n_skills, batch):
        questions = {}
        for i in range(start, start + batch):
            questions[f"S{i}"] = {
                "type": "choice",
                "instructions": (f"For skill {i} (name: skill-{i} desc), does "
                                 "the state call for loading it?"),
                "criteria": {
                    "select": f"skill-{i} is the clear match",
                    "shortlist": f"skill-{i} is a plausible finalist",
                    "skip": f"skill-{i} does not apply",
                    "none": "no skill is needed for this state",
                },
            }
        reqs.append({"state": "User asks to set up a scheduled backup job.",
                     "questions": questions,
                     "meta": {"n_options": len(options),
                              "n_questions": len(questions),
                              "bar_scope": "one skillpick batch of 30"}})
    return reqs


def route_score_request():
    """Shape 4: route-style score question, 4-level rubric, lowest first
    (Trivial→Expert, per spec §2 route)."""
    questions = {
        "difficulty": {
            "type": "score",
            "instructions": "Rate the difficulty of the request in the state.",
            "criteria": ["Trivial", "Moderate", "Hard", "Expert"],
        }
    }
    return {"state": "Refactor the auth middleware and keep the API stable.",
            "questions": questions,
            "meta": {"n_options": 4, "n_questions": 1,
                     "bar_scope": "single route call"}}


def btzsc_choice_request():
    """Shape 5: BTZSC-style single choice question over all 72 labels."""
    labels = btzsc_labels()
    questions = {
        "label": {
            "type": "choice",
            "instructions": "Which label best describes the state?",
            "criteria": {lbl: f"the state is primarily {lbl}" for lbl in labels},
        }
    }
    return {"state": "The customer wrote in frustrated but hopeful, asking "
                     "for a refund this week.",
            "questions": questions,
            "meta": {"n_options": len(labels), "n_questions": 1,
                     "bar_scope": "single 72-label choose call"}}


def build_requests():
    """All five shapes → list of (shape_name, [request, ...]) in report order."""
    return [
        ("guardrail-choice-32", [guardrail_choice_request()]),
        ("rerank-noul-121", [rerank_noul_request(121)]),
        ("skillpick-choice-120", skillpick_choice_requests(120, 30)),
        ("route-score-4", [route_score_request()]),
        ("btzsc-choice-72", [btzsc_choice_request()]),
    ]


def validate_request(req):
    """Contract preflight (§1): state ≤ 60k JSON chars; question types are
    noul/choice/score; criteria rules. Returns list of violation strings."""
    v = []
    body = json.dumps({"state": req["state"], "questions": req["questions"]})
    if len(body) > STATE_CHAR_CAP:
        v.append(f"state_too_large: {len(body)} chars > {STATE_CHAR_CAP}")
    for name, q in req["questions"].items():
        t = q.get("type")
        if t not in ("noul", "choice", "score"):
            v.append(f"{name}: unknown question type {t!r}")
            continue
        ins = q.get("instructions")
        if not isinstance(ins, str) or not ins.strip():
            v.append(f"{name}: instructions must be non-empty")
        if t == "noul" and "criteria" in q:
            v.append(f"{name}: noul questions must not carry criteria")
        if t == "choice":
            crit = q.get("criteria")
            if not isinstance(crit, dict) or len(crit) < 2:
                v.append(f"{name}: choice needs >=2 criteria options")
        if t == "score":
            crit = q.get("criteria")
            if not isinstance(crit, list) or len(crit) < 2:
                v.append(f"{name}: score needs a rubric list of >=2 levels")
    return v


# --------------------------------------------------------------------------
# Response checks + report building (pure, unit-tested)
# --------------------------------------------------------------------------

def check_model_echo(resp, expected_model):
    """Model echo assertion: a response must not claim a model other than the
    expected one. A response with NO model field is an inconclusive pass
    (echo is optional on the wire; its absence is reported, never faked).
    Returns (observed_model_or_None, ok_bool)."""
    observed = None
    if isinstance(resp, dict):
        observed = resp.get("model")
    return observed, (observed is None or observed == expected_model)


def build_report_item(shape, samples, requests, expected_model,
                      echoes_ok=True, error=None):
    """One report entry: shape meta + stats + model echo check. Pure — no
    clock, no I/O — so it is fully unit-testable on canned samples."""
    reqs = requests
    item = {
        "shape": shape,
        "requests": len(reqs),
        "questions_per_request": [r["meta"]["n_questions"] for r in reqs],
        "options_max": max(r["meta"]["n_options"] for r in reqs),
        "bar_scope": reqs[0]["meta"]["bar_scope"],
        "expected_model": expected_model,
        "echoes_ok": bool(echoes_ok),
    }
    if error is not None:
        item["error"] = error
        item["stats"] = None
    else:
        item["stats"] = build_stats(samples)
    return item


def apply_bars(shape_stats, bars):
    """shape_stats: {shape: {"stats": {...} | None}}. bars: {bar_key: seconds}
    (e.g. {"route": 0.4}). Returns {shape: bool | None}: under_bar is
    p50_ms/1000 < bar_seconds; None when the shape has no bar or no stats."""
    out = {}
    for shape, entry in shape_stats.items():
        bar_key = SHAPE_BAR_KEYS.get(shape)
        stats = entry.get("stats") if isinstance(entry, dict) else entry
        if bar_key is None or bars is None or bar_key not in bars or not stats:
            out[shape] = None
        else:
            out[shape] = (stats["p50"] / 1000.0) < float(bars[bar_key])
    return out


def build_report(base_url, model, repeats, items, under_bar=None,
                 bars=None, generated_at=None):
    """Whole report: pretty, sorted-key JSON on write. items is the list of
    report entries from build_report_item."""
    rep = {
        "schema": REPORT_SCHEMA,
        "generated_at": (generated_at if generated_at is not None
                         else datetime.now(timezone.utc)
                         .isoformat(timespec="seconds")),
        "base_url": base_url,
        "model": model,
        "repeats": repeats,
        "shapes": items,
        "overall": {
            "shapes_ok": sum(1 for i in items
                             if i.get("stats") and i.get("echoes_ok")),
            "shapes_total": len(items),
        },
    }
    if under_bar is not None:
        rep["bars"] = dict(bars if bars is not None else DEFAULT_BARS)
        rep["under_bar"] = dict(under_bar)
    return rep


# --------------------------------------------------------------------------
# Live mode (implemented; exercised by the parent bench runner — never here)
# --------------------------------------------------------------------------

def post_json(base_url, payload, timeout=HTTP_TIMEOUT_S):
    """POST one request payload to the bridge; returns (parsed_json,
    wall_ms). Client-side wall time measured around the full round trip."""
    body = json.dumps({"state": payload["state"],
                       "model": payload["model"],
                       "questions": payload["questions"]}).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + QUESTION_API_PATH,
        data=body,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    wall_ms = (time.perf_counter() - t0) * 1000.0
    return json.loads(raw.decode()), wall_ms


def run_shape(base_url, shape, reqs, model, repeats):
    """Warm 1 call, then `repeats` timed calls (each timed call replays every
    request in the shape — a round). Returns (item, samples_or_None).
    Transport errors in EITHER phase degrade to an error item — a daemon
    down at warm-up must not crash the harness (robustness by default)."""
    for r in reqs:
        payload = {"state": r["state"], "model": model,
                   "questions": r["questions"]}
        violations = validate_request(payload)
        if violations:
            return (build_report_item(shape, [], reqs, model, echoes_ok=False,
                                      error="; ".join(violations)), None)
    samples = []
    echoes_ok = True
    try:
        for r in reqs:
            payload = {"state": r["state"], "model": model,
                       "questions": r["questions"]}
            resp, _warm_ms = post_json(base_url, payload)  # warm, untimed
            _ = resp
        for _ in range(repeats):
            for r in reqs:
                payload = {"state": r["state"], "model": model,
                           "questions": r["questions"]}
                resp, wall_ms = post_json(base_url, payload)
                samples.append(wall_ms)
                _, ok = check_model_echo(resp, model)
                echoes_ok = echoes_ok and ok
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return (build_report_item(shape, [], reqs, model,
                                  echoes_ok=False,
                                  error=f"{type(exc).__name__}: {exc}"),
                None)
    return (build_report_item(shape, samples, reqs, model, echoes_ok=echoes_ok),
            samples)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="laya_latency",
        description="p50/p95 latency harness for the Laya bridge "
                    "(offline-tested; live mode drives the daemon).")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="model id sent + echo-checked (default: %default)")
    ap.add_argument("--bars", default=None,
                    help="optional JSON file {bar-key: seconds} overriding "
                         "the published Jev bars (route/rerank-round/choose)")
    return ap.parse_args(argv)


def load_bars(path):
    """--bars file: {shape/bar key: seconds}. Missing file → published bars."""
    bars = dict(DEFAULT_BARS)
    if path:
        with open(path) as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise ValueError("--bars file must contain a JSON object")
        bars.update({k: float(v) for k, v in user.items()})
    return bars


def main(argv=None):
    args = parse_args(argv)
    bars = load_bars(args.bars)
    shapes = build_requests()

    items = []
    stats_by_shape = {}
    for shape, reqs in shapes:
        item, _samples = run_shape(args.base_url, shape, reqs,
                                   args.model, args.repeats)
        items.append(item)
        stats_by_shape[shape] = {"stats": item["stats"]}

    under_bar = apply_bars(stats_by_shape, bars)
    report = build_report(args.base_url, args.model, args.repeats, items,
                          under_bar=under_bar, bars=bars)

    # The ONLY file this harness ever writes.
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote {args.out}: "
          f"{report['overall']['shapes_ok']}/{report['overall']['shapes_total']} "
          "shapes ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
