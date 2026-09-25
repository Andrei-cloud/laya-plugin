#!/usr/bin/env python
"""Rebuild the three bench arm HERMES_HOME configs from one shared base.

base  — no MCP; laya — stdio MCP (bin/laya-mcp, own audit file);
warm/guided — streamable-HTTP MCP to the launchd daemon (daemon audit file).
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))

BASE = """model:
  default: ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit
  provider: mlx-core
  max_tokens: 32768
  base_url: http://127.0.0.1:11234/v1
providers:
  mlx-core:
    name: MLXCore
    base_url: http://127.0.0.1:11234/v1
    model: ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit
plugins:
  disabled:
    - browser
    - macos
    - homeassistant
    - spotify
    - yuanbao
    - platforms/browser
    - platforms/macos
    - platforms/homeassistant
    - platforms/spotify
    - platforms/yuanbao
    - google_meet
    - kanban
memory:
  flush_on_session_end: false
terminal:
  cwd: .
"""

STDIO_MCP = """mcp_servers:
  laya:
    command: %s/../bin/laya-mcp
    timeout: 180
    connect_timeout: 60
    enabled: true
    env:
      LAYA_PYTHON: %s/../../laya/.venv-build/bin/python
      LAYA_ASSETS: %s/../../laya/release
      LAYA_SOURCE: %s/../../laya/models/source
      LAYA_UNIT: gpu
      LAYA_AUDIT: %s/audit-armB.jsonl
""" % (HERE, HERE, HERE, HERE, HERE)

WARM_MCP = """mcp_servers:
  laya:
    url: http://127.0.0.1:11270/mcp
    timeout: 180
    connect_timeout: 60
    enabled: true
"""

for arm, extra in (("hermes-base", ""), ("hermes-laya", STDIO_MCP),
                   ("hermes-warm", WARM_MCP)):
    d = os.path.join(HERE, arm)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "config.yaml"), "w") as f:
        f.write(BASE + extra)
    print("wrote", os.path.join(d, "config.yaml"))
