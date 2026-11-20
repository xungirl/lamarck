"""Study 2: strong search model (PREREG.md, section "Study 2"), as code.

Question: with `qwen4-max` as proposer and selector and everything else identical to the seven Spider
source runs of the revised plan (sql-src-1..7, `qwen3.8-flash` in every role), does Spider held-out improve?

Runs: s2-src-1..5, seeds 11-15 (the first study used 1-7; PREREG reserved 1-10), configuration identical to
sql-src-i except run id, seed and search model (a test checks this). Extension (PREREG, Study 2 deviation 1):
s2-src-6..10, seeds 16-20, added after the n = 5 result; runs 1-5 (PRIMARY_RUNS) stay the primary analysis. They live in their own experiment root
(runs/study2) and spend from their own ledger (results/study2/budget/spend_ledger.json, cap RMB 9.00); the
first study's ledger (RMB 9.50 cap) is never opened. The seed harness's evaluations on the two Spider sub-splits
are taken from the first study's cache (same harness, policy model, split and rollout: `lamarck seed-cache`).

Money gate: a run is launched only if its conservative per-run cost bound RUN_BOUND_RMB still fits under the
cap after the money spent, in-flight reservations and the remaining bound of every running run (lamarck.cli
money_gate). Runs launch in seed order; a run the gate never admits is reported as not run. The gateway's cap
is the hard stop. RUN_BOUND_RMB is the "high" scenario of estimate() rounded up to RMB 0.05.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..gateway.spend import PRICES
from . import plan as rev3
from .plan import QWEN, SPIDER_EVOLVE, SPIDER_HELDOUT, RunSpec, dependencies

__all__ = ["dependencies"]

REPO = Path(__file__).resolve().parents[2]
SEARCH = "qwen4-max"
POLICY_PARAMS: dict[str, dict[str, Any]] = rev3.POLICY_PARAMS  # policy settings identical to the controls
# Search settings as in the pre-revision qwen4-max run (deviation 1): thinking off. max_tokens is SearchLLM's
# default (8,192), as in that run and in the controls.
SEARCH_EXTRA: dict[str, Any] = dict(rev3.SEARCH_EXTRA)
SEARCH_MAX_TOKENS = 8192
SEEDS = (11, 12, 13, 14, 15, 16, 17, 18, 19, 20)
# the pre-registered n = 5 runs: the primary analysis, never replaced; runs 6-10 are the extension
PRIMARY_RUNS = tuple(f"s2-src-{i}" for i in range(1, 6))
EXTENSION_RUNS = tuple(f"s2-src-{i}" for i in range(6, 11))
CONTROLS = tuple(f"sql-src-{i}" for i in range(1, rev3.N_SQL_SOURCES + 1))
GROUP = "s2-source"
SPEND_FILE = REPO / "results" / "study2" / "budget" / "spend_ledger.json"
SPEND_CAP_RMB = 9.00
RUN_BOUND_RMB = 2.10  # conservative per-run cost bound used by the money gate (see estimate())
EST_RMB: dict[str, float] = {GROUP: RUN_BOUND_RMB}
UNFUNDED_GROUPS: set[str] = set()
RUN_BUDGET = rev3.RUN_BUDGET  # per-run call caps, unchanged
DEFAULT_ROOT = REPO / "runs" / "study2"
PREVIOUS_ROOT = REPO / "runs" / "rev3"  # cache source for the seed harness's evaluations


def build_plan() -> dict[str, RunSpec]:
    plan: dict[str, RunSpec] = {}
    for i, seed in enumerate(SEEDS, 1):
        name = f"s2-src-{i}"
        cfg = rev3._cfg(name, "text2sql", SPIDER_EVOLVE, SPIDER_HELDOUT, QWEN, "full", seed, False)
        plan[name] = RunSpec(name, replace(cfg, search_model=SEARCH), group=GROUP)
    return plan


def estimate(
    pre_revision: Path = REPO / "results" / "pre_revision" / "summary.json",
    spend_file: Path = REPO / "results" / "budget" / "spend_ledger.json",
) -> dict[str, Any]:
    """Per-run RMB estimate of a Study 2 source run from the two ledgers.

    Per-call tokens of qwen4-max come from the pre-revision gateway ledger (successful proposer / selector
    calls, all pre-revision runs). Calls per run and the policy cost come from the spend ledger of sql-src-1..7
    (same protocol and splits). Scenarios:
      low      prompts at the sizes the flash proposer/selector saw on these sub-splits, mean calls,
               qwen4-max output lengths, policy = 2 x the controls' mean
      central  qwen4-max pre-revision prompt sizes (full splits, k = 3: larger), mean calls, policy 2 x mean
      high     as central with the maximum observed calls per run and policy = 2 x the controls' maximum
    The policy factor 2: the pre-revision qwen4-max run roughly doubled policy tokens per held-out task
    (477 -> 936), because its harness made two policy calls per task.
    """
    pre = json.loads(pre_revision.read_text())["ledger"]["models"]
    by = json.loads(spend_file.read_text())["by"]
    p_in, p_out, _ = PRICES[SEARCH]

    def per_call(key: str) -> tuple[float, float]:
        m = pre[key]
        ok = m["calls"] - m["failed"]
        return m["prompt_tokens"] / ok, m["completion_tokens"] / ok

    (prop_in, prop_out), (sel_in, sel_out) = per_call("proposer|qwen4-max"), per_call("selector|qwen4-max")
    rr = [{role: by[f"run_role:{r}|{role}"] for role in ("policy", "proposer", "selector")} for r in CONTROLS]
    prop_calls = [r["proposer"]["calls"] for r in rr]
    sel_calls = [r["selector"]["calls"] for r in rr]
    policy = [r["policy"]["rmb"] for r in rr]
    flash_prop_in = sum(r["proposer"]["prompt_tokens"] for r in rr) / sum(prop_calls)
    flash_sel_in = sum(r["selector"]["prompt_tokens"] for r in rr) / sum(sel_calls)

    def call(t_in: float, t_out: float) -> float:
        return (t_in * p_in + t_out * p_out) / 1e6

    mean_p, mean_s = sum(prop_calls) / len(rr), sum(sel_calls) / len(rr)
    scen = {
        "low": {
            "proposer": mean_p * call(flash_prop_in, prop_out),
            "selector": mean_s * call(flash_sel_in, sel_out),
            "policy": 2 * sum(policy) / len(policy),
        },
        "central": {
            "proposer": mean_p * call(prop_in, prop_out),
            "selector": mean_s * call(sel_in, sel_out),
            "policy": 2 * sum(policy) / len(policy),
        },
        "high": {
            "proposer": max(prop_calls) * call(prop_in, prop_out),
            "selector": max(sel_calls) * call(sel_in, sel_out),
            "policy": 2 * max(policy),
        },
    }
    for s in scen.values():
        s["run"] = s["proposer"] + s["selector"] + s["policy"]
        s["five_runs"] = 5 * s["run"]
    return {
        "inputs": {
            "qwen4max_proposer_tokens_per_call": [round(prop_in), round(prop_out)],
            "qwen4max_selector_tokens_per_call": [round(sel_in), round(sel_out)],
            "flash_prompt_tokens_per_call_on_subsplits": [round(flash_prop_in), round(flash_sel_in)],
            "proposer_calls_per_run": prop_calls,
            "selector_calls_per_run": sel_calls,
            "control_policy_rmb_per_run": [round(x, 4) for x in policy],
            "price_rmb_per_million": [p_in, p_out],
        },
        "scenarios": {k: {kk: round(vv, 3) for kk, vv in v.items()} for k, v in scen.items()},
        "run_bound_rmb": RUN_BOUND_RMB,
        "cap_rmb": SPEND_CAP_RMB,
    }


if __name__ == "__main__":
    print(json.dumps(estimate(), indent=1))
