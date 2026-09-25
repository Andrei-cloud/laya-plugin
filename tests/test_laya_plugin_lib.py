"""Tests for the vendored plugin library + RemoteEngine (M2 packaging).

Why this file exists (observed defect, M2 drill): Hermes loads a plugin
as an ISOLATED package — `plugins install` copytrees ONLY the plugin dir,
so the sys.path dance to the repo's server/ works in the dev tree and
fails in EVERY real install (`No module named 'naming'`). The fix is to
vendor the module closure inside the plugin; these tests are the drift
gates that keep the copies honest.

D7: all names here are laya-named. Spec refs: plan Files map (D6/D7),
spec-v2 §1 (wire).
"""
import ast
import hashlib
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SERVER = os.path.join(REPO, "server")
sys.path.insert(0, SERVER)

from scripts.sync_plugin_lib import MODULES, PLUGIN_DIRS  # noqa: E402


def _local_imports(path):
    tree = ast.parse(open(path).read())
    imps = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imps |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            imps.add(n.module.split(".")[0])
    return {m for m in imps if (os.path.exists(os.path.join(SERVER, m + ".py")))}


def _closure(seeds):
    seen, need = set(), list(seeds)
    while need:
        m = need.pop()
        if m in seen or not os.path.exists(os.path.join(SERVER, m + ".py")):
            continue
        seen.add(m)
        need += sorted(_local_imports(os.path.join(SERVER, m + ".py")))
    return seen


class TestVendoredLib(unittest.TestCase):
    def test_modules_list_is_the_real_closure(self):
        """MODULES must be the transitive closure of what the plugin
        imports at runtime — a new server/ import that forgets the sync
        list breaks every install (the naming defect all over again)."""
        seeds = {"naming", "laya_decisions", "laya_usecases", "laya_remote"}
        # laya_remote imports naming only; engine.py rides in because
        # laya_usecases imports its guard rail (documented, lazy-safe).
        closure = {m + ".py" for m in _closure(seeds)}
        missing = closure - set(MODULES)
        self.assertEqual(
            missing, set(),
            f"server/ modules reachable from the plugin but not vendored: "
            f"{sorted(missing)} — add to scripts/sync_plugin_lib.MODULES")

    def test_vendored_copies_byte_identical(self):
        """The CI drift gate (same check as --check): vendored lib/ files
        must equal server/ byte-for-byte — two truths is one too many."""
        for plugin_dir in PLUGIN_DIRS:
            lib = os.path.join(REPO, plugin_dir, "lib")
            for mod in MODULES:
                src, dst = os.path.join(SERVER, mod), os.path.join(lib, mod)
                with self.subTest(plugin=plugin_dir, mod=mod):
                    self.assertTrue(os.path.exists(dst), f"missing {dst}")
                    self.assertEqual(
                        hashlib.sha256(open(src, "rb").read()).hexdigest(),
                        hashlib.sha256(open(dst, "rb").read()).hexdigest())

    def test_engine_module_is_stdlib_importable(self):
        """The agent process imports the vendored engine.py at module
        level (via laya_usecases) but must NEVER need numpy/coreai to do
        it — the heavy laya_port import is lazy inside _build_agent. If a
        future edit hoists it, the Hermes plugin stops loading."""
        import subprocess
        lib = os.path.join(REPO, PLUGIN_DIRS[0], "lib")
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); import engine; "
             "assert not hasattr(engine, 'CombinedAgent'); "
             "print('lazy-ok')" % lib],
            capture_output=True, text=True, timeout=60)
        self.assertIn("lazy-ok", r.stdout, r.stderr[-400:])

    def test_plugin_isolated_load_and_registration(self):
        """THE regression test for the observed defect: load the plugin
        from a COPY placed outside the repo (exactly what Hermes does),
        with the repo's server/ NOT on sys.path — import + register(ctx)
        must succeed and register 13 tools."""
        import shutil
        import tempfile

        class Ctx:
            def __init__(self):
                self.tools, self.cmds = [], []
                self.hooks = []

            def register_tool(self, **kw):
                self.tools.append(kw["name"])

            def register_hook(self, name, fn):
                self.hooks.append(name)

            def register_middleware(self, kind, fn):
                self.hooks.append("mw:" + kind)

            def register_command(self, name, fn, description="",
                                 args_hint=""):
                self.cmds.append(name)

            def register_system_prompt_section(self, id, content,
                                               **kw):
                pass

            def get_config(self, name, default):
                return default

        with tempfile.TemporaryDirectory() as td:
            dest = os.path.join(td, "plugins", "hermes-laya")
            shutil.copytree(os.path.join(REPO, "hermes", "plugin",
                                         "hermes-laya"), dest,
                            ignore=shutil.ignore_patterns("__pycache__"))
            init = os.path.join(dest, "__init__.py")
            spec = __import__("importlib.util", fromlist=["util"]) \
                .spec_from_file_location("hermes_laya_isolated", init,
                                         submodule_search_locations=[dest])
            mod = __import__("importlib.util", fromlist=["util"]) \
                .module_from_spec(spec)
            # simulate a foreign process: no repo server/ on the path
            saved = list(sys.path)
            sys.path[:] = [p for p in sys.path
                           if REPO not in os.path.abspath(p + os.sep)]
            try:
                sys.modules["hermes_laya_isolated"] = mod
                spec.loader.exec_module(mod)
                ctx = Ctx()
                mod.register(ctx)
                canon = [t for t in ctx.tools if t.startswith("laya_")]
                alias = [t for t in ctx.tools if t.startswith("jev_")]
                self.assertEqual(len(canon), 7)
                self.assertEqual(len(alias), 6)   # guard unaliased (D6)
                self.assertIn("laya", ctx.cmds)
                self.assertIn("jev", ctx.cmds)
                self.assertIn("mw:llm_request", ctx.hooks)
            finally:
                sys.path[:] = saved
                sys.modules.pop("hermes_laya_isolated", None)


class _StubDaemon(BaseHTTPRequestHandler):
    """Minimal /v1/laya + /health twin of the warm daemon: answers every
    question with a fixed distribution over the criteria keys."""
    scenarios = {}      # class-level: set per test

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "warm": True,
                             "chains": ["base", "ft_lang", "ft_gr14_ppo",
                                        "ft_tool5v2_ppo", "ft_skill4_ppo"]})
        else:
            self._send(404, {"error": "nf"})

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        req = json.loads(self.rfile.read(n))
        auth = self.scenarios.get("require_auth")
        if auth and self.headers.get("authorization") != f"Bearer {auth}":
            self._send(401, {"status": "invalid_request",
                             "error": "unauthorized"})
            return
        answers = {}
        for name, q in req["questions"].items():
            spec = self.scenarios.get("answer", {})
            if q["type"] == "noul":
                answers[name] = {"type": "noul",
                                 "noul": spec.get("noul", 0.9)}
            elif q["type"] == "choice":
                keys = list(q["criteria"])
                top = spec.get("choice", keys[0])
                probs = {k: (0.7 if k == top else 0.3 / (len(keys) - 1))
                         for k in keys}
                answers[name] = {"type": "choice", "choice": top,
                                 "probabilities": probs,
                                 "confidence": probs[top]}
            else:
                answers[name] = {"type": "score", "score": 1.0,
                                 "probabilities": {"0": 0.0, "1": 1.0},
                                 "spread_reported": True, "confidence": 1.0}
        self._send(200, {"answers": answers, "model": req.get("model"),
                         "usage": {"requests": 1, "chars_in": 1,
                                   "chars_out": 1}})

    def log_message(self, *a):
        pass


class TestRemoteEngine(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubDaemon)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever,
                                  daemon=True)
        self.t.start()
        _StubDaemon.scenarios = {}
        os.environ.pop("LAYA_TOKEN", None)
        os.environ.pop("JEV_TOKEN", None)
        import naming
        naming._reset_state()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        import naming
        naming._reset_state()

    def _eng(self, **kw):
        from laya_remote import RemoteEngine
        return RemoteEngine(base_url=f"http://127.0.0.1:{self.port}", **kw)

    def test_choice_question_reshapes_to_engine_contract(self):
        eng = self._eng()
        out = eng.question("disposition",
                           {"type": "choice", "instructions": "how?",
                            "criteria": {"allow": "a", "ask_user": "b",
                                         "block": "c"}},
                           "rm -rf /")
        for key in ("task", "chain", "choice", "confidence", "acted",
                    "act_p", "probs", "latency_ms"):
            self.assertIn(key, out)
        self.assertEqual(out["task"], "guardrail")
        # chain label comes from the daemon's own /health table
        self.assertEqual(out["chain"], "ft_gr14_ppo")
        self.assertAlmostEqual(sum(out["probs"].values()), 1.0, places=3)

    def test_noul_reshapes_to_yes_no_probs(self):
        _StubDaemon.scenarios = {"answer": {"noul": 0.8}}
        out = self._eng().question("q1", {"type": "noul",
                                          "instructions": "relevant?"},
                                   "state")
        self.assertEqual(set(out["probs"]), {"no", "yes"})
        self.assertAlmostEqual(out["probs"]["yes"], 0.8, places=3)

    def test_guard_verdict_flows_over_remote(self):
        """ask_guardrail (engine.py, the ONE rail) must work unchanged
        against the remote engine — that is the whole duck-type point."""
        from engine import ask_guardrail
        _StubDaemon.scenarios = {"answer": {"choice": "block"}}
        out = ask_guardrail(self._eng(), "rm -rf /")
        self.assertEqual(out["verdict"], "deny")
        _StubDaemon.scenarios = {"answer": {"choice": "ask_user"}}
        out = ask_guardrail(self._eng(), "sudo reboot")
        self.assertEqual(out["verdict"], "confirm")

    def test_dead_daemon_raises_runtimeerror_fail_open(self):
        """The usecases' fail-open rails catch RuntimeError; a dead daemon
        must produce exactly that (never hang, never another type)."""
        from laya_remote import RemoteEngine
        eng = RemoteEngine(base_url="http://127.0.0.1:1",
                           timeout_s=2)
        with self.assertRaises(RuntimeError):
            eng.question("q", {"type": "noul", "instructions": "?"}, "s")

    def test_daemon_error_code_surface_never_leaks_body(self):
        _StubDaemon.scenarios = {"require_auth": "sekret"}
        eng = self._eng()
        try:
            eng.question("q", {"type": "noul", "instructions": "?"}, "s")
            self.fail("expected RuntimeError")
        except RuntimeError as e:
            self.assertIn("http_401", str(e))
            self.assertNotIn("sekret", str(e))

    def test_token_alias_sent_as_bearer(self):
        _StubDaemon.scenarios = {"require_auth": "t0ken"}
        os.environ["JEV_TOKEN"] = "t0ken"      # alias spelling
        import naming
        naming._reset_state()
        out = self._eng().question("q1", {"type": "noul",
                                          "instructions": "?"}, "s")
        self.assertGreater(out["probs"]["yes"], 0.5)

    def test_unknown_qtype_value_error_locals(self):
        with self.assertRaises(ValueError):
            self._eng().question("q", {"type": "bogus"}, "s")


if __name__ == "__main__":
    unittest.main()
