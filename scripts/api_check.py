"""One real call through an in-process gateway with the RMB spend ledger (the 'test the API once'
step before any spending). Prints the HTTP outcome, usage and the RMB charged; never the key.

    uv run python scripts/api_check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lamarck.cli import KEYFILE, SPEND_CAP_RMB, SPEND_FILE
from lamarck.gateway.client import GatewayClient
from lamarck.stack import local_stack


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        stack = local_stack(Path(tmp), keyfile=KEYFILE, spend_file=SPEND_FILE, spend_cap=SPEND_CAP_RMB)
        tok = stack.grant("api-check", ["policy"], {"policy": 1})
        c = GatewayClient(stack.gateway_url, tok)
        out: dict[str, object] = {}
        try:
            r = c.chat(
                "policy",
                "qwen3.8-flash",
                [{"role": "user", "content": "Reply with the single word: ok"}],
                temperature=0.0,
                max_tokens=8,
                enable_thinking=False,
            )
            out = {"ok": True, "content": r.content[:40], "tokens": r.tokens}
        except Exception as exc:  # report, do not retry
            out = {"ok": False, "error": str(exc)[:300]}
        spent = json.loads(SPEND_FILE.read_text()) if SPEND_FILE.exists() else {}
        out["spent_rmb_total"] = spent.get("spent_rmb")
        print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
