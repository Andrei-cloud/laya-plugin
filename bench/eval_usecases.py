#!/usr/bin/env python
"""Per-use-case wire eval against the live daemon (RemoteEngine -> :11270).

Measures the use cases that have a trained head but only offline gate-table
accuracy, through the exact wire the plugin uses — accuracy + latency p50/p95
per use case. Packs are the FROZEN eval suites from the model repo (already
train-excluded by construction; never regenerate them here):

  guardrail  laya_usecases.guard_command  vs label.disposition (both oracle
             suites: adversarial_guard[2].jsonl, seen + unseen)
  lang_route engine.question(lang slate)  vs label.lang (open-massive sample)
  skill      laya_usecases.skill_select  vs label.skill (synth_skill_v2:
             all answerable rows + sampled no-skill negatives)
  triage     bench/evalpacks/triage.jsonl if present (authored pack; the
             model repo has no lane-labeled frozen suite -> TODO in
             docs/USECASE-COVERAGE.md)

FAIRNESS: run ONLY when no GPU benchmark is in flight (daemon calls ride the
same machine; latency bars must not be measured under load).

Run:  python3 eval_usecases.py [--only guardrail] [--limit N]
Writes runs/usecase_eval.json + prints a table.
"""
import argparse
import json
import os
import random
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
OUT = os.path.join(HERE, "runs", "usecase_eval.json")
LAYA_EVAL = os.path.expanduser("~/Developer/ai/laya/eval/datasets")

import laya_usecases as U           # noqa: E402
from laya_remote import RemoteEngine  # noqa: E402


def rows(path, limit=None, where=None):
    out = []
    for l in open(path):
        if not l.strip():
            continue
        r = json.loads(l)
        if where and not where(r):
            continue
        out.append(r)
    if limit:
        random.seed(13)
        out = out if len(out) <= limit else random.sample(out, limit)
    return out


def timed(fn, *a, **k):
    t0 = time.perf_counter()
    res = fn(*a, **k)
    return res, round((time.perf_counter() - t0) * 1000, 1)


def summarize(case, ok, n, lat):
    lat = sorted(lat)
    return {"n": n, "correct": ok, "accuracy": round(ok / max(1, n), 4),
            "lat_p50_ms": round(statistics.median(lat), 1) if lat else 0,
            "lat_p95_ms": lat[int(len(lat) * 0.95) - 1] if lat else 0}


def eval_guardrail(eng, limit):
    # Gate semantics (GO_GATE.md): a RED LINE is a dangerous row answered
    # ALLOW (the product escalates ask_user on destructive rows BY DESIGN —
    # that is the "GO as gate" verdict, not a miss). We report BOTH:
    #   exact_acc   — choice == label.disposition (strict)
    #   red         — dangerous (block-labeled) rows answered allow (must be <=2)
    #   safe_acc    — non-allow answers on block-labeled + benign allowed
    ok, lat, misses = 0, [], []
    red, n, red_rows = 0, 0, []
    for suite in ("adversarial_guard", "adversarial_guard2"):
        p = os.path.join(LAYA_EVAL, suite + ".jsonl")
        if not os.path.exists(p):
            continue
        rs = rows(p, limit)
        for r in rs:
            gold = r["label"]["disposition"]
            ans, ms = timed(U.guard_command, eng, r["state"])
            lat.append(ms)
            n += 1
            got = ans.get("choice")
            if got == gold:
                ok += 1
            else:
                misses.append({"suite": suite, "state": r["state"][:90],
                               "gold": gold, "got": got,
                               "conf": ans.get("confidence")})
            if gold == "block":
                red_rows.append(r["state"][:90])
                if got == "allow":
                    red += 1
    return ok, n, lat, misses, {"red": red, "red_total": len(red_rows)}


def eval_lang(eng, limit):
    p = os.path.join(LAYA_EVAL, "dataset_open_massive.jsonl")
    if not os.path.exists(p):
        return 0, 0, [], [], {}
    ok, lat, misses, n = 0, [], [], 0
    for r in rows(p, limit):
        q = r["questions"]["lang"]
        ans, ms = timed(eng.question, "lang", q, r["state"])
        lat.append(ms)
        n += 1
        if ans.get("choice") != r["label"]["lang"]:
            misses.append({"state": r["state"][:90], "gold": r["label"]["lang"],
                           "got": ans.get("choice")})
        else:
            ok += 1
    return ok, n, lat, misses, {}


def eval_skill(eng, limit):
    p = os.path.join(LAYA_EVAL, "dataset_synth_skill_v2.jsonl")
    if not os.path.exists(p):
        return 0, 0, [], [], {}
    allrows = rows(p)
    gold_rows = [r for r in allrows if r["label"].get("skill")]
    none_rows = [r for r in allrows if not r["label"].get("skill")]
    random.seed(7)
    picked = gold_rows[:limit or 200] + random.sample(
        none_rows, min(len(none_rows), (limit or 100) // 2))
    ok, lat, misses, n = 0, [], [], 0
    for r in picked:
        slate = list(r["questions"]["skill"]["criteria"])
        ans, ms = timed(U.skill_select, eng, r["state"], slate)
        lat.append(ms)
        n += 1
        if ans.get("skill") != r["label"].get("skill"):
            misses.append({"state": r["state"][:90],
                           "gold": r["label"].get("skill"),
                           "got": ans.get("skill")})
        else:
            ok += 1
    return ok, n, lat, misses, {}


def eval_triage(eng, limit):
    p = os.path.join(HERE, "evalpacks", "triage.jsonl")
    if not os.path.exists(p):
        return 0, 0, [], [], {}
    ok, lat, misses, n = 0, [], [], 0
    for r in rows(p, limit):
        ans, ms = timed(U.triage_message, eng, r["message"])
        lat.append(ms)
        n += 1
        got = ans.get("route")   # triage routes: now/today/queue/ignore
        if got != r["gold"]:
            misses.append({"state": r["message"][:90], "gold": r["gold"],
                           "got": got})
        else:
            ok += 1
    return ok, n, lat, misses, {}


def eval_mail(eng, limit):
    """Held-out mail pack over the REAL wire. The lane slate is a foreign
    slate until ft_mail is combined; route_task pins the chain the daemon
    should answer with (chain rule in server/engine.py honours the hint)."""
    p = os.path.join(HERE, "evalpacks", "mail.jsonl")
    if not os.path.exists(p):
        return 0, 0, [], [], {}
    q = {"type": "choice",
         "instructions": "Which mailbox lane does the mail in `state` belong to?",
         "criteria": {"needs_reply": "a real person expects a reply from me",
                      "updates": "updates / FYI worth reading eventually",
                      "promotional": "marketing / promotions",
                      "sales": "sales outreach",
                      "spam": "junk or malicious mail"},
         "route_task": "mail_sort"}
    ok, lat, misses, n = 0, [], [], 0
    for r in rows(p, limit):
        ans, ms = timed(eng.question, "lane", q, r["state"])
        lat.append(ms)
        n += 1
        got = ans.get("choice") if (ans.get("confidence") or 0) >= 0.7 else None
        if got == r["gold"]["lane"]:
            ok += 1
        else:
            misses.append({"state": r["state"][:90], "gold": r["gold"]["lane"],
                           "got": got, "conf": ans.get("confidence")})
    return ok, n, lat, misses, {"coverage": round((n - sum(
        1 for m in misses if m["got"] is None)) / max(1, n), 4)}


EVALS = {"guardrail": eval_guardrail, "lang": eval_lang,
         "skill": eval_skill, "triage": eval_triage, "mail": eval_mail}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, choices=list(EVALS))
    ap.add_argument("--limit", type=int, default=None,
                    help="rows per suite (guardrail: per oracle suite)")
    ap.add_argument("--url", default=None, help="default: DECISIOND_URL/env")
    a = ap.parse_args()

    eng = RemoteEngine(a.url)
    health = eng._get("/health") if hasattr(eng, "_get") else None
    print("daemon:", health if health else "(no /health helper)")

    report = {}
    for case in ([a.only] if a.only else list(EVALS)):
        t0 = time.perf_counter()
        ok, n, lat, misses, extras = EVALS[case](eng, a.limit)
        if n == 0:
            print(f"{case:10s} no pack/rows — skipped")
            continue
        s = summarize(case, ok, n, lat)
        s.update(extras)
        s["total_s"] = round(time.perf_counter() - t0, 1)
        s["misses_sample"] = misses[:25]
        report[case] = s
        print(f"{case:10s} {ok}/{n} acc={s['accuracy']:.3f} "
              f"p50={s['lat_p50_ms']}ms p95={s['lat_p95_ms']}ms")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(report, open(OUT, "w"), indent=1)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
