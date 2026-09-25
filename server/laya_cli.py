"""Dual-named decision CLI — ``laya <cmd>`` / ``jev <cmd>`` (spec-v2 §2 CLI
ops table — THE contract; plan D6 CLI row + D7).

The CLI is THIN by design: parse stdin JSON → call the ONE primitive in
``laya_usecases`` (imported as ``U``) → print ONE JSON document on stdout →
return the §2 exit code. Every rail, threshold, fail-open shape and
redaction lives in laya_usecases — nothing is re-implemented at this layer.
The ``jev`` name is a *binary* shim (``bin/jev`` exec's ``bin/laya``), never
an argument-parsing alias (D6: the alias belongs at the binary level; both
names run this exact file with identical stdin/stdout JSON contracts).

Exit codes (spec-v2 §2, universal):
  0  answered — including every degraded fail-open answer (§2: every
     Jev-touching cmd except ``ask`` exits 0 with a usable answer)
  2  invalid input — stdin JSON parse fail, missing required key, or the
     primitive returned status "invalid" (its body is printed verbatim);
     argparse usage errors land here too. NEVER a traceback on stdout
     (tracebacks go to stderr).
  3  the engine could not build (missing asset) — {"error":
     "engine_unavailable"}; NOT 2, never a crash. Only the ``ask`` escape
     hatch can reach it: every other question-hitting command substitutes a
     dead engine and answers degraded (exit 0), and the zero-model-call
     commands (ladder, doctor, plan build-only) never build the engine at
     all (spec §7: ladder is "config + templates", no head).

stdin/stdout contract: stdin is ONE JSON object (empty stdin = ``{}``);
stdout is EXACTLY ONE JSON document (json.dumps, no trailing logs); every
failure additionally writes a single-line note to stderr.

Arg naming (spec-v2 §2 + D6 accept-under-both — the documented dual-spelling
pattern applied to stdin keys): ``route`` accepts ``turn`` or ``text``;
``search`` accepts ``question`` or ``query`` and ``round_index`` or
``round``; flags override stdin (``--round/--max-rounds/--top-k/--keep-last/
--now``); unknown flags are argparse errors, exit 2.

Import-time side effects: NONE — the Engine module is imported and built
lazily, only after arg parsing, only inside commands that hit a model, via
the same env contract as laya_mcp (LAYA_ASSETS/LAYA_SOURCE/LAYA_UNIT are
engine.py's own contract; this CLI never reads them itself except ``doctor``,
which reports buildability WITHOUT building). Env for the D6 alias surface
is read only through ``naming.env_alias``; the TEXT_MODEL{,_API_KEY,
_BASE_URL} trio is the one documented exception — Jev left those unbranded
(plan D6 table) and they are read verbatim.

"Jev" appears in comments and accepted aliases only (D7: no jev_* artifacts
here; bin/jev is a shim file whose only content is exec'ing bin/laya).
"""
import argparse
import json
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:                    # top-level import style,
    sys.path.insert(0, _HERE)                # same as laya_api_sec

import laya_api_http as api                  # noqa: E402  ask rides
import laya_constants as K                   # noqa: E402  answer_request:
import laya_usecases as U                    # noqa: E402  engine.question
import laya_api_sec as sec                   # noqa: E402  through the
import naming                                # noqa: E402  laya_ops
from naming import env_alias                 # noqa: E402  answerers + §1

EXIT_OK = 0        # answered (incl. degraded fail-open)
EXIT_INVALID = 2   # invalid input / primitive status "invalid" / argparse
EXIT_ENGINE = 3    # engine could not build (missing asset)

# §2 ask row: engine raises mapped into the laya_api_sec vocabulary —
# RuntimeError is the engine worker's error class (RETRYABLE "overloaded"
# is its service code); ValueError is laya_ops refusing to serialize an
# answer that would break a §1 invariant ("invalid_response"). Anything
# else about the *request* is "malformed". The ask row has NO fail-open.
def _ask_error_code(exc):
    code = getattr(exc, "code", None)
    if code:
        return code                        # LayaApiError keeps its own code
    if isinstance(exc, RuntimeError):
        return "timeout" if "timeout" in str(exc) else "overloaded"
    if isinstance(exc, ValueError):
        return "invalid_response"
    return "malformed"


# plan row (§2): TEXT_MODEL{,_API_KEY,_BASE_URL} stay unbranded (plan D6
# table) — the ONE env read in this file outside naming.env_alias.
OPENAI_PLAN_BASE = "https://api.openai.com/v1"

# engine.py's asset file name, quoted for doctor's BUILDABILITY report only
# (doctor never builds; a rename there must rename here — comment cites it).
_MODEL_ASSET_NAME = "laya-combined-f16.aimodel"

_NOT_IMPL_HINTS = {
    "memo": ("key/value notes land under $XDG_CACHE_HOME/laya/memo (mode "
             "off|shadow|on via LAYA_MEMO↔JEV_MEMO); owned by the memo "
             "ticket"),
    "spend": ("usage totals read the dual decision log "
              "(<profile>/logs/laya-decisions.jsonl); owned by the spend "
             "ticket"),
    "dashboard": ("the upstream router-dashboard already reads our "
                  "dual-written log natively; our own viewer is its own "
                  "ticket"),
    "setup-key": ("the one-time key ceremony (loopback page -> keystore + "
                  "profile .envs, written under both names) is its own "
                  "ticket; set LAYA_API_KEY (or JEV_/TYPESAFE_API_KEY) "
                  "meanwhile"),
}


class EngineUnavailable(Exception):
    """The engine could not be built (missing asset / bad env / load
    failed). §2: ``ask`` answers {"error":"engine_unavailable"} exit 3;
    every fail-open command substitutes a dead engine instead and answers
    degraded, exit 0."""


class _Invalid(Exception):
    """Structured CLI-layer invalid input (§2: missing required key, stdin
    not JSON): body {"error": <code>} prints, exit 2. Never a traceback."""

    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


def _note(msg):
    """ONE single-line error note per failure — stderr only; stdout stays
    exactly one JSON document (§2)."""
    sys.stderr.write("laya-cli: " + " ".join(str(msg).split()) + "\n")


def _emit(obj):
    """Exactly ONE JSON document on stdout per command invocation."""
    print(json.dumps(obj, ensure_ascii=False))


# ---------------------------------------------------------------------------
# engine (lazy — built after arg parsing, only for question-hitting
# commands; same env contract as laya_mcp, engine.py's own module)
# ---------------------------------------------------------------------------

_ENGINE = None


class _DeadEngine:
    """Substitute for a could-not-build engine on fail-open commands: every
    question raises RuntimeError, so each primitive answers with its own §2
    fail-open column and the CLI exits 0 (§2 universal rule)."""

    def __init__(self, why):
        self._why = str(why)

    def question(self, name, q, state):
        raise RuntimeError(f"engine unavailable: {self._why[:160]}")


def _engine():
    """Build the Engine ONCE per one-shot process (the CLI is not a daemon:
    it builds, answers, exits). Raises EngineUnavailable when the asset or
    env is missing. Tests monkeypatch THIS function."""
    global _ENGINE
    if _ENGINE is None:
        try:
            from engine import Engine
            _ENGINE = Engine()
        except Exception as exc:            # KeyError(env), RuntimeError(load)
            raise EngineUnavailable(
                f"{type(exc).__name__}: {exc}") from None
    return _ENGINE


def _engine_for_failopen():
    """Every §2 row except ``ask``: an engine that cannot build degrades to
    a dead engine — the primitive's own fail-open answer, exit 0. §2 note:
    unreachable != failure; a missing asset must never crash these."""
    try:
        return _engine()
    except EngineUnavailable as exc:
        _note(f"engine unavailable, answering degraded: {exc}")
        return _DeadEngine(exc)


# ---------------------------------------------------------------------------
# stdin (ONE JSON object; empty -> {})
# ---------------------------------------------------------------------------

def _read_stdin_json():
    try:
        raw = sys.stdin.read()
    except OSError as exc:
        raise _Invalid("malformed", f"cannot read stdin: {exc}") from None
    raw = raw.strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        raise _Invalid("malformed", f"stdin is not JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise _Invalid("malformed", "stdin must be ONE JSON object")
    return obj


def _require(obj, *names):
    """First present key among ``names`` (D6 accept-under-both); missing or
    empty -> exit 2 with an invalid_request body (§2: missing required
    key)."""
    for n in names:
        if n in obj and obj[n] not in (None, "", [], {}):
            return obj[n]
    raise _Invalid("invalid_request",
                   f"missing required key: {'|'.join(names)}")


def _invalid_from_primitive(out):
    """A primitive's status:"invalid" is a contract result, not a crash:
    print its body VERBATIM, note to stderr, exit 2 (§2)."""
    if isinstance(out, dict) and out.get("status") == "invalid":
        _note(f"invalid request: {out.get('detail', '')}")
        _emit(out)
        return EXIT_INVALID
    return None


# ---------------------------------------------------------------------------
# routing config (route row): env alias ROUTING_CONFIG path (override, no
# chain fallthrough), else naming.routing_config_chain(): <hermes>/laya|jev/
# then <xdg>/laya|jev/ (D6 — ONE chain shared with the plugin);
# unparseable -> {} fail-open (spec §3).
# ---------------------------------------------------------------------------

def _load_routing_config():
    p = env_alias("ROUTING_CONFIG")
    if p:                                   # override: NO chain fallback
        try:
            with open(p) as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _note(f"routing config {p} unreadable; using {{}} (fail-open, "
                  "spec §3)")
            return {}
    for f in naming.routing_config_chain():  # ONE chain (D6): hermes_root
        if f.is_file():                      # profile layer, then XDG
            try:
                with open(f) as fh:
                    data = json.load(fh)
            except ValueError:
                _note(f"routing config {f} unparseable; using {{}} "
                      "(fail-open, spec §3)")
                return {}
            return data if isinstance(data, dict) else {}
    return {}


# ---------------------------------------------------------------------------
# commands (each: parse stdin keys exactly as spec-v2 §2 names them →
# call the ONE primitive → _emit → §2 exit code)
# ---------------------------------------------------------------------------

def cmd_ask(args):
    """Raw wire-question passthrough — the escape hatch (§2 ``ask`` row).
    Rides the SAME validate→answer→validate path as the HTTP question API
    (laya_api_http.answer_request: engine.question through the laya_ops
    answerers + §1 invariants) — CLI and HTTP cannot drift. NO fail-open:
    any refusal or engine error is exit 2 {"error": <code>} in the
    laya_api_sec vocabulary; a could-not-build engine is the §2 exit 3."""
    payload = _read_stdin_json()
    try:
        eng = _engine()
    except EngineUnavailable as exc:
        _note(exc)
        _emit({"error": "engine_unavailable", "detail": str(exc)[:300]})
        return EXIT_ENGINE
    try:
        _emit(api.answer_request(payload, eng))
        return EXIT_OK
    except sec.LayaApiError as exc:
        _note(f"refused: {exc.code} {exc.detail or ''}")
        _emit({"error": exc.code, "detail": str(exc)[:300]})
        return EXIT_INVALID
    except Exception as exc:                 # engine raise: NO fail-open
        code = _ask_error_code(exc)
        _note(f"engine error: {exc}")
        _emit({"error": code, "detail": str(exc)[:300]})
        return EXIT_INVALID


def cmd_route(args):
    """§2 route row: turn|text (D6 accept-under-both), current_model,
    context_tokens, pinned."""
    obj = _read_stdin_json()
    turn = _require(obj, "turn", "text")
    try:
        context_tokens = int(obj.get("context_tokens") or 0)
    except (TypeError, ValueError):
        raise _Invalid("invalid_request",
                       '"context_tokens" must be a number') from None
    out = U.route_turn(_engine_for_failopen(), str(turn),
                       _load_routing_config(),
                       current_model=obj.get("current_model"),
                       context_tokens=context_tokens,
                       pinned=bool(obj.get("pinned", False)))
    _emit(out)
    return EXIT_OK


def cmd_rerank(args):
    obj = _read_stdin_json()
    query = _require(obj, "query", "question")   # D6 accept-under-both
    passages = obj.get("passages")
    if not isinstance(passages, list):
        raise _Invalid("invalid_request", '"passages" must be a list')
    _emit(U.rerank_passages(_engine_for_failopen(), str(query), passages))
    return EXIT_OK


def cmd_compact_select(args):
    obj = _read_stdin_json()
    turns = obj.get("turns")
    if not isinstance(turns, list) or not turns:
        raise _Invalid("invalid_request", '"turns" must be a non-empty list')
    keep_last = int(obj.get("keep_last") or K.COMPACT_KEEP_LAST)
    if args.keep_last is not None:               # flag overrides stdin
        keep_last = int(args.keep_last)
    _emit(U.compact_select(_engine_for_failopen(), turns,
                           keep_last=keep_last))
    return EXIT_OK


def cmd_pick_skill(args):
    obj = _read_stdin_json()
    state = _require(obj, "state", "turn")       # D6 accept-under-both
    skills = obj.get("skills")
    if not isinstance(skills, dict) or not skills:
        raise _Invalid("invalid_request",
                       '"skills" must be a non-empty {name: description} '
                       "mapping")
    _emit(U.skill_select(_engine_for_failopen(), state, skills))
    return EXIT_OK


def cmd_choose(args):
    """stdin is the FULL action_choice_request_v1 object (§2 choose row,
    incl. its ``schema`` field — the primitive accepts both spellings)."""
    obj = _read_stdin_json()
    out = U.choose_action(_engine_for_failopen(), obj)
    code = _invalid_from_primitive(out)
    if code is not None:
        return code                              # body printed verbatim
    _emit(out)
    return EXIT_OK


def cmd_search(args):
    obj = _read_stdin_json()
    question = _require(obj, "question", "query")   # D6 accept-under-both
    results = obj.get("results")
    if not isinstance(results, list):
        raise _Invalid("invalid_request", '"results" must be a list')
    round_index = obj.get("round_index", obj.get("round")) or 1
    max_rounds = obj.get("max_rounds") or 3
    top_k = obj.get("top_k")
    if args.round is not None:                   # flags override stdin
        round_index = int(args.round)
    if args.max_rounds is not None:
        max_rounds = int(args.max_rounds)
    if args.top_k is not None:
        top_k = int(args.top_k)
    out = U.search_decide(
        _engine_for_failopen(), str(question), results,
        queries_tried=obj.get("queries_tried") or (),
        candidate_queries=obj.get("candidate_queries") or (),
        round_index=round_index, max_rounds=max_rounds,
        top_k=top_k, reading_failed=bool(obj.get("reading_failed", False)))
    code = _invalid_from_primitive(out)
    if code is not None:
        return code
    _emit(out)
    return EXIT_OK


def _message_from(obj):
    """triage/mail stdin: ``message`` (string or object) or the
    from/subject/body triple (§2 rows)."""
    if "message" in obj:
        return obj["message"]
    if any(k in obj for k in ("from", "subject", "body")):
        return {"from": obj.get("from", ""),
                "subject": obj.get("subject", ""),
                "body": obj.get("body", "")}
    raise _Invalid("invalid_request",
                   "missing required key: message or from/subject/body")


def cmd_triage(args):
    obj = _read_stdin_json()
    _emit(U.triage_message(_engine_for_failopen(), _message_from(obj)))
    return EXIT_OK


def cmd_mail(args):
    obj = _read_stdin_json()
    _emit(U.mail_sort(_engine_for_failopen(), _message_from(obj)))
    return EXIT_OK


SUPERVISE_FACT_KEYS = ("no_output_s", "nudge_streak", "question_pending",
                       "last_output", "notes")   # §2 supervise row facts


def cmd_supervise(args):
    obj = _read_stdin_json()          # facts dict, §2 supervise row keys
    facts = {k: obj[k] for k in SUPERVISE_FACT_KEYS if k in obj}
    _emit(U.supervise_run(_engine_for_failopen(), facts))
    return EXIT_OK


def cmd_plan(args):
    """§2 plan row: the BUILDER is pure bookkeeping — no model, no network
    (a build-only run must NEVER touch the net). --call sends the built
    request to TEXT_MODEL via urllib ONLY when both TEXT_MODEL and
    TEXT_MODEL_API_KEY are set; any failure of the call or the parse lands
    on U.parse_plan_response's fallback, exit 0 (§2 fail-open column)."""
    obj = _read_stdin_json()
    command = _require(obj, "command", "goal")
    built = U.plan_request(command, obj.get("goal"))
    if not args.call:
        out = {"status": "built",
               "plan_model": env_alias("PLAN_MODEL")
               or os.environ.get("TEXT_MODEL") or None}
        out.update(built)
        _emit(out)
        return EXIT_OK
    text_model = os.environ.get("TEXT_MODEL")
    api_key = os.environ.get("TEXT_MODEL_API_KEY")
    if not text_model or not api_key:
        _note("--call without TEXT_MODEL + TEXT_MODEL_API_KEY: nothing "
              "sent, answering with the fallback plan")
        _emit(U.parse_plan_response(None))
        return EXIT_OK
    try:
        _emit(_call_text_model(built, text_model, api_key))
    except Exception as exc:
        _note(f"plan call failed ({type(exc).__name__}); using fallback")
        _emit(U.parse_plan_response(None))
    return EXIT_OK


def _call_text_model(built, text_model, api_key):
    """The ONE network touch in this file, behind --call only (urllib,
    stdlib). LAYA_PLAN_MODEL↔JEV_PLAN_MODEL alias overrides the model
    (plan D6); base URL from the unbranded TEXT_MODEL_BASE_URL. The parsed
    answer rides U.parse_plan_response — any parse failure is its fallback."""
    import urllib.request
    model = env_alias("PLAN_MODEL") or text_model
    base = (os.environ.get("TEXT_MODEL_BASE_URL")
            or OPENAI_PLAN_BASE).rstrip("/")
    body = {"model": model, "messages": built["messages"],
            "response_format": {"type": "json_schema",
                                "json_schema": built["schema"]}}
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return U.parse_plan_response(
        payload["choices"][0]["message"]["content"])


def cmd_ladder(args):
    """Ladder bookkeeping (§2 ladder row): the ONE shared state file via
    naming.ladder_state_path (D6 symlink rule), NEVER a model call and
    NEVER an engine build (spec §7: "No head — config + templates").
    --now is the test clock seam. fire/no-fire is still a usable answer:
    exit 0 everywhere except invalid stdin on `choose`."""
    if args.ladder_cmd == "choose":
        obj = _read_stdin_json()
        rung = _require(obj, "rung")
        rungs = obj.get("rungs")
        if not isinstance(rungs, list):     # fall back to routing config
            rungs = ((_load_routing_config().get("escalation") or {})
                     .get("rungs") or [])
        now = float(args.now) if args.now is not None else None
        _emit(U.ladder_choose(None, str(rung), rungs, now=now))
        return EXIT_OK

    try:
        obj = _read_stdin_json()            # other subcmds ignore bad stdin
    except _Invalid as exc:
        _note(f"{exc}; ignoring stdin")
        obj = {}
    now = float(args.now) if args.now is not None else None

    if args.ladder_cmd == "status":
        _emit(U.ladder_status(None, now=now))
        return EXIT_OK
    if args.ladder_cmd == "refuse":
        rung = _require(obj, "rung")
        ts = now if now is not None else time.time()
        state = U._ladder_load()            # the ONE state path (D6)
        entry = state["rungs"].setdefault(str(rung), {})
        entry["refused_ts"] = ts
        try:
            U._ladder_save(state)
        except OSError as exc:
            raise _Invalid("invalid_request",
                           f"state write failed: {exc}") from None
        _emit({"refused": str(rung), "refused_ts": ts})
        return EXIT_OK
    # clear: empties the ONE shared state (atomic 0600). Under D6 the jev
    # path is a symlink to the same file, so cooldowns reset under both
    # names at once — which is exactly why the ladder never forks state.
    try:
        U._ladder_save({"rungs": {}})
    except OSError as exc:
        raise _Invalid("invalid_request",
                       f"state write failed: {exc}") from None
    _emit({"cleared": True})
    return EXIT_OK


def cmd_not_impl(args):
    """§2 local bookkeeping commands a LATER ticket owns (memo/spend/
    dashboard/setup-key): exit 0 with an honest not_impl body — a silent
    failure would read as success, a loud fake answer would lie."""
    _emit({"status": "not_impl", "cmd": args.cmd,
           "hint": _NOT_IMPL_HINTS[args.cmd]})
    return EXIT_OK


def cmd_doctor(args):
    """§2 doctor row: reports engine buildability, assets dir, alias
    divergences and key PRESENCE (naming.describe_api_key — length only,
    never the key). Builds NO engine — buildability is a path check.
    Exit 1 ONLY when no key AND --need-key (unreachable != failure, §2
    note); every other report exits 0."""
    assets_dir = env_alias("ASSETS")     # generic chain LAYA_/JEV_/TYPESAFE_
    source_dir = env_alias("SOURCE") or (os.path.join(assets_dir, "configs")
                                         if assets_dir else None)
    assets = []
    if assets_dir and os.path.isdir(assets_dir):
        assets = sorted(os.listdir(assets_dir))
    model_seen = bool(assets_dir and os.path.exists(
        os.path.join(assets_dir, _MODEL_ASSET_NAME)))
    source_seen = bool(source_dir and os.path.isdir(source_dir))
    buildable = model_seen and source_seen
    key = naming.describe_api_key()          # presence only, never value
    _emit({"engine": "buildable" if buildable else "error",
           "assets": assets,
           "warnings": naming.doctor_alias_report()["divergences"],
           "model_asset_seen": model_seen,
           "source_seen": source_seen,
           "key": key})
    if args.need_key and not key["present"]:
        _note("no API key found (LAYA_/JEV_/TYPESAFE_API_KEY env, keystore, "
              "or ~/.config/laya|jev/credentials) and --need-key was set")
        return 1
    return EXIT_OK


# ---------------------------------------------------------------------------
# argument table (flags minimal per §2 row; unknown flags -> argparse error
# -> exit 2)
# ---------------------------------------------------------------------------

def _build_parser():
    parser = argparse.ArgumentParser(
        prog="laya",
        description="Laya decision CLI (dual-named: `jev` is the same "
                    "command via bin/jev) — stdin JSON in, exactly one "
                    "JSON document out.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name, func, **kw):
        sp = sub.add_parser(name, **kw)
        sp.set_defaults(func=func)
        return sp

    add("ask", cmd_ask, help="raw wire questions passthrough (noul|choice|"
        "score); the escape hatch — NO fail-open: any error exits 2")
    add("route", cmd_route, help="pick the model for this turn")
    add("rerank", cmd_rerank, help="screen memory passages entering context")
    cs = add("compact-select", cmd_compact_select,
             help="keep/summarize/drop transcript turns")
    cs.add_argument("--keep-last", type=int, default=None,
                    help="tail turns always kept (overrides stdin keep_last)")
    add("pick-skill", cmd_pick_skill, help="which SKILL.md should load")
    add("choose", cmd_choose, help="next GUI/browser step "
        "(action_choice_request_v1 object on stdin)")
    se = add("search", cmd_search, help="one search-loop round")
    se.add_argument("--round", type=int, default=None,
                    help="override stdin round_index|round")
    se.add_argument("--max-rounds", type=int, default=None,
                    help="override stdin max_rounds")
    se.add_argument("--top-k", type=int, default=None,
                    help="override stdin top_k")
    add("triage", cmd_triage, help="route one inbound message "
        "now/today/queue/ignore")
    add("mail", cmd_mail, help="sort one mail into a lane")
    add("supervise", cmd_supervise, help="poll a delegated run")
    pl = add("plan", cmd_plan, help="build the TEXT_MODEL step-decomposition "
             "request; NEVER calls a model without --call")
    pl.add_argument("--call", action="store_true",
                    help="send to TEXT_MODEL via urllib (needs TEXT_MODEL + "
                         "TEXT_MODEL_API_KEY; any failure falls back, exit 0)")
    lad = add("ladder", cmd_ladder, help="escalation ladder bookkeeping "
              "(no model call, no engine build)")
    lsub = lad.add_subparsers(dest="ladder_cmd", required=True)
    for lcmd in ("choose", "status", "refuse", "clear"):
        lsp = lsub.add_parser(lcmd)
        lsp.add_argument("--now", type=float, default=None,
                         help="test clock seam (epoch seconds)")
    for name in _NOT_IMPL_HINTS:
        add(name, cmd_not_impl, help="later ticket owns this command; "
            "answers not_impl, exit 0")
    dr = add("doctor", cmd_doctor, help="alias/key/engine report (exit 1 "
             "only with --need-key when no key is present)")
    dr.add_argument("--need-key", action="store_true",
                    help="exit 1 when no API key is present")
    return parser


def main(argv=None):
    """Returns the process exit code (§2: 0 answered/degraded, 2 invalid,
    3 engine could not build). NO side effects on import; the engine is
    built lazily inside the command that hits a model."""
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:                 # usage error -> argparse's 2
        return int(exc.code) if isinstance(exc.code, int) else EXIT_INVALID
    try:
        return args.func(args)
    except _Invalid as exc:
        _note(exc)
        _emit({"error": exc.code, "detail": str(exc)[:300]})
        return EXIT_INVALID
    except EngineUnavailable as exc:           # belt: only ask builds
        _note(exc)                             # directly, but a direct hit
        _emit({"error": "engine_unavailable",  # anywhere is still §3, not
               "detail": str(exc)[:300]})     # a crash
        return EXIT_ENGINE
    except Exception:
        traceback.print_exc(file=sys.stderr)   # tracebacks: stderr ONLY
        _emit({"error": "internal_error", "detail": "see stderr"})
        return EXIT_INVALID


if __name__ == "__main__":
    sys.exit(main())
