#!/usr/bin/env python3
"""Vendor the plugin's server-module closure into each plugin directory.

Why: Hermes loads a plugin as an ISOLATED package — `plugins install`
copytrees ONLY the plugin dir and the loader never adds the repo's
server/ to sys.path (observed: real Hermes load fails with
`No module named 'naming'` even though every unit test passes). The
plugin therefore ships its library inside itself; this script keeps the
copies byte-identical to server/ (DRY: server/ stays the single source
of truth, CI re-runs this with --check and fails on drift).

D7: everything here is laya-named. Reference:
.hermes/plans/2026-09-25-112255-jev-parity-plan.md (Files map, D6/D7).
"""
import argparse
import hashlib
import os
import shutil
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Closure of laya_usecases + naming + laya_decisions, computed with ast
# over server/ (see tests/test_laya_plugin_lib.py — a test recomputes it
# and fails if this list drifts). engine.py is included because
# laya_usecases imports its guard rail; the agent process never builds a
# Core AI Engine in-process (RemoteEngine, laya_remote.py, is the default
# there), so importing engine.py standalone stays stdlib-safe.
MODULES = [
    "naming.py",
    "laya_constants.py",
    "laya_ops.py",
    "laya_decisions.py",
    "laya_usecases.py",
    "engine.py",
    "laya_remote.py",
]
# laya_api_http / laya_api_sec are the daemon's HTTP question-API layer —
# the plugin never imports them (RemoteEngine is a plain JSON client);
# tests/test_laya_plugin_lib.py recomputes the closure and fails on drift.

PLUGIN_DIRS = [
    os.path.join("hermes", "plugin", "hermes-laya"),
    os.path.join("hermes", "plugin", "hermes-laya-handoff"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="verify vendored copies match server/ (CI); no writes")
    args = ap.parse_args()

    drift, written = [], []
    for plugin_dir in PLUGIN_DIRS:
        lib = os.path.join(REPO, plugin_dir, "lib")
        if not args.check:
            os.makedirs(lib, exist_ok=True)
        for mod in MODULES:
            src = os.path.join(REPO, "server", mod)
            dst = os.path.join(lib, mod)
            src_bytes = open(src, "rb").read()
            src_hash = hashlib.sha256(src_bytes).hexdigest()[:16]
            dst_hash = (hashlib.sha256(open(dst, "rb").read()).hexdigest()[:16]
                        if os.path.exists(dst) else None)
            if src_hash == dst_hash:
                continue
            drift.append(f"{os.path.relpath(dst, REPO)}")
            if not args.check:
                shutil.copyfile(src, dst)
                written.append(dst)
        if not args.check:
            # drop stale vendored modules no longer in the closure
            keep = set(MODULES)
            for f in os.listdir(lib):
                if f.endswith(".py") and f not in keep and f != "__init__.py":
                    os.remove(os.path.join(lib, f))
                    if not args.check:
                        print(f"removed stale lib/{f}")

    if args.check:
        if drift:
            print("VENDORED LIB DRIFT (run scripts/sync_plugin_lib.py):\n  "
                  + "\n  ".join(drift))
            return 1
        print("vendored plugin libs in sync with server/")
        return 0
    print(f"synced {len(MODULES)} modules x {len(PLUGIN_DIRS)} plugins "
          f"({len(written)} files written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
