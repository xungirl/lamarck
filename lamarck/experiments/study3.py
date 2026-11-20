"""Study 3: confirmatory, fixed-n replication of Study 2 (PREREG.md, section "Study 3"), as code.

Hypothesis (direction from Study 2): with `qwen4-max` as proposer and selector, Spider held-out change after RSI
is greater than 0. Runs: exactly s3-src-1..10, seeds 21-30, each configuration equal to the corresponding
Study 2 run's except run id and seed (built by study2.source_plan; a test checks this). No run is added after
seeing results. Own experiment root (runs/study3) and own ledger (results/study3/budget/spend_ledger.json, cap
RMB 15.00); the other studies' ledgers are never opened by the gateway. The seed harness's evaluations on the
two Spider sub-splits come from the first study's cache, as in Study 2 (`lamarck seed-cache --from runs/rev3`).

Money gate as in Study 2 (lamarck.cli money_gate), per-run bound RUN_BOUND_RMB = Study 2's bound, which is above
Study 2's most expensive run.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from . import study2
from .plan import RunSpec, dependencies

__all__ = ["dependencies"]

REPO = Path(__file__).resolve().parents[2]
SEARCH = study2.SEARCH
POLICY_PARAMS = study2.POLICY_PARAMS
SEARCH_EXTRA = study2.SEARCH_EXTRA
SEARCH_MAX_TOKENS = study2.SEARCH_MAX_TOKENS
RUN_BUDGET = study2.RUN_BUDGET
SEEDS = tuple(range(21, 31))
PREFIX = "s3-src"
# every run of the plan is in the (only, primary) analysis: fixed n, no extension
PRIMARY_RUNS = tuple(f"{PREFIX}-{i}" for i in range(1, len(SEEDS) + 1))
CONTROLS = study2.CONTROLS
GROUP = "s3-source"
SPEND_FILE = REPO / "results" / "study3" / "budget" / "spend_ledger.json"
SPEND_CAP_RMB = 15.00
RUN_BOUND_RMB = study2.RUN_BOUND_RMB  # RMB 2.10
EST_RMB: dict[str, float] = {GROUP: RUN_BOUND_RMB}
UNFUNDED_GROUPS: set[str] = set()
DEFAULT_ROOT = REPO / "runs" / "study3"
PREVIOUS_ROOT = REPO / "runs" / "rev3"  # cache source for the seed harness's evaluations
ALPHA = 0.05  # one-sided


def build_plan() -> dict[str, RunSpec]:
    return study2.source_plan(PREFIX, SEEDS, GROUP)


def estimate(spend_file: Path = study2.SPEND_FILE) -> dict[str, Any]:
    """Per-run RMB of Study 2's ten runs (same protocol and models), from Study 2's ledger (read only)."""
    by = json.loads(spend_file.read_text())["by"]
    per_run = [by[f"run:s2-src-{i}"]["rmb"] for i in range(1, 11)]
    mean = statistics.fmean(per_run)
    return {
        "study2_rmb_per_run": [round(x, 4) for x in per_run],
        "mean_rmb_per_run": round(mean, 4),
        "max_rmb_per_run": round(max(per_run), 4),
        "ten_runs_at_mean": round(len(SEEDS) * mean, 3),
        "run_bound_rmb": RUN_BOUND_RMB,
        "cap_rmb": SPEND_CAP_RMB,
    }


if __name__ == "__main__":
    print(json.dumps(estimate(), indent=1))
