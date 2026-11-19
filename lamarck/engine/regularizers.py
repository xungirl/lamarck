"""RRSI's regularizers as small pure functions.

Re-implemented from the definitions in RRSI (Xia et al., arXiv 2609.24972;
reference code github.com/google-research/rrsi, Apache-2.0):

  edit budget      b_t = ceil(b_min + (b_max - b_min) * (1 + cos(pi t / T)) / 2)
  noise band       delta = z * sd(S(H) - S'(H)) for two evaluations of the SAME harness
  noise floor      admissible only if S' >= S* - delta   (S* = best incumbent score so far)
  cost rule        dS > delta:  dC <= beta0 + beta1 * dS
                   otherwise:   w_s dS - w_c dC + w_n * novelty > 0
  prune set        components whose best recent measured gain is <= 0

All scores are fractions in [0, 1]; dC is the RELATIVE change of mean tokens per task.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config import RunConfig
from ..types import EvalResult


def edit_budget(t: int, rounds: int, b_min: int, b_max: int) -> int:
    """Annealed cap on independent edits per candidate for round t (0-indexed)."""
    if rounds <= 1:
        return b_max
    t = max(0, min(t, rounds - 1))
    v = b_min + (b_max - b_min) * 0.5 * (1.0 + math.cos(math.pi * t / (rounds - 1)))
    return math.ceil(round(v, 9))


def relative_cost_change(c_new: float, c_old: float) -> float:
    return 0.0 if c_old <= 0 else (c_new - c_old) / c_old


def calibrate_delta(
    base_evals: Sequence[EvalResult], z: float = 2.0, reps: int = 2000, seed: int = 7
) -> dict[str, Any]:
    """Noise band from evaluations of the unchanged base harness.

    With >= 2 repeated evaluations the null difference of two evaluations is
    observed directly: sd_null = sqrt(2) * sd(S_r). Per-task paired resampling of
    the repeats gives a second estimate that does not collapse to 0 when only a
    few tasks flip. The band is floored at one task flip (1/n), the smallest
    difference an evaluation can resolve.
    """
    if not base_evals:
        raise ValueError("need at least one base evaluation")
    n = len(base_evals[0].outcomes)
    floor = 1.0 / max(1, n)
    scores = [e.score for e in base_evals]
    sd_direct = statistics.stdev(scores) * math.sqrt(2) if len(scores) >= 2 else 0.0
    sd_boot = 0.0
    if len(base_evals) >= 2:
        rng = random.Random(seed)
        tasks = sorted(base_evals[0].outcomes)
        diffs = []
        for _ in range(reps):
            a, b = rng.sample(range(len(base_evals)), 2)
            pick = [rng.choice(tasks) for _ in tasks]
            sa = sum(base_evals[a].outcomes[t].reward for t in pick) / len(pick)
            sb = sum(base_evals[b].outcomes[t].reward for t in pick) / len(pick)
            diffs.append(sa - sb)
        sd_boot = statistics.pstdev(diffs)
    sd = max(sd_direct, sd_boot)
    return {
        "delta": round(max(z * sd, floor), 6),
        "z": z,
        "sd_direct": round(sd_direct, 6),
        "sd_bootstrap": round(sd_boot, 6),
        "floor_one_task": round(floor, 6),
        "n_evals": len(base_evals),
        "n_tasks": n,
        "scores": [round(s, 6) for s in scores],
    }


@dataclass
class Decision:
    """Why a candidate is or is not admissible (persisted in the run history)."""

    sample: str
    admissible: bool
    reason: str
    score: float | None = None
    cost: float | None = None
    d_score: float | None = None
    d_cost: float | None = None
    guards: list[str] = field(default_factory=list)


def cost_rule(d_score: float, d_cost: float, novelty: int, delta: float, cfg: RunConfig) -> tuple[bool, str]:
    if d_score > delta:
        budget = cfg.beta0 + cfg.beta1 * d_score
        ok = d_cost <= budget
        return (
            ok,
            f"gain {d_score:+.4f} > delta {delta:.4f}; cost {d_cost:+.3f} {'<=' if ok else '>'} {budget:.3f}",
        )
    shaped = cfg.w_s * d_score - cfg.w_c * d_cost + cfg.w_n * novelty
    ok = shaped > 0
    return ok, f"gain {d_score:+.4f} within delta {delta:.4f}; shaped {shaped:+.3f} {'>' if ok else '<='} 0"


def judge(
    sample: str,
    cand: EvalResult | None,
    incumbent: EvalResult,
    best_score: float,
    delta: float,
    cfg: RunConfig,
    guards: list[str] | None = None,
    novelty: int = 0,
    gate_failure: str = "",
) -> Decision:
    """Admissibility of one evaluated candidate (RRSI Algorithm 2, one line per rule)."""
    if cand is None:
        return Decision(sample, False, gate_failure or "not evaluated")
    d_s = cand.score - incumbent.score
    d_c = relative_cost_change(cand.cost, incumbent.cost)
    d = Decision(sample, False, "", cand.score, cand.cost, d_s, d_c, list(guards or []))
    if cfg.use_noise_floor:
        if cand.score < best_score - delta:
            d.reason = f"below noise floor: {cand.score:.4f} < S* {best_score:.4f} - delta {delta:.4f}"
            return d
    elif d_s <= 0:
        d.reason = f"no gain: {d_s:+.4f} (noise floor disabled, strict improvement required)"
        return d
    why = "cost rule disabled"
    if cfg.use_cost_rule:
        ok, why = cost_rule(d_s, d_c, novelty, delta, cfg)
        if not ok:
            d.reason = f"cost rule failed: {why}"
            return d
    if guards:
        d.reason = "domain guard violated: " + "; ".join(guards)
        return d
    d.admissible = True
    d.reason = f"admissible: {why}"
    return d


def prune_set(measured: Sequence[tuple[int, list[str], float]], t: int, n_prune: int) -> list[str]:
    """Components whose best measured gain within the last n_prune rounds is <= 0.

    `measured` holds (round, components touched, d_score) for every evaluated
    candidate. Components never measured are not in the set.
    """
    best: dict[str, float] = {}
    seen: set[str] = set()
    for rnd, comps, d_s in measured:
        for c in comps:
            seen.add(c)
            if t - rnd <= n_prune:
                best[c] = max(best.get(c, -math.inf), d_s)
    return sorted(c for c in seen if best.get(c, -math.inf) <= 0)
