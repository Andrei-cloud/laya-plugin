#!/usr/bin/env python3
"""nightly-handoff — write whole-dialogue capsules for idle sessions
(T2.4; spec-v2 §2 handoff row). Scheduling is the USER's: this script
never installs a timer, cron, or launchd unit — run it from your own
launchd/launchctl/cron if you want nightly capsules.

What it does: finds sessions that went quiet longer than --idle-min
(default 240), builds the model-free capsule (1 200-word tail + Recovery
block — §5: the model-written digest measured a loss), stores it as
pending for that conversation lane. The next fresh session in the lane
receives it once (hermes-laya-handoff plugin), 36 h max age.

Idempotent: a lane whose newest session already has a pending capsule,
or whose capsule was written AFTER the session went quiet, is skipped.
Atomic: every write is tmp + os.replace, files 0600.
--dry-run touches NOTHING (not even directories).

Env: HERMES_HOME selects the profile (host-addressing seam). Python:
    LAYA_PYTHON or python3 with the laya-plugin server/ importable.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for p in (_ROOT / "server", _ROOT / "hermes" / "plugin" /
          "hermes-laya-handoff"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import capsules                                    # noqa: E402
from naming import hermes_root                     # noqa: E402


def recent_sessions(db, limit=200):
    """(session_id, platform, chat, thread, last_ts) newest-first. The
    session store is read-only here; schema probed, never assumed."""
    if not db.is_file():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        cols = {r[1] for r in conn.execute("pragma table_info(sessions)")}
        want = [c for c in ("id", "source", "chat_id", "thread_id",
                            "updated_at", "started_at") if c in cols]
        if "id" not in cols:
            return []
        ts_col = "updated_at" if "updated_at" in cols else "started_at"
        order = ts_col + " desc" if ts_col in cols else "rowid desc"
        rows = conn.execute(
            f"select {', '.join(want)} from sessions "
            f"order by {order} limit {int(limit)}").fetchall()
        out = []
        for r in rows:
            d = dict(zip(want, r))
            out.append((str(d.get("id")), str(d.get("source") or ""),
                        str(d.get("chat_id") or ""),
                        str(d.get("thread_id") or ""),
                        float(d.get(ts_col) or 0)))
        return out
    finally:
        conn.close()


def already_pending(lane: str, since_ts: float) -> bool:
    """Idempotence: skip when this lane has a FRESH pending capsule
    written after the session's last activity."""
    pp = capsules.pending_path(lane)
    try:
        meta = json.loads(pp.read_text())
        return float(meta.get("created_ts") or 0) >= since_ts - 1
    except (OSError, ValueError):
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Write idle-session handoff capsules (schedule this "
                    "yourself; this script never schedules itself).")
    ap.add_argument("--idle-min", type=int, default=240,
                    help="session is a candidate after N quiet minutes "
                         "(default 240)")
    ap.add_argument("--limit", type=int, default=10,
                    help="max capsules per run (default 10)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what WOULD be written; touch nothing")
    args = ap.parse_args(argv)

    home = hermes_root()
    db = home / "state.db"
    now = time.time()
    wrote = skipped = 0
    seen_lanes = set()
    for sid, platform, chat, thread, last in recent_sessions(db):
        if wrote >= args.limit:
            break
        if not sid or last <= 0 or (now - last) < args.idle_min * 60:
            continue
        lane = capsules.lane_key({"platform": platform, "chat_id": chat,
                                  "thread_id": thread, "session_id": sid})
        if lane in seen_lanes:
            skipped += 1               # newer session owns this lane
            continue
        seen_lanes.add(lane)
        if already_pending(lane, last):
            skipped += 1
            continue
        if args.dry_run:
            print(json.dumps({"dry_run": True, "session": sid,
                              "lane": lane,
                              "quiet_min": round((now - last) / 60)}))
            continue
        result = capsules.build_capsule(sid, lane)
        if result.get("status") == "ok":
            wrote += 1
            print(json.dumps({"wrote": result["path"], "lane": lane,
                              "words": result["words"],
                              "model_used": result["model_used"]}))
        else:
            skipped += 1
            print(json.dumps({"skipped": result.get("status"),
                              "session": sid, "lane": lane}))
    print(json.dumps({"summary": True, "wrote": wrote, "skipped": skipped,
                      "idle_min": args.idle_min, "dry_run": args.dry_run}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
