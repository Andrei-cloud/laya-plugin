#!/usr/bin/env python
"""Rebuild the three bench arm HERMES_HOME configs from one shared base.

base  — no MCP; laya — stdio MCP (bin/laya-mcp, own audit file);
warm/guided — streamable-HTTP MCP to the launchd daemon (daemon audit file).
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# Bench models (mlx-serve :11234). q38 = pinned teacher (existing arms);
# q36/q05 = faster-but-smaller challengers.
MODELS = {
    "q38": "ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit",
    "q36": "ddalcu/Qwen3.6-35B-A3B-MLX-Serve-4bit",
    "q05": "mlx-community/Qwen2.5-0.5B-Instruct-4bit",
}

BASE_TMPL = """model:
  default: {model}
  provider: mlx-core
  max_tokens: 32768
  base_url: http://127.0.0.1:11234/v1
providers:
  mlx-core:
    name: MLXCore
    base_url: http://127.0.0.1:11234/v1
    model: {model}
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

def home_dir(arm: str, model: str) -> str:
    """hermes-<arm> for the pinned teacher (q38, historical dirs), else
    hermes-<arm>-<model>."""
    return os.path.join(HERE, arm if model == "q38" else f"{arm}-{model}")


def main():
    for model, mid in MODELS.items():
        base = BASE_TMPL.format(model=mid)
        for arm, extra in (("hermes-base", ""), ("hermes-laya", STDIO_MCP),
                           ("hermes-warm", WARM_MCP)):
            d = home_dir(arm, model)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "config.yaml"), "w") as f:
                f.write(base + extra)
            print("wrote", os.path.join(d, "config.yaml"))


if __name__ == "__main__":
    main()
