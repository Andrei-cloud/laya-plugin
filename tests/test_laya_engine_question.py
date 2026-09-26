"""Unit tests for the Engine wire-question path (T1.2–T1.4 support):
question() shape building, chain-rule slate matching, _ask error mapping.
The real Core AI agent is FAKED here (hermetic, fast); the same shapes were
additionally probed against the live asset (see commit notes / probe script).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))


class FakeAgent:
    """Stands in for CombinedAgent: echoes what task/question it was fed."""
    chain_order = ["base", "ft_lang", "ft_gr14_ppo", "ft_tool5v2_ppo",
                   "ft_skill4_ppo"]
    L = 64

    def __init__(self):
        self.route = {"triage": 0, "lang_route": 1, "guardrail": 2,
                      "act_escalate": 2, "tool_route": 3, "skill_route": 4,
                      "base": 0}
        self.last = None

    def decide(self, task, state, question, pad_to=None):
        (name, q), = question.items()
        self.last = {"task": task, "state": state, "q": q, "pad_to": pad_to}
        names = list(q["criteria"])
        probs = {n: 1.0 / len(names) for n in names}
        return {"task": task, "chain": self.chain_order[self.route[task]],
                "choice": names[0], "confidence": probs[names[0]],
                "probs": probs, "act_p": 0.5, "acted": True}


@pytest.fixture()
def engine(monkeypatch):
    """A real Engine object whose worker drives FakeAgent instead of Core AI:
    patch _build_agent by pre-seeding the singleton queue path is fragile —
    instead we bypass __init__ entirely and hand-wire what _ask needs."""
    from server import engine as eng_mod
    cls = eng_mod.Engine
    # Fresh instance per test: Engine is a PROCESS SINGLETON, so reset the
    # slot before AND after — otherwise a test that stubs _submit leaks its
    # stub into every later test (observed: full-suite failure, solo pass).
    cls._singleton = None
    e = cls.__new__(cls)           # skip __init__ (no asset load)
    import queue
    e._initialized = True
    e._q = queue.Queue()
    e.chain_order = FakeAgent.chain_order
    e.Lmax = 64
    e.tool_slate = ["read_file", "terminal", "browser"]
    e.skill_slate = ["pdf", "docx"]
    e.calls = 0
    e.total_ms = 0.0
    agent = FakeAgent()

    import threading

    def _worker():
        while True:
            job = e._q.get()
            if job is None:
                break
            fn, fut = job
            try:
                cls._set_result(fut, fn(agent))
            except Exception as ex:
                cls._set_result(fut, ("__error__", f"{type(ex).__name__}: {ex}"))
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    setattr(e, "_fake", agent)   # tests may inspect what the fake was fed
    yield e
    e._q.put(None)
    t.join(timeout=5)
    cls._singleton = None        # leave no stubbed singleton behind (see above)


class TestQuestionShapes:
    def test_noul_builds_yes_no_choice_on_base(self, engine):
        out = engine.question("q1", {"type": "noul",
                                     "instructions": "is it relevant?"}, "st")
        assert engine.calls == 1
        assert out["chain"] == "base"
        assert set(out["probs"]) == {"no", "yes"}

    def test_score_builds_level_named_choice(self, engine):
        out = engine.question("d", {"type": "score",
                                    "instructions": "rate difficulty",
                                    "criteria": ["Trivial", "Moderate",
                                                 "Hard", "Expert"]}, "st")
        assert out["chain"] == "base"
        assert set(out["probs"]) == {"0", "1", "2", "3"}

    def test_choice_foreign_slate_rides_chain0(self, engine):
        out = engine.question("a", {"type": "choice", "instructions": "pick",
                                    "criteria": {"x": "x?", "y": "y?"}}, "st")
        assert out["chain"] == "base"

    def test_choice_guardrail_vocabulary_routes_guardrail_head(self, engine):
        out = engine.question("a", {"type": "choice", "instructions": "pick",
                                    "criteria": {"allow": "a", "ask_user": "b",
                                                 "block": "c"}}, "st")
        assert out["chain"] == "ft_gr14_ppo"      # chain 2

    def test_choice_tool_slate_routes_tool_head(self, engine):
        out = engine.question("t", {"type": "choice", "instructions": "pick",
                                    "criteria": {"read_file": "",
                                                 "terminal": "",
                                                 "browser": ""}}, "st")
        assert out["chain"] == "ft_tool5v2_ppo"   # chain 3

    def test_choice_skill_slate_routes_skill_head(self, engine):
        out = engine.question("s", {"type": "choice", "instructions": "pick",
                                    "criteria": {"pdf": "", "docx": ""}}, "st")
        assert out["chain"] == "ft_skill4_ppo"    # chain 4

    def test_route_task_hint_wins_over_heuristic(self, engine):
        out = engine.question("a", {"type": "choice", "instructions": "pick",
                                    "route_task": "base",
                                    "criteria": {"allow": "a",
                                                 "ask_user": "b",
                                                 "block": "c"}}, "st")
        assert out["chain"] == "base"

    def test_route_task_unknown_family_falls_back_not_raises(self, engine):
        """A hint for a head this asset does not carry (e.g. mail_sort before
        ft_mail ships) must degrade to the slate heuristic — the agent's
        route dict KeyErrors on unknown families and the API layer launders
        that into a misleading 529 'overloaded'."""
        out = engine.question("lane", {"type": "choice", "instructions": "pick",
                                       "route_task": "mail_sort",
                                       "criteria": {"needs_reply": "a",
                                                    "updates": "b",
                                                    "promotional": "c",
                                                    "sales": "d",
                                                    "spam": "e"}}, "st")
        assert out["chain"] == "base"          # foreign slate -> chain 0

    def test_unknown_type_raises(self, engine):
        with pytest.raises(ValueError):
            engine.question("a", {"type": "ranking", "instructions": "?"}, "st")

    def test_pad_to_asset_shape(self, engine):
        engine.question("q1", {"type": "noul", "instructions": "?"}, "st")
        assert getattr(engine, "_fake").last["pad_to"] == 64  # asset L_max
        assert engine.calls == 1


class TestAskErrorMapping:
    def test_worker_error_raises_runtimeerror(self, engine, monkeypatch):
        # Force the next _submit to return the error tuple.
        engine._submit = lambda fn, timeout=120: ("__error__", "ANECompiler FAILED")
        with pytest.raises(RuntimeError, match="ANECompiler FAILED"):
            engine._ask("base", "st", {"q": {"type": "choice",
                                             "instructions": "?",
                                             "criteria": {"a": "a", "b": "b"}}})

    def test_junk_submit_result_raises_runtimeerror(self, engine):
        engine._submit = lambda fn, timeout=120: ("unexpected",)
        with pytest.raises(RuntimeError):
            engine._ask("base", "st", {"q": {"type": "choice",
                                             "instructions": "?",
                                             "criteria": {"a": "a", "b": "b"}}})

    def test_calls_not_counted_on_error(self, engine):
        engine._submit = lambda fn, timeout=120: ("__error__", "boom")
        before = engine.calls
        with pytest.raises(RuntimeError):
            engine._ask("base", "st", {"q": {"type": "choice",
                                             "instructions": "?",
                                             "criteria": {"a": "a", "b": "b"}}})
        assert engine.calls == before


class TestLegacyDecideStillWorks:
    def test_decide_goes_through_same_path(self, engine):
        out = engine.decide("guardrail", "rm -rf /")
        assert out["chain"] == "ft_gr14_ppo"
        assert set(out["probs"]) == {"allow", "ask_user", "block"}
        assert engine.calls == 1


class TestAuditLine:
    """LAYA_AUDIT per-decision JSONL: the daemon runs with it set
    (LaunchAgent), and question()-path state is a DICT envelope —
    `state[:300]` on a dict raised KeyError(slice) and 500'd every
    audited question-API request while /health stayed green. Regression:
    audit must work for dict AND str states, truncate the serialized
    string, and never break the answer."""

    def _audit_lines(self, path):
        import json as _json
        with open(path) as f:
            return [_json.loads(l) for l in f if l.strip()]

    def test_dict_state_audits_without_crash(self, engine, tmp_path, monkeypatch):
        audit = tmp_path / "a.jsonl"
        monkeypatch.setenv("LAYA_AUDIT", str(audit))
        out = engine.question("g", {"type": "noul",
                                    "instructions": "rm -rf / ?"},
                              {"session": "x", "turn": 2})
        assert out["choice"]
        (rec,) = self._audit_lines(audit)
        assert rec["state"] == '{"session": "x", "turn": 2}'   # serialized
        assert rec["choice"] == out["choice"]

    def test_str_state_audits_and_truncates(self, engine, tmp_path, monkeypatch):
        audit = tmp_path / "b.jsonl"
        monkeypatch.setenv("LAYA_AUDIT", str(audit))
        engine.decide("guardrail", "x" * 900, "kill it?")
        (rec,) = self._audit_lines(audit)
        assert len(rec["state"]) == 300                        # truncation
        assert rec["state"].startswith("xxxx")

    def test_audit_failure_does_not_swallow_answer(self, engine, monkeypatch):
        # unwritable audit path -> question() must still answer (fail-open
        # on observability, never on the decision itself)... or raise only
        # if the engine chose to; the CONTRACT is: no KeyError(slice).
        monkeypatch.setenv("LAYA_AUDIT", "/nonexistent-dir/a.jsonl")
        with pytest.raises(OSError):        # plain file error, not KeyError
            engine.question("g", {"type": "noul", "instructions": "?"},
                            {"a": 1})


class TestInitBarrier:
    """M2-drill regression: the singleton init barrier. A failed build must
    (a) propagate to the caller, (b) clear the slot so the next Engine()
    re-attempts the build, and (c) never leave a silently-"built" zombie
    that every later caller would adopt."""

    def _fresh(self, monkeypatch):
        from server import engine as eng_mod
        eng_mod.Engine._singleton = None          # clean slot
        return eng_mod.Engine

    def test_failed_build_raises_and_clears_slot(self, monkeypatch):
        cls = self._fresh(monkeypatch)

        def boom(self):
            raise RuntimeError("asset missing")
        monkeypatch.setattr(cls, "_build", boom)
        monkeypatch.delenv("LAYA_ASSETS", raising=False)
        with pytest.raises(RuntimeError) as ei:
            cls()
        assert "asset missing" in str(ei.value)
        assert cls._singleton is None            # slot cleared, no zombie

    def test_adopted_instance_gets_the_builders_error(self, monkeypatch):
        import threading
        cls = self._fresh(monkeypatch)
        started, done = threading.Event(), threading.Event()

        def slow_boom(self):
            started.set()
            done.wait(5)
            raise RuntimeError("late failure")
        monkeypatch.setattr(cls, "_build", slow_boom)
        err = {}

        def first():
            try:
                cls()
            except BaseException as e:
                err["first"] = e
        t = threading.Thread(target=first)
        t.start()
        started.wait(5)
        try:
            cls2 = cls()                          # adopts, waits for builder
            err["second"] = None
        except BaseException as e:
            err["second"] = e
        done.set()
        t.join(10)
        assert isinstance(err.get("first"), RuntimeError)
        assert isinstance(err["second"], RuntimeError) and "late failure" in str(err["second"])
        assert cls._singleton is None
