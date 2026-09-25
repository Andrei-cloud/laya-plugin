"""HTTP bridge around the same Engine — for harnesses that can't speak MCP
natively (opencode JS plugin). stdlib only; binds 127.0.0.1 only.

  POST /decide  {"task": "...", "state": "...", "options": {...}?}
  POST /guard   {"command": "..."}
  GET  /health

Run: python -m server.laya_http [--port 11260]   (env as in engine.py)
"""
import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Engine  # noqa: E402

ENGINE: Engine | None = None
LOCK = threading.Lock()


def _eng() -> Engine:
    global ENGINE
    if ENGINE is None:
        with LOCK:
            if ENGINE is None:
                ENGINE = Engine()
    return ENGINE


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            e = _eng()
            self._send(200, {"ok": True, "unit": os.environ.get("LAYA_UNIT", "gpu"),
                             "chains": e.agent.chain_order, "calls": e.calls,
                             "mean_latency_ms": round(e.total_ms / max(1, e.calls), 2)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            e = _eng()
            if self.path == "/decide":
                with LOCK:  # one Core AI session; serialize compute
                    out = e.decide(req["task"], req["state"], req.get("options"))
            elif self.path == "/guard":
                with LOCK:
                    out = e.decide("guardrail",
                                   f"About to run: {req['command'][:800]}")
                out["verdict"] = ("deny" if out["choice"] == "block" else
                                  "confirm" if (out["choice"] == "ask" or not out["acted"])
                                  else "allow")
            else:
                return self._send(404, {"error": "not found"})
            self._send(200, out)
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, format, *args):  # noqa: A002
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("LAYA_PORT", 11260)))
    a = ap.parse_args()
    _eng()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"laya bridge on http://127.0.0.1:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
