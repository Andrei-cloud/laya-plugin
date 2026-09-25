"""T2.5 premise proof: the dual-written alias log is byte-identical and
shaped so the upstream router-dashboard reads our decisions with zero
fork (its routing_store.py tails <home>/logs/jev-decisions.jsonl and
parses {kind, ts, ...} keys). Locked by regression: the day the two
files drift, the dashboard silently loses our traffic.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import laya_decisions as D                       # noqa: E402
import naming                                    # noqa: E402
from naming import _reset_state                  # noqa: E402

_ALL_ENV = [f"{ns}{n}" for n in ("LOG_DIR", "MODEL")
            for ns in ("LAYA_", "JEV_", "TYPESAFE_")] + [
    "HOME", "HERMES_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"]


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for var in _ALL_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("LAYA_LOG_DIR", str(tmp_path / "logs"))
    _reset_state()
    yield tmp_path
    _reset_state()


def test_dual_files_byte_identical():
    D.log_decision("route", {"routed": True, "model": "p:big",
                             "confidence": 0.9})
    D.log_decision("skill", {"skill": "pdf", "stage2": True})
    d = Path(naming.env_alias("LOG_DIR"))
    a = d / "laya-decisions.jsonl"
    b = Path(d) / "jev-decisions.jsonl"
    assert a.read_bytes() == b.read_bytes()      # THE T2.5 premise
    assert a.stat().st_mode & 0o777 == 0o600


def test_record_shaped_for_their_dashboard():
    # upstream routing_store.py: json.loads per line, reads kind + ts
    D.log_decision("route_effective", {"applied": True,
                                       "effective": "p:mid"})
    d = Path(naming.env_alias("LOG_DIR"))
    lines = (d / "jev-decisions.jsonl").read_text().splitlines()
    row = json.loads(lines[-1])
    assert row["kind"] == "route_effective"
    assert isinstance(row["ts"], float)
    assert row["applied"] is True


def test_never_prompt_text():
    D.log_decision("route", {"routed": False,
                             "reason": "user said api_key=sk-NOSHOWHERE"})
    d = Path(naming.env_alias("LOG_DIR"))
    raw = (d / "laya-decisions.jsonl").read_text()
    assert "NOSHOWHERE" not in raw               # redact-before-log holds
