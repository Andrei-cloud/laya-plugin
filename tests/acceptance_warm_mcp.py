"""Acceptance: warm HTTP/MCP daemon roundtrip (the daemon started by
scripts/install_daemon.sh or `python -m server.laya_http_mcp`).

Proves the three-tool surface over streamable HTTP end-to-end:
initialize -> list_tools (exactly 3) -> laya_guard red line stops,
benign command allowed. Requires the daemon already running and warm:
  curl -s http://127.0.0.1:${LAYA_PORT:-11270}/health  ->  "warm": true
"""
import asyncio, json, os
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

PORT = os.environ.get("LAYA_PORT", "11270")


async def main():
    ok = True
    async with streamablehttp_client(f"http://127.0.0.1:{PORT}/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await asyncio.wait_for(s.initialize(), timeout=60)
            tools = await s.list_tools()
            names = [t.name for t in tools.tools]
            print("tools:", names)
            ok &= len(names) == 3
            for cmd, expect in [("rm -rf / --no-preserve-root", "stop"),
                                ("git status", "allow")]:
                res = await asyncio.wait_for(
                    s.call_tool("laya_guard", {"command": cmd}), timeout=60)
                d = json.loads(res.content[0].text)
                stop = d.get("verdict") in ("deny", "confirm")
                good = stop if expect == "stop" else d.get("verdict") == "allow"
                ok &= good
                print(f"guard {cmd[:36]:38s} -> {d.get('choice')} "
                      f"conf={d.get('confidence')} verdict={d.get('verdict')} "
                      f"{d.get('latency_ms')}ms")
    print("WARM-MCP ROUNDTRIP:", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)


asyncio.run(main())
