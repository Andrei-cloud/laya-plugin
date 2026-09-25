"""Tests for server/laya_cli.py — the dual-named decision CLI (spec-v2 §2
CLI ops table — THE exit-code + output contract; plan D6 CLI row, D7).

In-process: main([...]) driven with monkeypatched stdin (io.StringIO) and
captured stdout (capsys). Hermetic fixture copied from
tests/test_laya_usecases.py: tmp HOME/HERMES_HOME/log dirs, every alias
spelling unset, naming._reset_state() before and after, keystore reads
stubbed out. Engine-dependent tests monkeypatch laya_cli._engine() to a
FakeEngine (copy of the one in test_laya_usecases.py); zero-model-call
commands are locked to NEVER build the engine by pointing LAYA_ASSETS at a
bogus path AND by a tripwire on _engine(). bin/* are read textually —
unit tests never spawn a shell.

Every assertion names the §2 rail it locks: exit 0 = answered (incl.
degraded fail-open), exit 2 = invalid input (body verbatim, NEVER a
traceback on stdout), exit 3 = engine could not build.
"""
import io
import json
import os
import sys
import types
import urllib.request
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "server"))

import laya_api_sec as sec                     # noqa: E402
import laya_cli                                # noqa: E402
import laya_constants as K                     # noqa: E402
import naming                                  # noqa: E402
from laya_cli import main                      # noqa: E402

_ALL_ENV = [f"{ns}{n}" for n in
            ("ROUTING_CONFIG", "LADDER_STATE", "MEMO", "MIN_CONFIDENCE",
             "MODEL", "API_KEY", "PROBE_ALLOWLIST", "PLAN_MODEL",
             "HANDOFF_JEV", "TOKEN", "LOG_DIR", "ASSETS", "SOURCE", "UNIT")
            for ns in ("LAYA_", "JEV_", "TYPESAFE_")] + [
    "DASHBOARD_TOKEN", "HOME", "HERMES_HOME", "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME", "TEXT_MODEL", "TEXT_MODEL_API_KEY",
    "TEXT_MODEL_BASE_URL"]

TIERS = {"simple": {"pool": ["p:small"]},
         "medium": {"pool": ["p:mid"]},
         "hard": {"pool": ["p:big"]}}


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    """Empty world per test (fixture style of test_laya_usecases.py):
    tmp HOME + HERMES_HOME + log dir, every alias spelling unset, naming
    memoization reset before and after, engine never inherited between
    tests, keystore lookups stubbed to 'nothing found' (macOS `security`
    must never run in unit tests)."""
    home = tmp_path / "home"
    home.mkdir()
    for var in _ALL_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("LAYA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setattr(laya_cli, "_ENGINE", None, raising=False)
    monkeypatch.setattr(naming, "keychain_secret",
                        lambda service, account: None)
    naming._reset_state()
    yield tmp_path
    naming._reset_state()


class FakeEngine:
    """Copy of the FakeEngine in tests/test_laya_usecases.py:
    engine.question(name, q, state) responder with the real engine's result
    shape. script maps question name -> probs dict (a list is consumed in
    order, the last entry repeats); unset names default sane (noul yes 0.9
    only for rel_<i>, 0.05 elsewhere; choice/score 0.9 on the first key).
    raise_at raises once ANY question is call #N."""

    def __init__(self, script=None, default_yes=0.9, raise_at=None,
                 exc=RuntimeError("engine down")):
        self.script = {k: (v if isinstance(v, list) else [v])
                       for k, v in (script or {}).items()}
        self.default_yes = default_yes
        self.raise_at = raise_at
        self.exc = exc
        self.calls = []

    def _keys(self, q):
        t = q["type"]
        if t == "noul":
            return ["no", "yes"]
        if t == "score":
            return [str(i) for i in range(len(q["criteria"]))]
        return list(q["criteria"])

    def _default_probs(self, name, q, keys):
        if q["type"] == "noul":
            yes = 0.9 if name.startswith("rel_") else 0.05
            return {"yes": yes, "no": 1 - yes}
        return {keys[0]: self.default_yes}

    def question(self, name, q, state):
        if self.raise_at is not None and len(self.calls) >= self.raise_at:
            raise self.exc
        self.calls.append((name, q, state))
        keys = self._keys(q)
        sq = self.script.get(name)
        probs = (sq.pop(0) if len(sq) > 1 else sq[0]) if sq else None
        if probs is None:
            probs = self._default_probs(name, q, keys)
        miss = set(probs) - set(keys)
        assert not miss, f"{name}: probs for unknown options {miss}"
        mass = sum(probs.values())
        full = {k: round(probs.get(k, 0.0) / mass, 4) for k in keys}
        top = max(keys, key=lambda k: full[k])
        return {"task": "t", "chain": "base", "choice": top,
                "confidence": full[top], "acted": True, "act_p": 0.9,
                "probs": full, "latency_ms": 1.0}

    @property
    def count(self):
        return len(self.calls)


def drive(tmp_path, monkeypatch, argv, stdin="", engine=None,
          fake_engine=True):
    """Run main() in-process: stdin as StringIO. fake_engine=True
    monkeypatches laya_cli._engine to the given FakeEngine;
    fake_engine=False leaves whatever tripwire the test installed (used by
    the zero-model-call commands, which must NEVER build the engine).
    Returns (exit_code, parsed_stdout_json_or_None, stderr_text).
    json.loads of the WHOLE stdout succeeding is itself the assertion that
    stdout held exactly ONE JSON document (§2)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    if fake_engine:
        if engine is None:
            engine = FakeEngine()
        monkeypatch.setattr(laya_cli, "_engine", lambda: engine)
    elif engine is not None:
        monkeypatch.setattr(laya_cli, "_engine", lambda: engine)
    code = main(argv)
    captured = capsys_read()
    try:
        body = json.loads(captured[0]) if captured[0].strip() else None
    except ValueError:
        body = None
    return code, body, captured[1]


# capsys is function-scoped; grab it through a module hook set per test.
_capturer = {}


def capsys_read():
    return _capturer["f"]()


@pytest.fixture(autouse=True)
def _cap(capsys):
    def f():
        out = capsys.readouterr()
        return out.out, out.err
    _capturer["f"] = f
    yield


# ------------------------------------------------------------------ ask --
# §2 ask row: raw passthrough, the escape hatch — NO fail-open: any engine
# error is exit 2 {"error": <code>} in the laya_api_sec vocabulary; a
# could-not-build engine is exit 3 (universal §2 exit codes).

class TestAsk:
    def test_happy_answers_wire_envelope(self, tmp_path, monkeypatch):
        e = FakeEngine({"q1": {"yes": 0.9, "no": 0.1}})
        stdin = json.dumps({"state": "hello there",
                            "questions": {"q1": {
                                "type": "noul",
                                "instructions": "is this fine?"}}})
        code, out, _ = drive(tmp_path, monkeypatch, ["ask"], stdin, e)
        assert code == 0
        assert out["answers"]["q1"] == {"type": "noul", "noul": 0.9}
        assert out["usage"]["requests"] == 1

    def test_dead_engine_exits_2_no_failopen(self, tmp_path, monkeypatch):
        # §2 ask row: NO fail-open — the engine raise maps to the sec
        # vocabulary (RuntimeError -> retryable "overloaded"), exit 2.
        e = FakeEngine(raise_at=0)
        stdin = json.dumps({"state": "x y z",
                            "questions": {"q1": {
                                "type": "noul",
                                "instructions": "is this fine?"}}})
        code, out, err = drive(tmp_path, monkeypatch, ["ask"], stdin, e)
        assert code == 2
        assert out["error"] in sec.ERROR_CODES and out["error"] == "overloaded"
        assert "Traceback" not in err            # a raise is not a crash

    def test_unserializable_answer_is_invalid_response(
            self, tmp_path, monkeypatch):
        class JunkEngine:                        # engine_out missing probs
            def question(self, name, q, state):
                return {"task": "t"}
        stdin = json.dumps({"state": "x y z",
                            "questions": {"q1": {
                                "type": "noul",
                                "instructions": "is this fine?"}}})
        code, out, _ = drive(tmp_path, monkeypatch, ["ask"], stdin, JunkEngine())
        assert code == 2
        assert out["error"] == "invalid_response"   # ValueError vocabulary

    def test_envelope_refusal_keeps_sec_code(self, tmp_path, monkeypatch):
        code, out, _ = drive(tmp_path, monkeypatch, ["ask"],
                             json.dumps({"state": "hi", "questions": {}}))
        assert code == 2
        assert out["error"] == "questions_required"

    def test_engine_could_not_build_is_exit_3(self, tmp_path, monkeypatch):
        def unbuildable():
            raise laya_cli.EngineUnavailable("KeyError: 'LAYA_ASSETS'")
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        monkeypatch.setattr(laya_cli, "_engine", unbuildable)
        code = main(["ask"])
        out = json.loads(capsys_read()[0])
        assert code == 3
        assert out["error"] == "engine_unavailable"   # §2: NOT 2, no crash


# ---------------------------------------------------------------- route --

class TestRoute:
    def _cfg(self, tmp_path, monkeypatch):
        p = tmp_path / "routing.json"
        p.write_text(json.dumps({"tiers": TIERS}))
        monkeypatch.setenv("LAYA_ROUTING_CONFIG", str(p))

    def test_happy_exit_0_single_json_doc(self, tmp_path, monkeypatch):
        self._cfg(tmp_path, monkeypatch)
        e = FakeEngine({"difficulty": {"0": 0.05, "1": 0.05, "2": 0.7,
                                       "3": 0.2},
                        "kind": {"coding": 0.9, "writing": 0.03,
                                 "research": 0.04, "general": 0.03}})
        code, out, _ = drive(tmp_path, monkeypatch, ["route"],
                             json.dumps({"turn": "refactor the parser",
                                         "current_model": "p:small"}), e)
        assert code == 0
        assert out["routed"] is True and out["model"] == "p:big"

    def test_text_alias_accepted(self, tmp_path, monkeypatch):
        # D6 accept-under-both: §2 names the stdin key turn|text
        self._cfg(tmp_path, monkeypatch)
        code, out, _ = drive(tmp_path, monkeypatch, ["route"],
                             json.dumps({"text": "what does ls do?"}))
        assert code == 0 and out is not None

    def test_missing_turn_exits_2(self, tmp_path, monkeypatch):
        code, out, err = drive(tmp_path, monkeypatch, ["route"], "{}")
        assert code == 2
        assert out["error"] == "invalid_request"
        assert "turn|text" in out["detail"]
        assert "Traceback" not in err

    def test_invalid_json_exits_2_with_json_body(self, tmp_path, monkeypatch):
        code, out, _ = drive(tmp_path, monkeypatch, ["route"], "{not json")
        assert code == 2
        assert out["error"] == "malformed"

    def test_dead_engine_degraded_exit_0(self, tmp_path, monkeypatch):
        # §2 universal: every cmd except ask exits 0 with a usable
        # degraded answer — routed:false, kept current model.
        self._cfg(tmp_path, monkeypatch)
        e = FakeEngine(raise_at=0)
        code, out, _ = drive(tmp_path, monkeypatch, ["route"],
                             json.dumps({"turn": "anything",
                                         "current_model": "p:mid"}), e)
        assert code == 0
        assert out["routed"] is False and out["model"] == "p:mid"
        assert out["notice"].startswith("[Laya] kept")

    def test_jev_fallback_config_dir_used(self, tmp_path, monkeypatch):
        # D6 config-dir chain without an env path: <hermes>/jev/ fallback
        jev = tmp_path / "hermes" / "jev"
        jev.mkdir(parents=True)
        (jev / "routing.json").write_text(json.dumps({"tiers": TIERS}))
        e = FakeEngine({"difficulty": {"0": 0.05, "1": 0.05, "2": 0.7,
                                       "3": 0.2}})
        code, out, _ = drive(tmp_path, monkeypatch, ["route"],
                             json.dumps({"turn": "trace this crash"}), e)
        assert code == 0 and out["routed"] is True

    def test_unparseable_config_fails_open_to_empty(self, tmp_path,
                                                    monkeypatch):
        # §3 fail-open: unparseable routing config -> {} -> keep current
        laya = tmp_path / "hermes" / "laya"
        laya.mkdir(parents=True)
        (laya / "routing.json").write_text("{broken")
        code, out, _ = drive(tmp_path, monkeypatch, ["route"],
                             json.dumps({"turn": "hello there",
                                         "current_model": "p:mid"}))
        assert code == 0
        assert out["routed"] is False
        assert out["reason"] == "no routing config"


# ----------------------------------------------------------------- misc ops --

class TestRerank:
    def test_happy(self, tmp_path, monkeypatch):
        stdin = json.dumps({"query": "which file",
                            "passages": [{"id": "a", "text": "text a"},
                                         {"id": "b", "text": "text b"}]})
        code, out, _ = drive(tmp_path, monkeypatch, ["rerank"], stdin)
        assert code == 0
        assert out["screening"] == "laya+local"
        assert set(out["chosen"]) == {"a", "b"}

    def test_missing_passages_exits_2(self, tmp_path, monkeypatch):
        code, out, _ = drive(tmp_path, monkeypatch, ["rerank"],
                             json.dumps({"query": "q"}))
        assert code == 2 and out["error"] == "invalid_request"


class TestCompactSelect:
    def test_keep_last_flag_plumbs_through(self, tmp_path, monkeypatch):
        # §2 compact-select: the last --keep-last N turns always keep,
        # a confident drop lands only on non-tail turns
        e = FakeEngine({"t0": {"drop": 0.95, "keep": 0.025,
                               "summarize": 0.025},
                        "t1": {"drop": 0.95, "keep": 0.025,
                               "summarize": 0.025}})
        code, out, _ = drive(tmp_path, monkeypatch,
                             ["compact-select", "--keep-last", "1"],
                             json.dumps({"turns": ["one two three four",
                                                  "five six seven eight"]}),
                             e)
        assert code == 0
        assert out["drop"] == [0] and out["keep"] == [1]

    def test_missing_turns_exits_2(self, tmp_path, monkeypatch):
        code, out, _ = drive(tmp_path, monkeypatch, ["compact-select"], "{}")
        assert code == 2 and out["error"] == "invalid_request"


class TestPickSkill:
    def test_happy_stage1_stage2(self, tmp_path, monkeypatch):
        e = FakeEngine({"needs_skill": {"yes": 0.9, "no": 0.1},
                        "skill_needed": {"yes": 0.9, "no": 0.1}})
        stdin = json.dumps({"state": "how do I diff two files?",
                            "skills": {"unix": "shell one-liners"}})
        code, out, _ = drive(tmp_path, monkeypatch, ["pick-skill"], stdin, e)
        assert code == 0
        assert out["skill"] == "unix" and out["stage2_used"] is True

    def test_missing_skills_exits_2(self, tmp_path, monkeypatch):
        code, out, _ = drive(tmp_path, monkeypatch, ["pick-skill"],
                             json.dumps({"state": "some longer question?"}))
        assert code == 2 and out["error"] == "invalid_request"


class TestChoose:
    def _req(self, cands=("open_notes", "reobserve", "abstain")):
        return {"schema": "jev.action_choice_request_v1",
                "goal": "open the notes app",
                "regions": ["Notes icon in dock"],
                "candidates": [{"id": c, "label": c} for c in cands]}

    def test_happy_above_floor_acts(self, tmp_path, monkeypatch):
        code, out, _ = drive(tmp_path, monkeypatch, ["choose"],
                             json.dumps(self._req()))
        assert code == 0
        assert out["selected_id"] == "open_notes" and out["acted"] is True
        assert out["floor"] == K.CHOOSE_FLOOR_DEFAULT

    def test_invalid_status_exits_2_body_verbatim(self, tmp_path, monkeypatch):
        # §2 choose row: a request without the fail-safe ids is invalid —
        # the primitive's invalid body prints VERBATIM with exit 2
        req = self._req()
        del req["candidates"]
        code, out, _ = drive(tmp_path, monkeypatch, ["choose"],
                             json.dumps(req))
        assert code == 2
        assert out["status"] == "invalid" and out["error"] == "invalid_request"
        assert out["selected_id"] == "abstain"     # §2 invalid body shape

    def test_engine_dead_failopen_exit_0(self, tmp_path, monkeypatch):
        e = FakeEngine(raise_at=0)
        code, out, _ = drive(tmp_path, monkeypatch, ["choose"],
                             json.dumps(self._req()), e)
        assert code == 0                            # §2: fail-open exits 0
        assert out["selected_id"] == "reobserve" and out["confidence"] == 0.0


class TestSearch:
    def _stdin(self, key="question"):
        return json.dumps({key: "what is the cap",
                           "results": [{"id": "r1", "title": "cap doc",
                                        "url": "https://x.test/cap",
                                        "snippet": "the cap is 480"}]})

    def test_happy_exit_0(self, tmp_path, monkeypatch):
        code, out, _ = drive(tmp_path, monkeypatch, ["search"], self._stdin())
        assert code == 0
        assert out["decision"] in ("answer", "search_more",
                                   "propose_queries",
                                   "answer_from_what_we_have", "unknown")

    def test_query_alias_accepted(self, tmp_path, monkeypatch):
        # D6 accept-under-both, documented: §2 names question|query
        code, out, _ = drive(tmp_path, monkeypatch, ["search"],
                             self._stdin("query"))
        assert code == 0 and out["status"] != "invalid"

    def test_primitive_invalid_exits_2(self, tmp_path, monkeypatch):
        # results of the wrong shape -> primitive's status "invalid" ->
        # body verbatim + exit 2 (§2: invalid input, not a crash)
        code, out, _ = drive(tmp_path, monkeypatch, ["search"],
                             json.dumps({"question": "q",
                                         "results": [42]}))
        assert code == 2
        assert out["status"] == "invalid" and out["decision"] == "unknown"

    def test_flags_override_stdin(self, tmp_path, monkeypatch):
        stdin = json.dumps({"question": "q q q", "results": [],
                            "round_index": 1, "max_rounds": 5,
                            "top_k": 3})
        code, out, _ = drive(tmp_path, monkeypatch,
                             ["search", "--round", "4", "--max-rounds", "4",
                              "--top-k", "9"], stdin)
        assert code == 0
        assert out["round_index"] == 4 and out["max_rounds"] == 4

    def test_engine_dead_failopen(self, tmp_path, monkeypatch):
        e = FakeEngine(raise_at=0)
        code, out, _ = drive(tmp_path, monkeypatch, ["search"],
                             self._stdin(), e)
        assert code == 0
        assert out["decision"] == "unknown" and out["sufficiency"] is None


class TestTriageMailSupervise:
    def test_triage_happy_routes_today(self, tmp_path, monkeypatch):
        code, out, _ = drive(tmp_path, monkeypatch, ["triage"],
                             json.dumps({"message":
                                         "the customer cannot log in and "
                                         "is blocked on us, please help"}))
        assert code == 0
        assert out["route"] in ("now", "today", "queue", "ignore")

    def test_triage_secret_shaped_never_sent(self, tmp_path, monkeypatch):
        # §2 triage row: secret-shaped -> now, unsent, NO engine call
        e = FakeEngine()
        code, out, _ = drive(tmp_path, monkeypatch, ["triage"],
                             json.dumps({"message":
                                         "api_key: supersecret1234"}), e)
        assert code == 0
        assert out["route"] == "now" and "secret_shaped" in out["flags"]
        assert e.count == 0

    def test_triage_missing_message_exits_2(self, tmp_path, monkeypatch):
        code, out, _ = drive(tmp_path, monkeypatch, ["triage"], "{}")
        assert code == 2 and out["error"] == "invalid_request"

    def test_mail_from_subject_body_triple(self, tmp_path, monkeypatch):
        stdin = json.dumps({"from": "a@example.test", "subject": "hi",
                            "body": "coffee later?"})
        code, out, _ = drive(tmp_path, monkeypatch, ["mail"], stdin)
        assert code == 0
        assert "lane" in out and "needs_attention" in out

    def test_supervise_happy(self, tmp_path, monkeypatch):
        stdin = json.dumps({"no_output_s": 10, "nudge_streak": 0,
                            "question_pending": False,
                            "last_output": "step 1 done", "notes": ""})
        code, out, _ = drive(tmp_path, monkeypatch, ["supervise"], stdin)
        assert code == 0 and out["action"] == "keep_waiting"

    def test_supervise_dead_engine_never_escalates(self, tmp_path,
                                                   monkeypatch):
        e = FakeEngine(raise_at=0)
        code, out, _ = drive(tmp_path, monkeypatch, ["supervise"],
                             json.dumps({"no_output_s": 5}), e)
        assert code == 0
        assert out["action"] == "keep_waiting"   # §2: never escalates blind


# ---------------------------------------------------------------- ladder --
# §2 ladder row + spec §7 "No head": ladder NEVER builds the engine. Locked
# by pointing LAYA_ASSETS at a bogus path AND a tripwire that fails the
# test if _engine() is ever called.

class TestLadder:
    def _tripwire(self, monkeypatch):
        def never(*a, **k):
            raise AssertionError("ladder must not build the engine")
        monkeypatch.setattr(laya_cli, "_engine", never)
        monkeypatch.setenv("LAYA_ASSETS", "/nonexistent/bogus/assets")
        monkeypatch.setenv("LAYA_SOURCE", "/nonexistent/bogus/source")

    def test_all_subcommands_exit_0_without_engine(self, tmp_path,
                                                   monkeypatch):
        self._tripwire(monkeypatch)
        stdin = json.dumps({"rung": "big", "rungs": [
            {"name": "big", "kind": "model", "model": "p:big",
             "cooldown": 900}]})
        for argv, stdin_arg in ((["ladder", "choose", "--now", "1000"], stdin),
                                (["ladder", "status", "--now", "1000"], ""),
                                (["ladder", "refuse", "--now", "1000"], stdin),
                                (["ladder", "clear", "--now", "1000"], "")):
            code, out, err = drive(tmp_path, monkeypatch, argv, stdin_arg,
                                   fake_engine=False)
            assert code == 0, f"{argv} -> {code} {out} {err}"
            assert out is not None

    def test_choose_cooldown_via_now_seam(self, tmp_path, monkeypatch):
        self._tripwire(monkeypatch)
        stdin = json.dumps({"rung": "big", "rungs": [
            {"name": "big", "cooldown": 900}]})
        code, out, _ = drive(tmp_path, monkeypatch,
                             ["ladder", "choose", "--now", "1000"], stdin,
                             fake_engine=False)
        assert code == 0 and out["fire"] is True
        code, out, _ = drive(tmp_path, monkeypatch,
                             ["ladder", "choose", "--now", "1500"], stdin,
                             fake_engine=False)
        assert code == 0 and out["fire"] is False
        assert "cooldown" in out["reason"]
        code, out, _ = drive(tmp_path, monkeypatch,
                             ["ladder", "status", "--now", "1500"], "",
                             fake_engine=False)
        assert code == 0
        assert out["rungs"]["big"]["ready"] is False
        code, _, _ = drive(tmp_path, monkeypatch, ["ladder", "clear"], "",
                           fake_engine=False)
        assert code == 0
        code, out, _ = drive(tmp_path, monkeypatch,
                             ["ladder", "choose", "--now", "1500"], stdin,
                             fake_engine=False)
        assert out["fire"] is True                # cleared: ready again

    def test_choose_missing_rung_exits_2(self, tmp_path, monkeypatch):
        self._tripwire(monkeypatch)
        code, out, _ = drive(tmp_path, monkeypatch, ["ladder", "choose"],
                             json.dumps({"rungs": []}), fake_engine=False)
        assert code == 2 and out["error"] == "invalid_request"


# ------------------------------------------------------------------ plan --
# §2 plan row: the builder NEVER touches the network; --call goes to
# TEXT_MODEL via urllib ONLY with TEXT_MODEL + TEXT_MODEL_API_KEY set; any
# call/parse failure lands on the fallback, exit 0.

def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("plan build-only must not touch the network")
    monkeypatch.setattr(urllib.request, "urlopen", boom)


class TestPlan:
    def test_build_only_never_calls_network(self, tmp_path, monkeypatch):
        _no_network(monkeypatch)
        code, out, _ = drive(tmp_path, monkeypatch, ["plan"],
                             json.dumps({"command": "open notes and type hi",
                                         "goal": "write it down"}))
        assert code == 0
        assert out["status"] == "built"
        assert out["schema"]["strict"] is True
        assert len(out["messages"]) == 2

    def test_never_send_filter_is_a_rail(self, tmp_path, monkeypatch):
        _no_network(monkeypatch)
        code, out, _ = drive(tmp_path, monkeypatch, ["plan"],
                             json.dumps({"command":
                                         "open mail and send the reply"}))
        assert code == 0
        assert out["never_send_filtered"] is True
        assert "send" not in out["messages"][-1]["content"].lower()

    def test_call_without_key_falls_back_no_network(self, tmp_path,
                                                    monkeypatch):
        _no_network(monkeypatch)                # urlopen must NOT be called
        code, out, _ = drive(tmp_path, monkeypatch, ["plan", "--call"],
                             json.dumps({"command": "open notes"}))
        assert code == 0
        assert out["status"] == "fallback"
        assert out["steps"] == [{"step_id": "step_0", "kind": "goal"}]

    def test_call_success_parses_steps(self, tmp_path, monkeypatch):
        class Resp:
            payload = {"choices": [{"message": {"content": json.dumps(
                {"steps": [{"kind": "open_app", "target": "Notes"},
                          {"kind": "type_text", "target": "hi"}]})}}]}

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            return Resp()
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setenv("TEXT_MODEL", "some-text-model")
        monkeypatch.setenv("TEXT_MODEL_API_KEY", "k")
        code, out, _ = drive(tmp_path, monkeypatch, ["plan", "--call"],
                             json.dumps({"command": "open notes"}))
        assert code == 0
        assert out["status"] == "ok" and out["steps"][0]["kind"] == "open_app"
        assert seen["auth"] == "Bearer k"

    def test_call_network_failure_falls_back_exit_0(self, tmp_path,
                                                    monkeypatch):
        def boom(*a, **k):
            raise OSError("network down")
        monkeypatch.setattr(urllib.request, "urlopen", boom)
        monkeypatch.setenv("TEXT_MODEL", "m")
        monkeypatch.setenv("TEXT_MODEL_API_KEY", "k")
        code, out, _ = drive(tmp_path, monkeypatch, ["plan", "--call"],
                             json.dumps({"command": "open notes"}))
        assert code == 0
        assert out["status"] == "fallback"      # §2 fail-open column

    def test_missing_command_exits_2(self, tmp_path, monkeypatch):
        _no_network(monkeypatch)
        code, out, _ = drive(tmp_path, monkeypatch, ["plan"], "{}")
        assert code == 2 and out["error"] == "invalid_request"


# ------------------------------------------------------------- not_impl --

class TestNotImpl:
    @pytest.mark.parametrize("cmd", ["memo", "spend", "dashboard",
                                     "setup-key"])
    def test_not_impl_exits_0_with_hint(self, tmp_path, monkeypatch, cmd):
        # these commands belong to LATER tickets; today they answer
        # honestly {"status":"not_impl"} exit 0, never a fake success
        code, out, _ = drive(tmp_path, monkeypatch, [cmd], "")
        assert code == 0
        assert out["status"] == "not_impl" and out["hint"]


# ---------------------------------------------------------------- doctor --

class TestDoctor:
    def test_no_key_exits_0_without_flag(self, tmp_path, monkeypatch):
        code, out, _ = drive(tmp_path, monkeypatch, ["doctor"], "")
        assert code == 0
        assert out["key"]["present"] is False
        assert "warnings" in out and "assets" in out

    def test_need_key_without_key_exits_1(self, tmp_path, monkeypatch):
        code, out, err = drive(tmp_path, monkeypatch,
                               ["doctor", "--need-key"], "")
        assert code == 1                        # §2: exit 1 ONLY here
        assert out["key"]["present"] is False

    def test_need_key_with_key_exits_0(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAYA_API_KEY", "sk-test-value")
        code, out, _ = drive(tmp_path, monkeypatch,
                             ["doctor", "--need-key"], "")
        assert code == 0
        assert out["key"]["present"] is True
        # describe() shape: length only, the key itself NEVER appears
        assert out["key"]["length"] == len("sk-test-value")
        assert "sk-test-value" not in json.dumps(out)

    def test_buildable_report_without_building(self, tmp_path, monkeypatch):
        def never(*a, **k):
            raise AssertionError("doctor must not build the engine")
        monkeypatch.setattr(laya_cli, "_engine", never)
        assets = tmp_path / "assets"
        (assets / "laya-combined-f16.aimodel").mkdir(parents=True)
        source = tmp_path / "source"
        source.mkdir()
        monkeypatch.setenv("LAYA_ASSETS", str(assets))
        monkeypatch.setenv("LAYA_SOURCE", str(source))
        code, out, _ = drive(tmp_path, monkeypatch, ["doctor"], "",
                             fake_engine=False)
        assert code == 0
        assert out["engine"] == "buildable"
        assert "laya-combined-f16.aimodel" in out["assets"]


# ----------------------------------------------------- argparse contract --

class TestArgvContract:
    def test_unknown_subcommand_exits_2(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        code = main(["frobnicate"])
        assert code == 2
        assert capsys_read()[1]                 # usage note on stderr

    def test_unknown_flag_exits_2(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        code = main(["route", "--bogus-flag", "x"])
        assert code == 2

    def test_no_traceback_on_stdout_when_primitive_blows_up(
            self, tmp_path, monkeypatch):
        # a bug in a primitive is still ONE JSON doc + exit 2; the
        # traceback goes to stderr ONLY (§2)
        def explode(*a, **k):
            raise ZeroDivisionError("boom")
        monkeypatch.setattr(laya_cli, "_engine",
                            lambda: FakeEngine())
        monkeypatch.setattr(laya_cli.U, "route_turn", explode)
        monkeypatch.setattr(sys, "stdin",
                            io.StringIO(json.dumps({"turn": "x y z"})))
        code = main(["route"])
        out_line, err = capsys_read()
        assert code == 2
        assert json.loads(out_line)["error"] == "internal_error"
        assert "Traceback" in err


# ------------------------------------------------- engine build discipline --

class TestEngineBuild:
    def test_engine_built_once_per_process(self, tmp_path, monkeypatch):
        builds = []

        class CountingEngine:
            def __init__(self):
                builds.append(1)

            def question(self, name, q, state):
                raise RuntimeError("dead")       # degraded answer, exit 0

        fake_mod = types.ModuleType("engine")
        fake_mod.Engine = CountingEngine
        monkeypatch.setitem(sys.modules, "engine", fake_mod)
        monkeypatch.setattr(laya_cli, "_ENGINE", None, raising=False)
        monkeypatch.setattr(sys, "stdin",
                            io.StringIO(json.dumps({"message":
                                                    "some normal message "
                                                    "here today friend?"})))
        assert main(["triage"]) == 0
        assert builds == [1]
        assert laya_cli._engine() is laya_cli._engine()
        assert len(builds) == 1                  # memoized: ONE build

    def test_missing_asset_degrades_not_crashes(self, tmp_path, monkeypatch):
        # A REAL missing asset: the engine module's Engine() raises (this
        # simulates engine.py's "engine load failed" exactly — the real
        # constructor is not run in unit tests: it blocks on its worker
        # probe). The §2 contract locked here: a fail-open question cmd
        # answers degraded exit 0, never a traceback on stdout.
        class NoAssetEngine:
            def __init__(self):
                raise RuntimeError("engine load failed: no such file "
                                   "laya-combined-f16.aimodel")

        fake_mod = types.ModuleType("engine")
        setattr(fake_mod, "Engine", NoAssetEngine)
        monkeypatch.setitem(sys.modules, "engine", fake_mod)
        monkeypatch.setattr(laya_cli, "_ENGINE", None, raising=False)
        monkeypatch.setenv("LAYA_ASSETS", "/nonexistent/bogus/assets")
        monkeypatch.setattr(sys, "stdin",
                            io.StringIO(json.dumps({"message":
                                                    "a plain note today"})))
        code = main(["triage"])
        out_line, err = capsys_read()
        assert code == 0
        assert json.loads(out_line)["status"] == "fail_open"
        assert "Traceback" not in out_line


# ------------------------------------------------------------------ bin --
# The dual binary names (D6). Read TEXTUALLY — unit tests never spawn a
# shell. bin/jev must be a one-line shim exec'ing bin/laya with "$@": the
# alias lives at the BINARY level, so both names run the identical CLI with
# identical stdin/stdout JSON contracts.

class TestBinShims:
    def test_bin_jev_is_a_one_line_shim_over_bin_laya(self):
        text = (_ROOT / "bin" / "jev").read_text()
        exec_lines = [ln for ln in text.splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#")]
        assert len(exec_lines) == 1, text
        line = exec_lines[0].strip()
        assert line.startswith("exec")
        assert '"$(dirname "$0")/laya"' in line     # calls bin/laya itself
        assert '"$@"' in line                       # passes every arg thru

    def test_bin_laya_launches_the_cli(self):
        text = (_ROOT / "bin" / "laya").read_text()
        assert text.startswith("#!/bin/sh")
        assert "${LAYA_PYTHON:-python3}" in text
        assert "../server/laya_cli.py" in text
        assert '"$@"' in text

    def test_both_bins_are_executable(self):
        for name in ("laya", "jev"):
            p = _ROOT / "bin" / name
            assert os.access(p, os.X_OK), f"bin/{name} is not executable"
