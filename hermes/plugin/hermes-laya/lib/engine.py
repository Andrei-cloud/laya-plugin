"""Shared decision engine: loads the combined Core AI asset ONCE per process
and serves typed decisions with the SAME semantics the heads were trained on
(question shapes copied from the training corpus — deviating changes the
distribution and invalidates the heads' calibration).

Env:
  LAYA_ASSETS  dir containing laya-combined-f16.aimodel/ + combined_provenance.json  (required)
  LAYA_SOURCE  configs dir (release/configs) with tokenizer/ under it, or a
               models/source dir (rl_agent_config.json + tokenizer/)      (required)
  LAYA_UNIT    gpu (default) | cpu | ne
"""
import json, os, sys, time

CORE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core")
if CORE not in sys.path:
    sys.path.insert(0, CORE)

GUARDRAIL_QUESTION = {
    "disposition": {
        "type": "choice",
        "instructions": "How should the harness treat the action in `state`?",
        "criteria": {
            "allow": "safe to run as-is",
            "ask_user": "possible risk, confirm with the operator first",
            "block": "refuse: destructive or policy-violating",
        },
    }
}
TRIAGE_QUESTION = {
    "action": {
        "type": "choice",
        "instructions": "Should the agent reply to the user or take an action (tool call)?",
        "criteria": {"reply": "Just respond to the user in text.",
                     "act": "Invoke a tool / take an action."},
    }
}
LANG_QUESTION = {
    "lang": {
        "type": "choice",
        "instructions": "What is the primary language of the request in the state?",
        "criteria": {l: f"The request is written in {l}." for l in
                     ["English", "Russian", "German", "French", "Spanish",
                      "Italian", "Portuguese", "Chinese", "Japanese", "Korean"]},
    }
}

# Task families this engine's question_for() can build (plus the chain-0
# pseudo-task "base" the worker registers). route_task hints outside this
# set fall back to the slate heuristic instead of KeyErrors in the agent.
_SUPPORTED_TASKS = {"guardrail", "act_escalate", "triage", "lang_route",
                    "tool_route", "skill_route", "base"}

# Families whose BINARY questions were TRAINED as choice {no,yes} (their
# corpus criteria are literally {"no":"no","yes":"yes"}): the wire must keep
# that rendering. Everything else — triage/mail/guardrail/skill and the
# chain-0 base — trained binaries as NATIVE noul (options "false: …"/
# "true: …"). The qtype embedding is a trained input; rendering one dialect
# in the other's shape flips poles (r36: tri-012 P(deadline) 0.022 native vs
# 0.842 rewritten). Corpus-derived source of truth: laya_port/combined.py
# BINARY_NOUL_TASKS (kept in sync by tests/test_laya_engine_question.py).
_CHOICE_BINARY_TASKS = frozenset({"supervise", "rerank"})

# Route keys the LOADED asset reports (set per-instance from the warmup
# probe). Class-level default keeps the pre-probe semantics for any object
# built without _build (unit fixtures): hints are then clamped exactly as
# before the chain expansion.
_ENGINE_ROUTES_DEFAULT = frozenset()


class Engine:
    """ONE dedicated worker thread drives the agent (invariant from
    coreai_agent.py: one wrapper instance == one event loop == one compute-unit
    session). Cross-thread run_until_complete costs ~3.4 s/call; same-thread
    calls are milliseconds. All calls serialize on the worker.

    PROCESS SINGLETON: Engine() always returns the same instance — a second
    Engine means a second Core AI session and a second ~8 s load (observed
    defect: /health rebuilt the engine on every poll)."""

    _singleton = None
    _slock = None
    # class-level default so objects built without _build (unit fixtures)
    # clamp route_task exactly as before the chain expansion
    _routes = _ENGINE_ROUTES_DEFAULT

    def __new__(cls):
        import threading
        if cls._slock is None:
            cls._slock = threading.Lock()
        with cls._slock:
            if cls._singleton is None:
                inst = super().__new__(cls)
                inst._initialized = False
                # init barrier: concurrent callers ADOPT the instance and
                # wait for ONE build; failure clears the slot so no caller
                # ever adopts a half-built, silently-"built" engine
                # (M2-drill finding: zombie singleton).
                inst._ready = threading.Event()
                inst._init_exc = None
                inst._claimed = False
                cls._singleton = inst
            return cls._singleton

    def __init__(self):
        if self._initialized:
            return
        import threading
        cls = type(self)
        # getattr defaults: fixtures that bypass __init__ hand-wire the
        # object and may not know about barrier attrs.
        with cls._slock:
            owner = not getattr(self, "_claimed", False)
            if owner:
                self._claimed = True
        if not owner:
            # another thread is mid-build: wait for ITS outcome
            ready = getattr(self, "_ready", None)
            if ready is None or not ready.wait(660):
                raise RuntimeError("engine load still in progress")
            if self._init_exc is not None:
                raise RuntimeError(f"engine load failed: {self._init_exc}")
            return
        try:
            self._build()
        except BaseException as exc:
            self._init_exc = exc
            with cls._slock:
                if cls._singleton is self:
                    cls._singleton = None      # never adopt a zombie
            raise
        finally:
            self._ready.set()

    def _build(self):
        import queue
        import threading
        assets = os.environ["LAYA_ASSETS"]
        source = os.environ.get("LAYA_SOURCE") or os.path.join(assets, "configs")
        unit = os.environ.get("LAYA_UNIT", "gpu")
        self._q: "queue.Queue" = queue.Queue()
        # (Core AI's load prints ~42 BENIGN ANE-validation warning lines to
        # fd1/stdout; the MCP server silences fd1 around load+warmup because
        # there stdout IS the JSON-RPC channel. Engine itself leaves fds.)

        def _build_agent():
            from laya_port.combined_agent import CombinedAgent
            agent = CombinedAgent(os.path.join(assets, "laya-combined-f16.aimodel"),
                                  source, unit=unit)
            # chain-0 task name for foreign/wire slates (chain rule: trained
            # slates ride their FT head, anything else rides base).
            agent.route.setdefault("base", 0)
            return agent

        def _worker():
            import asyncio
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                agent = _build_agent()
            except BaseException as e:
                # Build failed (missing numpy/assets/env): the probe MUST
                # get an error NOW — if the thread just died the warmup
                # probe would wait its full 600 s before failing the
                # startup (M2 drill: KeyError-free env -> 10-min stall).
                msg = f"{type(e).__name__}: {e}"
                self._q.put(None)  # keep the queue drainable
                first = self._q.get()
                if first is not None:
                    fn, fut = first
                    self._set_result(fut, ("__error__", msg))
                return
            while True:
                try:
                    job = self._q.get()
                    if job is None:
                        break
                    fn, fut = job
                    try:
                        self._set_result(fut, fn(agent))
                    except Exception as e:  # surface in caller
                        msg = f"{type(e).__name__}: {e}"
                        # Core AI can wedge under GPU/ANE contention
                        # (ANECompiler FAILED). Rebuild the agent so ONE bad
                        # compile doesn't poison every later call; the failed
                        # caller still gets a clean error (fail-open upstream).
                        if any(k in msg for k in ("ANE", "coreai", "CoreAI",
                                                  "mpsgraph", "Metal")):
                            try:
                                agent = _build_agent()
                                msg += " [agent rebuilt for next call]"
                            except Exception as e2:
                                msg += f" [AGENT REBUILD FAILED: {e2}]"
                        self._set_result(fut, ("__error__", msg))
                except BaseException as e:
                    # The thread must NEVER die silently: a dead worker hangs
                    # every later _submit (the exact "warm daemon stall").
                    sys.stderr.write(f"[laya-worker] survived {type(e).__name__}: {e}\n")
        self._thread = threading.Thread(target=_worker, daemon=True, name="laya-worker")
        self._thread.start()
        # block until the agent is actually loaded (first queued job is a probe)
        def _warm(a):
            # first call per SHAPE costs ~3-7 s (Core AI graph specialization);
            # pad every call to L_max and pay it ONCE here, at startup.
            q = {"disposition": {"type": "choice",
                                 "instructions": "How should the harness treat the action in `state`?",
                                 "criteria": {"allow": "safe to run as-is",
                                              "ask_user": "possible risk, confirm with the operator first",
                                              "block": "refuse: destructive or policy-violating"}}}
            a.decide("guardrail", state="warmup", question=q, pad_to=a.L)
            return a.chain_order, a.L, sorted(a.route)
        probe = self._submit(_warm, timeout=600)
        if isinstance(probe, tuple) and probe and probe[0] == "__error__":
            raise RuntimeError(f"engine load failed: {probe[1]}")
        self.chain_order, self.Lmax = list(probe[0]), probe[1]
        # route keys the LOADED asset actually carries (DEFAULT_ROUTE +
        # whatever chains were added). A route_task hint naming one of
        # these is honored even outside _SUPPORTED_TASKS (the static set
        # predates the mail/supervise/choose/compact/rerank chains and
        # must not silently drop their questions to chain-0); a hint for a
        # chain this asset lacks still degrades to the slate heuristic —
        # the agent's route dict would KeyError (laundered as 529).
        self._routes = set(probe[2]) if len(probe) > 2 else set()
        self.tool_slate = json.loads(os.environ.get("LAYA_TOOL_SLATE", "[]"))
        self.skill_slate = json.loads(os.environ.get("LAYA_SKILL_SLATE", "[]"))
        self.calls = 0
        self.total_ms = 0.0
        self._initialized = True   # ONLY after the probe answered —
        # adopters in __init__ treat this as "fully built"

    @staticmethod
    def _set_result(fut, value):
        import threading
        fut["value"] = value
        fut["event"].set()

    def _submit(self, fn, timeout=120):
        import threading
        fut = {"event": threading.Event(), "value": None}
        self._q.put((fn, fut))
        # BOUNDED wait: a wedged/dead worker must yield an error to the caller
        # (MCP layer turns it into a fail-open fallback), never freeze the
        # harness tool call forever.
        if not fut["event"].wait(timeout):
            return ("__error__", f"engine timeout after {timeout}s (worker wedged?)")
        return fut["value"]

    # ---- question builders (shapes verified against training corpus) ----
    def question_for(self, task, options=None):
        if task == "guardrail":
            return GUARDRAIL_QUESTION
        if task in ("act_escalate",):
            return GUARDRAIL_QUESTION
        if task == "triage":
            return TRIAGE_QUESTION
        if task == "lang_route":
            return LANG_QUESTION
        if task == "tool_route":
            slate = options or self.tool_slate
            if not slate:
                raise ValueError("tool_route needs options (or LAYA_TOOL_SLATE)")
            return {"tool": {"type": "choice",
                             "instructions": "Given `state`, which single tool should serve the request?",
                             "criteria": {t: (d if isinstance(d, str) and d else f"Call the {t} tool.")
                                          for t, d in (slate.items() if isinstance(slate, dict)
                                                       else [(t, "") for t in slate])}}}
        if task == "skill_route":
            slate = options or self.skill_slate
            if not slate:
                raise ValueError("skill_route needs options (or LAYA_SKILL_SLATE)")
            return {"skill": {"type": "choice",
                              "instructions": "Which skill should be loaded for `state`?",
                              "criteria": {t: (d if isinstance(d, str) and d else f"Use the {t} skill.")
                                           for t, d in (slate.items() if isinstance(slate, dict)
                                                        else [(t, "") for t in slate])}}}
        raise ValueError(f"unknown task {task}")

    # ---- generic wire question (chain rule; used by the question API) ----
    def _ask(self, task, state, question):
        """ONE submit-decode-report path (DRY): run a built question dict
        through the worker and shape the engine result. Error tuples from
        the bounded _submit raise RuntimeError (callers fail open upstream);
        stats/audit stay on the Engine, not the agent."""
        t0 = time.perf_counter()

        def work(agent):
            return agent.decide(task, state=state, question=question,
                                pad_to=self.Lmax)

        d = self._submit(work)
        if not isinstance(d, dict):  # error tuple ("__error__", msg) or junk —
            msg = d[1] if isinstance(d, tuple) and len(d) == 2 else repr(d)  # narrows
            raise RuntimeError(msg)   # to dict below for Pyright AND runtime
        ms = (time.perf_counter() - t0) * 1000
        self.calls += 1
        self.total_ms += ms
        out = {"task": d["task"], "chain": d["chain"], "choice": d["choice"],
               "confidence": round(d["confidence"], 4), "acted": bool(d["acted"]),
               "act_p": round(d["act_p"], 4),
               "probs": {k: round(v, 4) for k, v in d["probs"].items()},
               "latency_ms": round(ms, 2)}
        audit = os.environ.get("LAYA_AUDIT")
        if audit:  # per-decision JSONL for the benchmark harness
            import threading
            # state is a str on the chat path but the dict envelope on the
            # question API — state[:300] on a dict raised KeyError(slice)
            # and 500'd every audited question-API request. Serialize
            # first, truncate the *string*, never the object.
            state_repr = state if isinstance(state, str) else json.dumps(
                state, ensure_ascii=False, sort_keys=True)
            with threading.Lock():
                with open(audit, "a") as f:
                    import time as _t
                    f.write(json.dumps({"ts": round(_t.time(), 3), **out,
                                        "state": state_repr[:300]}) + "\n")
        return out

    def question(self, name, q, state):
        """Run ONE wire question ({type: noul|choice|score, instructions,
        criteria}) through the combined asset and return the raw engine
        result {task, chain, choice, confidence, acted, act_p, probs,
        latency_ms}. laya_ops converts it to the wire answer.

        Chain rule (BTZSC-proven): a slate that matches a trained head
        routes to that head; anything foreign rides chain-0 (base), whose
        zero-shot on foreign slates beat every FT head (AG 0.82/Emo 0.30).
        Matching here is by task-family of the caller-provided chain hint;
        the wire itself carries no head ids — usecases pass `route_task`
        when they know better, else heuristics below, else base."""
        qtype = q.get("type")
        hint = q.get("route_task")
        # a hint for a chain THIS asset carries is valid even when the
        # static _SUPPORTED_TASKS predates the chain (expansion-safe);
        # anything else keeps the old clamp semantics.
        supported = hint in self._routes if hint else False
        if qtype == "noul":
            # r36 DIALECT FIX: binary questions were trained in TWO
            # dialects — native noul (options "false: …"/"true: …") for
            # triage/mail/guardrail/skill/base, and choice {no,yes} for
            # supervise/rerank. The qtype embedding is a trained input:
            # rendering a native-noul family as choice {no,yes} flips its
            # poles (tri-012: P(deadline) 0.022 -> 0.842). Pick the
            # dialect from the family hint; foreign/no hint rides base =
            # native dialect (chain-0 was pretrained native-noul).
            if hint in _CHOICE_BINARY_TASKS and (hint in self._routes or not self._routes):
                qb = {"type": "choice", "instructions": q["instructions"],
                      "criteria": {"no": "no", "yes": "yes"}}
            else:
                # native dialect: criteria LEFT ABSENT — combined_agent
                # renders the training-time option strings ("false: no, the
                # statement does not hold"/"true: yes, the statement
                # holds"); passing explicit {"false":…,"true":…} texts would
                # render a THIRD, never-trained string.
                qb = {"type": "noul", "instructions": q["instructions"]}
            # noul used to pin task="base" unconditionally — that kept
            # ft_sup3's noul head unreachable on the wire even after its
            # chain shipped. With a carried hint the noul question rides
            # its family chain; without one, base as always.
            task = hint if supported else "base"
        elif qtype == "score":
            levels = q["criteria"]
            qb = {"type": "choice", "instructions": q["instructions"],
                  "criteria": {str(i): f"{lv} (level {i} of {len(levels) - 1})"
                               for i, lv in enumerate(levels)}}
            # score rode "base" unconditionally; with a carried family hint
            # (triage/mail urgency were TRAINED on this exact rendering)
            # the question belongs on the family chain.
            task = hint if supported else "base"
        elif qtype == "choice":
            qb = {"type": "choice", "instructions": q["instructions"],
                  "criteria": {k: (v if isinstance(v, str) and v else f"option {k}")
                               for k, v in q["criteria"].items()}}
            task = hint or self._match_slate(qb["criteria"])
            # A hint for a head this asset does not carry must not KeyError
            # through the agent (laundered as 529 by the API layer): fall
            # back to the slate heuristic — foreign slates ride chain-0
            # (base) per the chain rule. Families here MUST mirror
            # question_for() + the chain-0 pseudo-task "base" — or the
            # loaded asset must report the route key itself (_routes from
            # the warmup probe: expansion-safe for chains added later).
            if task not in _SUPPORTED_TASKS and not supported:
                task = self._match_slate(qb["criteria"])
        else:
            raise ValueError(f"unknown question type {qtype!r}")
        return self._ask(task, state, {name: qb})

    def _match_slate(self, criteria):
        """Heuristic slate→head matcher for choice questions whose task the
        caller did not pin. Exact-set match against the deployed heads'
        training slates (from provenance); foreign slates → 'base'."""
        names = set(criteria)
        for task, slate in (("tool_route", self.tool_slate),
                            ("skill_route", self.skill_slate)):
            if slate and names == set(slate):
                return task
        # guardrail/triage/lang use fixed 3/2/7-criteria vocabularies
        if names == {"allow", "ask_user", "block"}:
            return "guardrail"
        if names == {"reply", "act"}:
            return "triage"
        # ft_lang was trained on exactly this 7-ISO-code slate (ft/train.jsonl
        # lang_route); the matcher once omitted it and lang rode chain-0 at
        # chance — silent starvation class: derive from the heads present.
        if names == {"ar", "en", "es", "hi", "ja", "ru", "zh"}:
            return "lang_route"
        return "base"

    def decide(self, task, state, options=None):
        # Legacy typed-task path: build the trained question shape, then ride
        # the SAME submit-decode-report path as wire questions (DRY).
        q = self.question_for(task, options)
        return self._ask(task, state, q)


# --------------------------------------------------------------------------
# The verdict rail (ONE definition — DRY; mirrored client-side in
# clients/opencode/laya-decision.ts which documents keeping in sync).
# Safety semantics: the harness may ACT only on "allow" at confidence
# >= GUARD_ACT_CONFIDENCE; everything else needs human confirmation.
# 0.7 is the deployed threshold (matches engine `acted` = conf>=0.7 from
# the r15 heads and the test_core red-line); the floor MUST NOT be loosened
# without a bench round — "rm -rf / must never pass" is canary-verified.
# --------------------------------------------------------------------------

GUARD_ACT_CONFIDENCE = 0.7


def guardrail_verdict(choice, confidence, floor=None):
    """Map a guardrail disposition to a harness verdict.
    block -> deny; ask_user or low-confidence -> confirm; else allow."""
    floor = GUARD_ACT_CONFIDENCE if floor is None else float(floor)
    if choice == "block":
        return "deny"
    if choice == "ask_user" or confidence < floor:
        return "confirm"
    return "allow"


def ask_guardrail(engine, command, floor=None):
    """One guardrail question + the verdict rail, over the generic wire
    question path (chain rule picks ft_gr14_ppo for the trained vocabulary).
    Returns {choice, confidence, verdict, latency_ms, chain}."""
    q = {"type": "choice",
         "instructions": "How should the harness treat the action in `state`?",
         "criteria": {"allow": "safe to run as-is",
                      "ask_user": "possible risk, confirm with the operator "
                                  "first",
                      "block": "refuse: destructive or policy-violating"}}
    raw = engine.question("disposition", q, f"About to run: {command}"[:900])
    conf = float(raw["confidence"])
    return {"choice": raw["choice"], "confidence": conf,
            "verdict": guardrail_verdict(raw["choice"], conf, floor),
            "latency_ms": raw["latency_ms"], "chain": raw["chain"]}
