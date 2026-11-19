from __future__ import annotations

import pytest

from lamarck.config import RunConfig
from lamarck.engine.regularizers import calibrate_delta, cost_rule, edit_budget, judge, prune_set
from lamarck.types import EvalResult, TaskOutcome


def ev(rewards: list[float], tokens: int = 100, tree: str = "t") -> EvalResult:
    return EvalResult(
        tree, "evolve", "m", 0, {f"x{i}": TaskOutcome(f"x{i}", r, tokens) for i, r in enumerate(rewards)}
    )


def test_edit_budget_anneals_from_max_to_min() -> None:
    table = [edit_budget(t, 5, 1, 3) for t in range(5)]
    assert table[0] == 3 and table[-1] == 1
    assert table == sorted(table, reverse=True)
    assert edit_budget(0, 1, 1, 3) == 3


def test_calibrate_floor_is_one_task_flip() -> None:
    cal = calibrate_delta([ev([1, 0, 1, 0]), ev([1, 0, 1, 0])])
    assert cal["delta"] == pytest.approx(0.25)


def test_calibrate_uses_observed_variation() -> None:
    a, b, c = ev([1] * 10 + [0] * 10), ev([1] * 14 + [0] * 6), ev([1] * 6 + [0] * 14)
    cal = calibrate_delta([a, b, c])
    assert cal["delta"] > 0.05
    assert cal["sd_direct"] > 0


def test_noise_floor_blocks_regressions_beyond_delta() -> None:
    cfg = RunConfig()
    inc = ev([1, 1, 1, 1, 0, 0, 0, 0])
    worse = ev([1, 1, 0, 0, 0, 0, 0, 0])
    d = judge("s1", worse, inc, best_score=0.5, delta=0.1, cfg=cfg)
    assert not d.admissible and "noise floor" in d.reason


def test_cost_rule_rejects_expensive_small_gains() -> None:
    cfg = RunConfig(beta0=0.1, beta1=1.0)
    ok, _ = cost_rule(0.2, 0.25, 0, 0.05, cfg)  # budget 0.1 + 0.2 = 0.3
    assert ok
    ok, _ = cost_rule(0.2, 0.5, 0, 0.05, cfg)
    assert not ok
    ok, _ = cost_rule(0.0, -0.2, 0, 0.05, cfg)  # within band but cheaper -> shaped > 0
    assert ok


def test_guards_are_non_compensatory() -> None:
    cfg = RunConfig()
    inc, cand = ev([0, 0, 0, 0]), ev([1, 1, 1, 1])
    d = judge("s1", cand, inc, 0.0, 0.1, cfg, guards=["unauthorized writes rose 0 -> 1"])
    assert not d.admissible and "guard" in d.reason


def test_disabled_floor_requires_strict_gain() -> None:
    cfg = RunConfig(use_noise_floor=False, use_cost_rule=False)
    inc = ev([1, 0])
    assert not judge("s1", ev([1, 0]), inc, 0.5, 0.0, cfg).admissible
    assert judge("s1", ev([1, 1]), inc, 0.5, 0.0, cfg).admissible


def test_prune_set() -> None:
    measured = [(0, ["a.md"], 0.1), (1, ["b.py"], -0.05), (2, ["b.py", "c.json"], 0.0)]
    assert prune_set(measured, 2, 3) == ["b.py", "c.json"]
    assert prune_set(measured, 9, 3) == ["a.md", "b.py", "c.json"]  # nothing recent -> all stale
