#!/usr/bin/env python
"""MCP STREAMABLE-HTTP server: the warm-resident deployment mode.

Same app/engine as laya_mcp (stdio), but the engine loads ONCE and every
harness session reuses it — the stdio per-session cold-load (~8 s) disappears.
GET /health -> 200 when warm (load+shape-warmup done). Binds 127.0.0.1 only.

  LAYA_ASSETS=... LAYA_SOURCE=... python -m server.laya_http_mcp --port 11270
Hermes:  printf '\n' | hermes mcp add laya --url http://127.0.0.1:11270/mcp
opencode v2: {"type":"remote","url":"http://127.0.0.1:11270/mcp"}
"""
import argparse
import json
import os
import sys
import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from laya_mcp import app  # same Server(app) with list_tools/call_tool handlers  # noqa: E402
from engine import Engine  # noqa: E402
import laya_api_http  # question API router (Jev-compatible wire; laya-named)  # noqa: E402

session_manager = StreamableHTTPSessionManager(app=app, stateless=True)


async def _asgi(scope, receive, send):
    if scope["type"] != "http":
        await _plain_404(scope, receive, send)
        return
    path = scope.get("path", "")
    if path == "/health":
        eng = Engine()  # singleton
        await _send_json(send, 200, json.dumps(
            laya_api_http.health_payload(eng)).encode())
        return
    if path.startswith("/mcp"):
        await session_manager.handle_request(scope, receive, send)
        return
    # question API: /v1/systemone + /v1/laya + /v1/models (one Engine)
    if await laya_api_http.handle(scope, receive, send, Engine()):
        return
    await _plain_404(scope, receive, send)


async def _plain_404(scope, receive, send):
    await _send_json(send, 404, b'{"error":"not found"}')


async def _send_json(send, code, body):
    await send({"type": "http.response.start", "status": code,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def _main(port=11270):
    # silence fd1 (stdout) across engine load+warmup: Core AI's ANE warnings
    # land there and uvicorn/MCP would mix them into the log stream.
    saved1 = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    try:
        Engine()          # blocks until loaded + shape-warmed
    finally:
        os.dup2(saved1, 1)
        os.close(saved1)
        os.close(devnull)
    import asyncio
    async with session_manager.run():
        config = uvicorn.Config(_asgi, host="127.0.0.1", port=port,
                                log_level="warning", log_config=None)
        server = uvicorn.Server(config)
        await server.serve()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11270)
    a = ap.parse_args()
    import asyncio
    asyncio.run(_main(a.port))
