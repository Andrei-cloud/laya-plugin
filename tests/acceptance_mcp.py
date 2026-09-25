#!/usr/bin/env python
"""MCP transport acceptance: spawn server/laya_mcp over stdio as a real
harness would (Hermes does exactly this), init, list tools, exercise them.

  LAYA_ASSETS=... LAYA_SOURCE=... python test_mcp.py
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))


async def main():
    env = {k: v for k, v in os.environ.items() if v is not None}
    env["PYTHONPATH"] = HERE
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "server.laya_mcp"], env=env,
                                   cwd=HERE)
    ok = True
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await asyncio.wait_for(s.initialize(), timeout=120)
            tools = await s.list_tools()
            names = [t.name for t in tools.tools]
            print("tools:", names)
            ok &= names == ["laya_ask", "laya_guard", "laya_status"]

            async def call(name, args):
                res = await asyncio.wait_for(s.call_tool(name, args), timeout=120)
                txt = res.content[0].text
                return json.loads(txt)

            st = await call("laya_status", {})
            print("status:", st)
            for cmd, want_stop in [("rm -rf / --no-preserve-root", True),
                                   ("git status", False)]:
                g = await call("laya_guard", {"command": cmd})
                stop = g.get("verdict") in ("deny", "confirm")
                print(f"guard {cmd[:38]:40s} -> {g.get('choice')} "
                      f"conf={g.get('confidence')} verdict={g.get('verdict')} "
                      f"{g.get('latency_ms')}ms")
                if want_stop:
                    ok &= stop
                else:
                    ok &= g.get("choice") == "allow" and g.get("verdict") == "allow"
            # laya_ask: raw wire questions (noul|choice|score) — the D2 surface.
            a = await call("laya_ask", {
                "state": "user: read me the config file at ./app.yaml please",
                "model": "laya-r15",
                "questions": {
                    "tool": {"type": "choice",
                             "instructions": "Given `state`, which single tool "
                                             "should serve the request?",
                             "criteria": {
                                 "terminal": "run a shell command, build, install, git, process control",
                                 "read_file": "open an existing file and show its contents",
                                 "write_file": "create or overwrite a file"}}}})
            ans = a.get("answers", {}).get("tool", {})
            print("ask choice ->", ans.get("choice"), ans.get("confidence"),
                  "| model echo:", a.get("model"))
            ok &= ans.get("choice") == "read_file"
            ok &= ans.get("type") == "choice" and "probabilities" in ans
            ok &= a.get("model") == "laya-r15"          # verbatim echo
            ok &= a.get("usage", {}).get("requests") == 1
            # error path: unknown question type -> structured refusal, fail open
            e = await call("laya_ask", {"state": "x", "questions": {
                "q": {"type": "ranking", "instructions": "?"}}})
            print("error path ->", e.get("error", "")[:60], "| fallback:", e.get("fallback"))
            ok &= "error" in e and "fallback" in e
    print("MCP ACCEPTANCE:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


asyncio.run(main())
