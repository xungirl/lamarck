"""One tiny real call per model through an in-process gateway with the RMB spend ledger (the 'test the API
once' step before any spending). Prints the HTTP outcome, usage and the RMB charged; never the key.
Exits non-zero if any call failed, so a launcher stops there.

    uv run python scripts/api_check.py                  # first study: qwen3.8-flash, ledger results/budget/
    uv run python scripts/api_check.py --plan study2    # Study 2: qwen3.8-flash and qwen4-max,
                                                        # ledger results/study2/budget/ (cap RMB 9.00)
    ... --simulate text2sql --spend-file F              # offline dry run: simulated upstream, no network

The spend ledger is owned by one process at a time: no gateway may be running on the same ledger.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lamarck.cli import KEYFILE, plan_money
from lamarck.gateway.client import GatewayClient
from lamarck.stack import local_stack

# (role, model, extra request params): the settings the plan's runs use, thinking off
CHECKS = {
    "rev3": [("policy", "qwen3.8-flash")],
    "study2": [("policy", "qwen3.8-flash"), ("proposer", "qwen4-max")],
}


def main() -> None:
    ap = argparse.ArgumentParser(description="one tiny call per model of a plan")
    ap.add_argument("--plan", choices=sorted(CHECKS), default="rev3")
    ap.add_argument("--spend-file", type=Path, default=None, help="default: the plan's ledger")
    ap.add_argument("--simulate", choices=("text2sql", "toy"), default=None, help="offline, no key read")
    a = ap.parse_args()
    spend_file, cap = plan_money(a.plan)
    spend_file = a.spend_file or spend_file
    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as tmp:
        stack = local_stack(
            Path(tmp),
            keyfile=None if a.simulate else KEYFILE,
            spend_file=spend_file,
            spend_cap=cap,
            simulate=a.simulate,
        )
        for role, model in CHECKS[a.plan]:
            tok = stack.grant("api-check", [role], {role: 1})
            c = GatewayClient(stack.gateway_url, tok)
            out: dict[str, object] = {"role": role, "model": model}
            try:
                r = c.chat(
                    role,
                    model,
                    [{"role": "user", "content": "Reply with the single word: ok"}],
                    temperature=0.0,
                    max_tokens=8,
                    enable_thinking=False,
                )
                out.update(
                    ok=True,
                    content=r.content[:40],
                    prompt_tokens=r.prompt_tokens,
                    completion_tokens=r.completion_tokens,
                )
            except Exception as exc:  # report, do not retry
                out.update(ok=False, error=str(exc)[:300])
            results.append(out)
        spent = json.loads(spend_file.read_text()) if spend_file.exists() else {}
        by = spent.get("by", {})
        for out in results:
            out["rmb_this_model_api_checks"] = by.get(f"run_role:api-check|{out['role']}", {}).get("rmb")
    report = {
        "plan": a.plan,
        "simulated": bool(a.simulate),
        "spend_file": str(spend_file),
        "cap_rmb": cap,
        "spent_rmb_total": spent.get("spent_rmb"),
        "checks": results,
    }
    print(json.dumps(report, indent=1))
    sys.exit(0 if all(r["ok"] for r in results) else 1)


if __name__ == "__main__":
    main()
