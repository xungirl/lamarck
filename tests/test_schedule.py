"""The scheduler's money gate and the revised plan (PREREG deviation 3)."""

from __future__ import annotations

from lamarck.cli import money_gate
from lamarck.experiments.plan import EST_RMB, UNFUNDED_GROUPS, build_plan, dependencies


def spend(spent: float, cap: float = 9.5, inflight: float = 0.0, by: dict[str, float] | None = None) -> dict:
    return {
        "cap_rmb": cap,
        "spent_rmb": spent,
        "in_flight_rmb": inflight,
        "by": {f"run:{k}": {"rmb": v} for k, v in (by or {}).items()},
    }


def test_gate_allows_in_priority_order_and_reserves_for_blocked_runs() -> None:
    order = ["a", "b", "c"]
    est = {"a": 1.0, "b": 1.0, "c": 1.0}
    # 2.5 left: a and b fit, c does not
    assert money_gate(order, order, [], est, spend(7.0)) == {"a", "b"}
    # "a" is waiting for a dependency but still claims its money before "c"
    assert money_gate(order, ["a", "c"], [], est, spend(8.0)) == {"a"}


def test_gate_counts_remaining_estimate_of_running_runs() -> None:
    est = {"r": 2.0, "x": 1.0}
    # r has spent 1.5 of its 2.0 estimate (spent includes it): 9.5 - 5 - 0.5 = 4.0 left for x
    assert money_gate(["x"], ["x"], ["r"], est, spend(5.0, by={"r": 1.5})) == {"x"}
    # a run that spent more than its estimate claims nothing more
    assert money_gate(["x"], ["x"], ["r"], est, spend(8.4, by={"r": 3.0})) == {"x"}
    assert money_gate(["x"], ["x"], ["r"], est, spend(8.6, by={"r": 3.0})) == set()
    # in-flight reservations count against the money left
    assert money_gate(["x"], ["x"], [], est, spend(8.0, inflight=0.6)) == set()


def test_revised_plan_matches_deviation_3() -> None:
    plan = build_plan()
    for spec in plan.values():
        assert spec.cfg.search_model == "qwen3.8-flash"
        assert (spec.cfg.rounds, spec.cfg.k) == (3, 2)
        assert spec.group in EST_RMB
        for d in dependencies(spec):
            assert list(plan).index(d) < list(plan).index(spec.name)  # priority order respects deps
    funded = [s for s in plan.values() if s.group not in UNFUNDED_GROUPS]
    assert all(s.cfg.policy_model == "qwen3.8-flash" for s in funded)
    pairs = [n for n in plan if n.startswith("sql-full-")]
    assert pairs == ["sql-full-1", "sql-full-2", "sql-full-3"]
    assert plan["sql-src-1"].cfg.evolve_split == "spider_evolve_s30"
    assert plan["sql-full-1"].cfg.heldout_split == "bird_heldout_s40"
    assert plan["so-src-1"].cfg.evolve_split == "so_evolve_s20"
    assert [n for n in plan if n.startswith("so-") and n != "so-src-1"] == [
        "so-full",
        "so-transfer",
        "so-cold",
        "so-problems",
    ]
