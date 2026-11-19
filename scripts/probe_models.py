"""Probe the policy, search and cross-family models through the gateway (a handful of calls, all in the ledger)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from lamarck.gateway.client import GatewayClient, create_grant

root = Path(sys.argv[1] if len(sys.argv) > 1 else "runs/main")
url = "http://127.0.0.1:18080"
secret = (root / "gateway-state" / "admin_secret").read_text().strip()
tok = create_grant(url, secret, "probe-models", ["policy", "proposer"], budget={"policy": 12, "proposer": 3})
c = GatewayClient(url, tok)
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": "Get an order",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    }
]
out = {}
for model, extra in [("qwen3.8-flash", {"enable_thinking": False}), ("deepseek-v4.1-flash", {})]:
    for i in range(2):
        t0 = time.time()
        r = c.chat(
            "policy",
            model,
            [{"role": "user", "content": "Return only SQL: count rows of table singer."}],
            temperature=0.0,
            max_tokens=200,
            seed=1234,
            **extra,
        )
        out[f"{model} sql #{i}"] = {
            "content": r.content[:120],
            "tokens": r.tokens,
            "s": round(time.time() - t0, 2),
        }
    r = c.chat(
        "policy",
        model,
        [{"role": "user", "content": "What is the status of order ORD-10002?"}],
        tools=tools,
        temperature=0.0,
        max_tokens=200,
        **extra,
    )
    out[f"{model} tool"] = {"tool_calls": r.tool_calls, "content": r.content[:80]}
t0 = time.time()
r = c.chat(
    "proposer",
    "qwen4-max",
    [{"role": "user", "content": 'Reply with JSON {"ok": true} only.'}],
    temperature=0.8,
    max_tokens=50,
)
out["qwen4-max"] = {"content": r.content, "tokens": r.tokens, "s": round(time.time() - t0, 2)}
print(json.dumps(out, indent=1))
