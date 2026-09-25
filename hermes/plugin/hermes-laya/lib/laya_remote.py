"""RemoteEngine — duck-typed Engine over the warm decision daemon.

The Hermes agent process does not carry the Core AI stack (numpy/coreai,
~8 s asset load) and MUST NOT mount a second in-process Engine: one Core
AI session belongs to ONE process (com.laya.decisiond, :11270) which
serves the identical wire at POST /v1/laya. This client speaks that wire
and returns exactly what laya_usecases expects from engine.question() —
the raw engine result — so every use case runs UNCHANGED against the
remote engine and keeps its existing fail-open rail when the daemon is
down (a RuntimeError here is what the usecases already catch).

D8 (golden rule): same daemon, same wire, both harnesses (the OpenCode
native plugin hits the same /v1/laya from TS; this is its Python twin).
Reference: .hermes/plans/2026-09-25-jev-parity-api-spec-v2.md §1 (wire).
"""
import json
import os
import time
import urllib.error
import urllib.request

from naming import env_alias

DEFAULT_URL = "http://127.0.0.1:11270"
DEFAULT_TIMEOUT_S = 45.0

# Which trained head answered, for the audit/display fields only (the
# in-process Engine fills this from the chain table; the wire answer does
# not carry it). Vocabulary heuristics mirror engine.py::_match_slate —
# semantic constants kept in sync by hand, cited there. Nothing in
# laya_usecases decisions depends on these; they label logs.
_GUARD_VOCAB = {"allow", "ask_user", "block"}
_TRIAGE_VOCAB = {"reply", "act"}


class RemoteEngine:
    """Engine-duck over HTTP. Only .question(name, q, state) is needed by
    laya_usecases (21 call sites, the single engine seam); ask_guardrail
    from engine.py runs against it unchanged because it only consumes
    choice/confidence/latency_ms/chain from question()'s result."""

    def __init__(self, base_url=None, timeout_s=None):
        self.base_url = (base_url or env_alias("DECISIOND_URL")
                         or DEFAULT_URL).rstrip("/")
        t = timeout_s if timeout_s is not None else env_alias("DECISION_TIMEOUT_S")
        self.timeout_s = float(t) if t else DEFAULT_TIMEOUT_S
        self.calls = 0
        self.total_ms = 0.0
        self._chains = None            # cached chain table from /health

    # ---- transport (codes only in errors: never secret, never prompt) ----
    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST")
        token = env_alias("TOKEN")     # LAYA_TOKEN ↔ JEV_TOKEN ↔ DASHBOARD_TOKEN
        if token:
            req.add_header("authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # structured refusal body is {status, error} (codes only) — safe
            # to surface; anything else: status code only.
            detail = ""
            try:
                body = json.loads(e.read().decode("utf-8"))
                detail = str(body.get("error") or body.get("status") or "")[:120]
            except Exception:
                pass
            raise RuntimeError(f"daemon http_{e.code}{' ' + detail if detail else ''}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RuntimeError(f"daemon unreachable ({type(e).__name__})") from None
        except ValueError:
            raise RuntimeError("daemon malformed (non-JSON response)") from None

    def _get(self, path):
        req = urllib.request.Request(self.base_url + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=min(3.0, self.timeout_s)) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    # ---- Engine duck ----
    def question(self, name, q, state):
        """POST one question to /v1/laya, reshape the wire answer back into
        the engine's raw result contract (the keys laya_usecases reads):
        {task, chain, choice, confidence, acted, act_p, probs, latency_ms}.
        Raises RuntimeError on any daemon failure — exactly the exception
        the usecases' fail-open rails already catch."""
        qtype = q.get("type")
        if qtype not in ("noul", "choice", "score"):
            raise ValueError(f"unknown question type {qtype!r}")
        payload = {"model": env_alias("MODEL") or "laya-r15",
                   "state": state, "questions": {name: dict(q)}}
        t0 = time.perf_counter()
        body = self._post("/v1/laya", payload)
        ms = round((time.perf_counter() - t0) * 1000, 2)
        ans = (body.get("answers") or {}).get(name)
        if not isinstance(ans, dict):
            raise RuntimeError(f"daemon answer missing for {name!r}")

        if qtype == "noul":
            p_yes = float(ans.get("noul", 0.0))
            probs = {"no": round(1.0 - p_yes, 4), "yes": round(p_yes, 4)}
        else:
            probs = {k: float(v) for k, v in (ans.get("probabilities") or {}).items()}
        if not probs:
            raise RuntimeError(f"daemon answer for {name!r} carries no probabilities")
        keys = list(probs)
        top = max(keys, key=lambda k: probs[k])
        choice = ans.get("choice") if qtype != "noul" else top
        conf = float(ans.get("confidence", probs[top]))

        task = self._task_hint(q)
        self.calls += 1
        self.total_ms += ms
        return {"task": task, "chain": self._chain_for(task),
                "choice": choice, "confidence": round(conf, 4),
                "acted": conf >= 0.7, "act_p": round(conf, 4),
                "probs": {k: round(float(v), 4) for k, v in probs.items()},
                "latency_ms": ms}

    @staticmethod
    def _task_hint(q):
        crit = q.get("criteria")
        names = set(crit) if isinstance(crit, dict) else set()
        if names == _GUARD_VOCAB:
            return "guardrail"
        if names == _TRIAGE_VOCAB:
            return "triage"
        return "base"

    def _chain_for(self, task):
        """Display/audit label only: map task→chain name via the daemon's
        own chain table (GET /health, cached). /health reports
        {chains:[...]} in head order (base first, then ft_lang, ft_gr14,
        ft_tool5v2, ft_skill4 — the deploy order), so map by KNOWN HEAD
        ORDER of the r15 build; foreign tasks → chain 0. None when the
        daemon does not expose it — never an error. Nothing in
        laya_usecases decisions reads this field."""
        if self._chains is None:
            h = self._get("/health")
            ch = (h or {}).get("chains")
            self._chains = list(ch) if isinstance(ch, list) and ch else []
        # r15 head order (deploy-proven; cited to engine.FakeAgent and
        # bench runs). Unknown task → base, matching Engine._match_slate.
        slot = {"lang_route": "ft_lang", "guardrail": "ft_gr14",
                "act_escalate": "ft_gr14", "tool_route": "ft_tool5v2",
                "skill_route": "ft_skill4"}.get(task)
        if slot:
            for c in self._chains:
                if slot in c:
                    return c
        return self._chains[0] if self._chains else None

    # convenience mirror of Engine.decide's guard path (same rail, remote)
    def healthy(self):
        h = self._get("/health")
        return bool(h) and bool(h.get("ok"))
