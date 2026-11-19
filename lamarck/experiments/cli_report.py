"""`make report`: export finished runs of the revised plan to results/, write the cost table from the
RMB spend ledger, rebuild the dashboard.

Money is exact for the revised plan: the gateway prices every call from the usage the API returned
(lamarck/gateway/spend.py). Pre-revision usage (runs/main) is summarized in results/pre_revision/.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..dashboard.build import build as build_dashboard
from .plan import build_plan
from .report import SPEND_FILE
from .report import build as build_results

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "runs" / "rev3"
RESULTS = REPO / "results"


def cost_table(summary: dict[str, Any]) -> dict[str, Any]:
    sp = summary.get("spend") or {}
    models = [
        {"key": k, "calls": v["calls"], "prompt_tokens": v["prompt_tokens"], "cached_tokens": v["cached_tokens"],
         "completion_tokens": v["completion_tokens"], "rmb": v["rmb"]}
        for k, v in sorted((sp.get("by_role") or {}).items())
    ]  # fmt: skip
    plan = build_plan()
    groups: dict[str, dict[str, Any]] = {}
    for name, r in summary["runs"].items():
        g = groups.setdefault(
            plan[name].group,
            {"runs": 0, "policy_calls": 0, "search_calls": 0, "policy_tokens": 0, "rmb": 0.0},
        )
        g["runs"] += 1
        for k in ("policy_calls", "search_calls", "policy_tokens"):
            g[k] += r["cost"][k]
        g["rmb"] += r["cost"].get("rmb") or 0.0
    by_run = sp.get("by_run") or {}
    outside = {k: v["rmb"] for k, v in by_run.items() if k not in summary["runs"]}
    note = (
        f"RMB is exact for the revised plan: each call priced from the usage the API returned, cached input at the "
        f"cached rate, failed calls not billed. Total RMB {sp.get('spent_rmb', 0):.4f} of the RMB "
        f"{sp.get('cap_rmb', 0):.2f} cap; calls outside the exported runs (API check, aborted first launch, "
        f"held-out of unfinished runs): {', '.join(f'{k} {v:.4f}' for k, v in sorted(outside.items())) or 'none'}. "
        "Pre-revision usage is in results/pre_revision/summary.json (estimated)."
    )
    return {"note": note, "cap_rmb": sp.get("cap_rmb"), "spent_rmb": sp.get("spent_rmb"), "calls": sp.get("calls"),
            "prices_rmb_per_million": sp.get("prices_rmb_per_million"), "models": models,
            "groups": [{"group": k, **v} for k, v in sorted(groups.items())], "outside_runs": outside}  # fmt: skip


def main() -> None:
    summary = build_results(ROOT, RESULTS, SPEND_FILE)
    (RESULTS / "cost.json").write_text(json.dumps(cost_table(summary), indent=1))
    print(build_dashboard())


if __name__ == "__main__":
    main()
