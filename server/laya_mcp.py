"""MCP stdio server: exposes the Laya decision asset to any MCP harness
(Hermes: hermes mcp add; Claude Desktop / opencode mcp: same config shape).

Tools (D2 surface replace; wire semantics per spec-v2 §1):
  laya_ask      raw named wire questions (noul|choice|score) passthrough —
                the escape hatch: answers carry the §1 invariants
  laya_guard    one-call safety verdict for a proposed command/action
  laya_status   engine health, head chains, call stats

Run: python -m server.laya_mcp   (env: LAYA_ASSETS, LAYA_SOURCE, LAYA_UNIT)
"""
import asyncio
import json
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Engine, ask_guardrail  # noqa: E402

ENGINE: Engine | None = None
LOCK = asyncio.Lock()

app = Server("laya-decision", version="0.2.0")

ASK_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {"type": "string",
                  "description": "Agent state: last action/command + short "
                                 "context. Plain text or JSON; <=60 000 chars."},
        "questions": {
            "type": "object",
            "description": "Named wire questions, one per key: "
                           "{name: {type:'noul'|'choice'|'score', "
                           "instructions:str, criteria?: {opt:meaning} | "
                           "[levels lowest-first]}}. noul carries no "
                           "criteria; choice needs >=2 options; score "
                           "needs >=2 rubric levels.",
        },
        "model": {"type": "string",
                  "description": "Optional id; echoed back verbatim."},
    },
    "required": ["state", "questions"],
}

GUARD_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string",
                    "description": "The exact command or action about to run."},
    },
    "required": ["command"],
}


def _engine() -> Engine:
    global ENGINE
    if ENGINE is None:
        ENGINE = Engine()
    return ENGINE


async def _decide(task: str, state: str, options=None) -> dict:
    eng = _engine()
    async with LOCK:  # one Core AI session per process; serialize passes
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: eng.decide(task, state, options))


async def _ask(arguments: dict) -> dict:
    """Raw wire-question passthrough — the SAME validate→answer→validate
    path as the HTTP question API (laya_api_http.answer_request): MCP and
    HTTP cannot drift in semantics or invariants (DRY + D8-conformance)."""
    import laya_api_http
    eng = _engine()
    async with LOCK:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: laya_api_http.answer_request(arguments, eng))


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="laya_ask",
             description="Ask the Laya decision model named wire questions in "
                         "one call: types noul (yes/no probability), choice "
                         "(closed-set pick + calibrated probabilities), score "
                         "(rubric levels, lowest first). Answers satisfy the "
                         "wire invariants (Σp=1, choice=argmax, score matches "
                         "its distribution); confidence<0.6 means escalate to "
                         "the LLM / human. Escape hatch for any harness.",
             inputSchema=ASK_SCHEMA),
        Tool(name="laya_guard",
             description="Safety-gate one proposed command/action before running it. "
                         "Returns allow/ask/block + confidence. block/ask => do not run "
                         "without explicit user confirmation.",
             inputSchema=GUARD_SCHEMA),
        Tool(name="laya_status",
             description="Plugin health: asset loaded, chains, mean latency, call count.",
             inputSchema={"type": "object", "properties": {}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "laya_ask":
            out = await _ask(arguments)
        elif name == "laya_guard":
            # verdict rail lives in ONE place (engine.ask_guardrail) shared
            # with the use-case tools and mirrored in the opencode client.
            eng = _engine()
            async with LOCK:
                loop = asyncio.get_event_loop()
                out = await loop.run_in_executor(
                    None, lambda: ask_guardrail(
                        eng, arguments["command"]))
        elif name == "laya_status":
            e = _engine()
            out = {"ok": True, "unit": os.environ.get("LAYA_UNIT", "gpu"),
                   "chains": e.chain_order, "calls": e.calls,
                   "mean_latency_ms": round(e.total_ms / max(1, e.calls), 2)}
        else:
            raise ValueError(f"unknown tool {name}")
        return [TextContent(type="text", text=json.dumps(out))]
    except Exception as e:  # never crash the harness over a decision
        code = getattr(e, "code", None)  # structured refusals keep their code
        return [TextContent(type="text",
                            text=json.dumps({"error": code or f"{type(e).__name__}: {e}",
                                             "fallback": "proceed with normal judgement"}))]


def main():
    # Engine load prints benign ANE-validation warnings to fd1 — which IS the
    # MCP stdio channel. Silence fd1 for the load+warmup window (Engine.__init__
    # blocks until the worker finished warming, so the window is exact), then
    # restore before the SDK starts writing JSON-RPC frames.
    import sys
    saved1 = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    try:
        _engine()  # fail fast at startup if the asset is missing (visible in `hermes mcp test`)
    finally:
        os.dup2(saved1, 1)
        os.close(saved1)
        os.close(devnull)

    async def run():
        async with stdio_server() as (r, w):
            await app.run(r, w, app.create_initialization_options())
    asyncio.run(run())


if __name__ == "__main__":
    main()
