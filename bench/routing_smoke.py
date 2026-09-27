#!/usr/bin/env python
"""Routing smoke after a chain-expansion deploy (r31-e step 4).

POSTs one wire question per family through /v1/laya EXACTLY as the
use-cases build it (same instructions/criteria text), then reads the
daemon's audit JSONL (LAYA_AUDIT) — the ground truth for which chain
actually served the question (RemoteEngine._chain_for is a display label
only). Fail-loud: any mismatch exits 1, so the deploy script stops before
re-measuring accuracies against a mis-routed daemon.

Expectations encode the CURRENT expansion state:
  triage (choice/score/noul) -> ft_triage5     (chain expansion v1)
  mail_sort                  -> ft_mail8
  supervise (noul + action)  -> ft_sup3
  rerank / compact / choose  -> base   (their chains ship only after the
  FT winners pass the pack gate — a hint for a chain the asset lacks MUST
  clamp to base, which this smoke also proves).
"""
import json, os, sys, time
import urllib.request

URL = os.environ.get("DECISIOND_URL", "http://127.0.0.1:11270")
AUDIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "bench", "audit-daemon.jsonl")

TRIAGE_KINDS = ["info", "action-request", "question", "customer-problem",
                "social", "other"]
MAIL_LANES = ["needs_reply", "updates", "promotional", "sales", "spam"]
SUP_ACTIONS = ["keep_waiting", "answer_question", "nudge", "escalate", "collect"]

CASES = [
    ("triage kind",
     {"type": "choice", "route_task": "triage",
      "instructions": "What kind of message is the message in `state`?",
      "criteria": {k: k for k in TRIAGE_KINDS}}, "ft_triage5"),
    ("triage urgency",
     {"type": "score", "route_task": "triage",
      "instructions": "How urgent is the message in `state`, lowest urgency first?",
      "criteria": ["later", "soon", "today", "right-now"]}, "ft_triage5"),
    ("triage noul",
     {"type": "noul", "route_task": "triage",
      "instructions": "does the message ask for an action?"}, "ft_triage5"),
    ("mail lane",
     {"type": "choice", "route_task": "mail_sort",
      "instructions": "Which mailbox lane does the mail in `state` belong to?",
      "criteria": {k: k for k in MAIL_LANES}}, "ft_mail8"),
    ("supervise noul",
     {"type": "noul", "route_task": "supervise",
      "instructions": "is the run blocked or stuck?"}, "ft_sup3"),
    ("supervise action",
     {"type": "choice", "route_task": "supervise",
      "instructions": "What should the supervisor do about the run described in `state`?",
      "criteria": {k: k for k in SUP_ACTIONS}}, "ft_sup3"),
    ("choose (chain absent -> must clamp)",
     {"type": "choice", "route_task": "choose",
      "instructions": "Given `state`, which single next action should run?",
      "criteria": {"click_a": "a", "click_b": "b", "reobserve": "look again",
                   "abstain": "give up"}}, "base"),
]


def post_question(q):
    payload = {"model": "laya-r15", "state": "routing smoke probe",
               "questions": {"smoke": dict(q)}}
    req = urllib.request.Request(
        URL + "/v1/laya", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main():
    health = json.load(urllib.request.urlopen(URL + "/health", timeout=10))
    print("daemon chains:", health.get("chains"))
    if not health.get("warm"):
        sys.exit("SMOKE FAIL: daemon not warm")
    # the audit must exist for ground truth (deploy script sets LAYA_AUDIT)
    if not os.path.exists(AUDIT):
        sys.exit(f"SMOKE FAIL: audit file missing ({AUDIT}) — no ground truth")

    fails = 0
    for name, q, expect in CASES:
        before = os.path.getsize(AUDIT)
        t0 = time.time()
        post_question(q)
        deadline = time.time() + 10
        entry = None
        while time.time() < deadline:           # audit flush is same-thread;
            if os.path.getsize(AUDIT) > before:  # tiny grace for file flush
                with open(AUDIT, "rb") as f:
                    f.seek(before)
                    line = f.readline().decode().strip()
                if line:
                    entry = json.loads(line)
                    break
            time.sleep(0.2)
        if entry is None:
            print(f"  FAIL {name}: no audit line appeared")
            fails += 1
            continue
        got = entry.get("chain")
        ms = round((time.time() - t0) * 1000)
        mark = "ok  " if got == expect else "FAIL"
        print(f"  {mark} {name}: chain={got} (expect {expect}) "
              f"task={entry.get('task')} {ms}ms")
        if got != expect:
            fails += 1

    if fails:
        sys.exit(f"SMOKE FAIL: {fails}/{len(CASES)} mis-routed")
    print(f"SMOKE PASS: {len(CASES)}/{len(CASES)} questions routed as designed")


if __name__ == "__main__":
    main()
