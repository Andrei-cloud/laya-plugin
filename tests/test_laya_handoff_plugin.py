"""Tests for hermes-laya-handoff (capsules + plugin seams).

Hermetic: tmp HOME/HERMES_HOME per test, naming._reset_state before and
after, the real ``hermes`` CLI is NEVER invoked (export via injected
runner). FakeCtx mirrors the plugin API. Every assertion names the spec §
or doctrine it locks (D10: no seam without a test).
"""
import importlib.util
import json
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN = _ROOT / "hermes" / "plugin" / "hermes-laya-handoff"
sys.path.insert(0, str(_ROOT / "server"))
sys.path.insert(0, str(_PLUGIN))

import naming                                    # noqa: E402
from naming import _reset_state                  # noqa: E402
import capsules                                  # noqa: E402


def _load_plugin():
    """Import the package's __init__ by path (hyphenated dir name)."""
    spec = importlib.util.spec_from_file_location(
        "hermes_laya_handoff", str(_PLUGIN / "__init__.py"),
        submodule_search_locations=[str(_PLUGIN)])
    m = importlib.util.module_from_spec(spec)
    sys.modules["hermes_laya_handoff"] = m
    spec.loader.exec_module(m)
    return m


_ALL_ENV = [f"{ns}{n}" for n in
            ("HERMES_HOME", "HANDOFF_JEV", "HANDOFF_CONFIDENTIAL",
             "LOG_DIR", "ROUTING_CONFIG")
            for ns in ("LAYA_", "JEV_", "TYPESAFE_")] + [
    "HOME", "HERMES_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    "HERMES_SESSION_ID", "HERMES_SESSION_PLATFORM", "HERMES_CLI"]


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for var in _ALL_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("LAYA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    _reset_state()
    capsules._reset_test_state() if hasattr(capsules, "_reset_test_state") \
        else None
    yield tmp_path
    _reset_state()


def _runner(rows):
    """Fake subprocess.run for export_messages: `rows` is a list of jsonl
    rows (each {"messages": [...]}); echoes them on stdout."""
    payload = "\n".join(json.dumps(r) for r in rows)

    def run(cmd, **kw):
        class R:
            returncode = 0
            stdout = payload + "\n"
        return R()
    return run


MSGS = [{"role": "user", "content": "ship the report " + str(i)}
        for i in range(4)]


class TestTriggers:
    def test_bare_word_only(self):
        assert capsules.is_trigger("handoff")
        assert capsules.is_trigger("  Handoff. ")
        assert capsules.is_trigger("laya handoff")
        assert not capsules.is_trigger("write me a handoff manual")
        assert not capsules.is_trigger("the handoff failed yesterday")
        assert not capsules.is_trigger(None)

    def test_trigger_set_is_small(self):
        # D6: bare word + both-namespace prefixes; nothing else invented
        assert set(capsules.TRIGGERS) == {"handoff", "jev handoff",
                                          "laya handoff"}


class TestLane:
    def test_lane_direct_when_chat_known(self):
        lane = capsules.lane_key({"platform": "telegram",
                                  "chat_id": "42", "thread_id": "7",
                                  "session_id": "s1"})
        assert lane == "telegram:42:7"

    def test_lane_falls_to_session_store(self):
        # the deliverer's fields (platform+sender, NO chat_id) must meet
        # the writer's key via the session store
        db = Path(os.environ["HERMES_HOME"]) / "state.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute("create table sessions (id text primary key, "
                     "source text, chat_id text, thread_id text)")
        conn.execute("insert into sessions values ('s9','telegram','42','')")
        conn.commit()
        conn.close()
        assert capsules.lane_from_session("s9") == "telegram:42"
        assert capsules.lane_key({"session_id": "s9",
                                  "platform": "telegram",
                                  "sender_id": "u1"}) == "telegram:42"

    def test_lane_sender_fallback_without_store(self):
        lane = capsules.lane_key({"session_id": "s1", "platform": "telegram",
                                  "sender_id": "u1"})
        assert lane == "telegram:u1"

    def test_lane_cleaned(self):
        assert "/" not in capsules._clean("../../etc/passwd")


class TestBuild:
    def test_build_writes_0600_and_pending(self):
        out = capsules.build_capsule("s1", "cli:me", messages=MSGS)
        assert out["status"] == "ok" and out["model_used"] is False
        cp = Path(out["path"])
        assert stat.S_IMODE(cp.stat().st_mode) == 0o600
        assert cp.read_text().startswith("RECOVERY — previous session")
        assert (capsules.pending_path("cli:me")).is_file()

    def test_build_no_messages(self):
        assert capsules.build_capsule("s1", "cli:me", messages=[]) \
            == {"status": "no_messages", "lane": "cli:me"}

    def test_export_via_cli_not_sqlite(self):
        got = capsules.export_messages("s1", runner=_runner(
            [{"messages": MSGS}]))
        assert got == MSGS

    def test_export_failure_is_empty(self):
        def boom(cmd, **kw):
            raise OSError("no hermes")
        assert capsules.export_messages("s1", runner=boom) == []

    def test_export_skips_non_roles(self):
        rows = [{"messages": [{"role": "tool", "content": "x"},
                              {"role": "user", "content": "hi"},
                              {"role": "assistant",
                               "content": [{"text": "yo"}]}]}]
        got = capsules.export_messages("s1", runner=_runner(rows))
        assert got == [{"role": "user", "content": "hi"},
                       {"role": "assistant", "content": "yo"}]


class TestConfidential:
    def test_marker_file_is_authority(self):
        d = capsules.handoff_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "CONFIDENTIAL").write_text("")
        assert capsules.confidential_here() is True

    def test_env_alias_switch(self):
        os.environ["LAYA_HANDOFF_CONFIDENTIAL"] = "true"
        naming._reset_state()
        assert capsules.confidential_here() is True

    def test_question_redacted_in_capsule(self):
        secret = [{"role": "user",
                   "content": "use api_key=sk-SUPERSECRET123456 for it"}]
        d = capsules.handoff_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "CONFIDENTIAL").write_text("")
        out = capsules.build_capsule("s1", "cli:me", messages=secret)
        text = Path(out["path"]).read_text()
        assert "SUPERSECRET" not in text          # privacy-before-WRITE
        assert "[redacted]" in text

    def test_pending_meta_carries_no_content(self):
        # pending-*.json is metadata only (lane, ts, session_id, flags) —
        # message TEXT never belongs there; the capsule itself is the
        # scrubbed artifact (test_question_redacted_in_capsule).
        d = capsules.handoff_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "CONFIDENTIAL").write_text("")
        capsules.build_capsule(
            "s1", "cli:me",
            messages=[{"role": "user", "content": "pwd=hunter42secret"}])
        raw = capsules.pending_path("cli:me").read_text()
        assert "hunter42secret" not in raw
        assert "pwd=" not in raw


class TestDelivery:
    def test_take_once_and_deleted(self):
        capsules.build_capsule("s1", "cli:me", messages=MSGS)
        got = capsules.take_pending("cli:me", current_session="s2")
        assert "RECOVERY" in got
        assert capsules.take_pending("cli:me", current_session="s2") == ""

    def test_expired_dropped_unread(self):
        capsules.build_capsule("s1", "cli:me", messages=MSGS)
        pp = capsules.pending_path("cli:me")
        meta = json.loads(pp.read_text())
        meta["created_ts"] = time.time() - 37 * 3600
        pp.write_text(json.dumps(meta))
        assert capsules.take_pending("cli:me") == ""
        assert not pp.is_file()                   # dropped, not left rotting

    def test_self_session_never_gets_its_own_capsule(self):
        capsules.build_capsule("s1", "cli:me", messages=MSGS)
        assert capsules.take_pending("cli:me",
                                     current_session="s1") == ""

    def test_missing_capsule_file_is_empty(self):
        assert capsules.take_pending("ghost") == ""

    def test_corrupt_pending_deleted(self):
        d = capsules.handoff_dir()
        d.mkdir(parents=True, exist_ok=True)
        capsules.pending_path("cli:me").write_text("{nope")
        assert capsules.take_pending("cli:me") == ""
        assert not capsules.pending_path("cli:me").is_file()


class FakeCtx:
    def __init__(self, own_handoff=True):
        self.hooks, self.tools, self.cmds, self.sps = [], [], [], []
        self.own_handoff = own_handoff

    def register_hook(self, name, fn):
        self.hooks.append(name)

    def register_tool(self, **kw):
        self.tools.append(kw["name"])

    def register_command(self, name, fn, description=""):
        if name == "handoff" and self.own_handoff:
            raise ValueError("built-in owns the name 'handoff'")
        self.cmds.append(name)

    def register_system_prompt_section(self, name, rule, max_chars):
        self.sps.append((name, max_chars, len(rule)))

    def get_config(self, name, default):
        return default


class TestRegister:
    def test_registers_all_seams(self):
        p = _load_plugin()
        ctx = FakeCtx()
        res = p.register(ctx)
        assert ctx.hooks == ["pre_gateway_dispatch", "pre_llm_call"]
        assert ctx.cmds == ["wrapup"]              # handoff refused -> wrapup
        assert ctx.sps and ctx.sps[0][1] == 700
        assert res["skipped"] and "handoff" in res["skipped"][0]

    def test_handoff_command_when_free(self):
        p = _load_plugin()
        ctx = FakeCtx(own_handoff=False)
        p.register(ctx)
        assert ctx.cmds == ["handoff"]

    def test_dispatch_never_answers(self):
        p = _load_plugin()
        p._reset_test_state()
        p.register(FakeCtx())

        class Source:
            platform = "telegram"
            chat_id = "42"
            thread_id = ""
            user_id = "u1"

        class Event:
            text = "handoff"
            source = Source()
            internal = False

        class GW:
            def _is_user_authorized(self, source):
                return True

        out = p._on_dispatch(Event(), gateway=GW(), session_store=None)
        assert out is None                         # never answers for gateway

    def test_dispatch_ignores_prose_and_internal(self):
        p = _load_plugin()
        p.register(FakeCtx())

        class E:
            text = "write the handoff doc please"
            internal = False
            source = None
        assert p._on_dispatch(E()) is None
        assert p._DISPATCH.get() is None

        class E2:
            text = "handoff"
            internal = True                        # plugin-authored: never
            source = None
        assert p._on_dispatch(E2()) is None
        assert p._DISPATCH.get() is None

    def test_dispatch_refuses_unauthorized(self):
        p = _load_plugin()
        p.register(FakeCtx())
        calls = []

        class Source:
            platform = "telegram"
            chat_id = "42"
            thread_id = ""
            user_id = "evil"

        class Event:
            text = "handoff"
            source = Source()
            internal = False

        class GW:
            def _is_user_authorized(self, source):
                calls.append(source)
                return False

        p._on_dispatch(Event(), gateway=GW())
        assert calls and p._DISPATCH.get() is None  # never spent a build


class TestDeliveryHook:
    def test_pre_llm_injects_once(self, monkeypatch):
        p = _load_plugin()
        p.register(FakeCtx())
        capsules.build_capsule("writer-session", "cli:me", messages=MSGS)
        # deliverer sees a fresh session on the same lane
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cli")
        first = p._on_pre_llm_call(session_id="fresh-session",
                                   platform="cli", sender_id="me")
        assert first and "RECOVERY" in first["context"]
        again = p._on_pre_llm_call(session_id="fresh-session",
                                   platform="cli", sender_id="me")
        assert again is None                        # ONE shot per session

    def test_no_session_no_injection(self):
        p = _load_plugin()
        p.register(FakeCtx())
        assert p._on_pre_llm_call(session_id="") is None

    def test_injection_disabled_by_config(self, monkeypatch):
        p = _load_plugin()

        class Ctx(FakeCtx):
            def get_config(self, name, default):
                return False if name == "inject" else default
        p.register(Ctx())
        capsules.build_capsule("s1", "cli:me", messages=MSGS)
        out = p._on_pre_llm_call(session_id="fresh2", platform="cli",
                                 sender_id="me")
        # inject=False must not deliver
        assert out is None or "RECOVERY" not in (out or {}).get("context", "")
