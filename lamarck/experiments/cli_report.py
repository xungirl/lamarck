"""`make report`: export finished runs to results/, compute the cost estimate, rebuild the dashboard.

Cost estimate: only where a public list price was found (2026-11-19 web search). The numbers are
labelled ESTIMATE everywhere they appear.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..dashboard.build import build as build_dashboard
from .plan import build_plan
from .report import build as build_results

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "runs" / "main"
RESULTS = REPO / "results"

# CNY per 1M tokens (input, output). Source: Alibaba Cloud Model Studio price cut announced for
# 2026-08-27 as reported by IT Home / ifeng (https://tech.ifeng.com/c/8vvgXx2XLJp), retrieved
# 2026-11-19. The article does not state the unit; we assume per 1M tokens. No public list price
# was found for qwen4-max or deepseek-v4.1-flash on this endpoint.
PRICES_CNY_PER_M: dict[str, tuple[float, float]] = {"qwen3.8-flash": (0.80, 2.70)}
CNY_PER_USD = 7.1  # rough conversion, for orientation only


def cost_table(summary: dict[str, Any]) -> dict[str, Any]:
    models = []
    for key, v in sorted(summary["ledger"].get("models", {}).items()):
        _role, model = key.split("|", 1)
        usd = None
        if model in PRICES_CNY_PER_M:
            pin, pout = PRICES_CNY_PER_M[model]
            usd = (v["prompt_tokens"] * pin + v["completion_tokens"] * pout) / 1e6 / CNY_PER_USD
        models.append({"key": key, "calls": v["calls"], "prompt_tokens": v["prompt_tokens"],
                       "completion_tokens": v["completion_tokens"], "usd": usd})  # fmt: skip
    plan = build_plan()
    groups: dict[str, dict[str, int]] = {}
    for name, r in summary["runs"].items():
        g = groups.setdefault(
            plan[name].group, {"runs": 0, "policy_calls": 0, "search_calls": 0, "policy_tokens": 0}
        )
        g["runs"] += 1
        for k in ("policy_calls", "search_calls", "policy_tokens"):
            g[k] += r["cost"][k]
    note = (
        "Token counts are exact (gateway ledger). USD is an ESTIMATE for qwen3.8-flash only, from a reported "
        "list price of CNY 0.80 / 2.70 per 1M input / output tokens (assumed unit) at 7.1 CNY/USD, retrieved "
        "2026-11-19; no public price was found for qwen4-max or deepseek-v4.1-flash. Ledger totals include "
        "pilot/calibration evaluations and the discarded first attempts."
    )
    return {"note": note, "prices_cny_per_million": PRICES_CNY_PER_M, "cny_per_usd": CNY_PER_USD,
            "models": models, "groups": [{"group": k, **v} for k, v in sorted(groups.items())]}  # fmt: skip


def main() -> None:
    summary = build_results(ROOT, RESULTS)
    (RESULTS / "cost.json").write_text(json.dumps(cost_table(summary), indent=1))
    print(build_dashboard())


if __name__ == "__main__":
    main()
