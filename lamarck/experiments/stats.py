"""Statistics for the paired study (paper Table 3 / App. C.4) and for descriptive intervals.

paired t      t = mean(d) / (sd(d) / sqrt(n)), df = n - 1, two-sided p, 95% t interval
signed-rank   exact two-sided Wilcoxon: zero differences dropped, average ranks for ties,
              every one of the 2^n sign assignments enumerated (no normal approximation)
d_z           mean(d) / sd(d)   (paired standardized effect)
bootstrap     percentile interval for a mean over tasks (descriptive)
"""

from __future__ import annotations

import itertools
import math
import random
import statistics
from collections.abc import Sequence
from typing import Any

from scipy import stats as sps


def average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    return ranks


def signed_rank_exact(diffs: Sequence[float], tol: float = 1e-12) -> dict[str, float]:
    d = [x for x in diffs if abs(x) > tol]
    n = len(d)
    if n == 0:
        return {"n_nonzero": 0, "w_plus": 0.0, "p_two_sided": 1.0}
    ranks = average_ranks([abs(x) for x in d])
    w_plus = sum(r for r, x in zip(ranks, d, strict=True) if x > 0)
    total = sum(ranks)
    mean = total / 2
    obs = abs(w_plus - mean)
    extreme = 0
    count = 0
    for signs in itertools.product((0, 1), repeat=n):
        w = sum(r for r, s in zip(ranks, signs, strict=True) if s)
        count += 1
        if abs(w - mean) >= obs - 1e-9:
            extreme += 1
    return {"n_nonzero": n, "w_plus": w_plus, "p_two_sided": extreme / count, "assignments": count}


def paired_summary(a: Sequence[float], b: Sequence[float]) -> dict[str, Any]:
    """Compare paired samples a (treatment) vs b (control): differences a - b."""
    if len(a) != len(b) or len(a) < 2:
        raise ValueError("need >= 2 pairs of equal length")
    d = [x - y for x, y in zip(a, b, strict=True)]
    n = len(d)
    mean = statistics.fmean(d)
    sd = statistics.stdev(d)
    se = sd / math.sqrt(n)
    if se > 0:
        t = mean / se
        p = 2 * float(sps.t.sf(abs(t), n - 1))
        half = float(sps.t.ppf(0.975, n - 1)) * se
    else:
        t, p, half = (math.inf if mean else 0.0), (0.0 if mean else 1.0), 0.0
    return {
        "n": n,
        "mean_a": statistics.fmean(a),
        "mean_b": statistics.fmean(b),
        "mean_diff": mean,
        "sd_diff": sd,
        "ci95": [mean - half, mean + half],
        "t": t,
        "df": n - 1,
        "p_t_two_sided": p,
        "wilcoxon": signed_rank_exact(d),
        "d_z": (mean / sd) if sd > 0 else math.inf,
        "wins": sum(x > 0 for x in d),
        "losses": sum(x < 0 for x in d),
        "ties": sum(x == 0 for x in d),
        "diffs": d,
    }


def bootstrap_mean_ci(values: Sequence[float], reps: int = 5000, seed: int = 0) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(values)
    means = sorted(statistics.fmean(rng.choices(values, k=n)) for _ in range(reps))
    return means[int(0.025 * reps)], means[int(0.975 * reps) - 1]


def wilson_ci(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """Wilson score interval for a proportion k/n (descriptive interval for one evaluation)."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return centre - half, centre + half
