"""Statistics for the paired study (paper Table 3 / App. C.4) and for descriptive intervals.

paired t      t = mean(d) / (sd(d) / sqrt(n)), df = n - 1, two-sided p, 95% t interval
signed-rank   exact two-sided Wilcoxon: zero differences dropped, average ranks for ties,
              every one of the 2^n sign assignments enumerated (no normal approximation)
d_z           mean(d) / sd(d)   (paired standardized effect)
one-sample    the paired machinery applied to a list of per-run changes (t and exact signed-rank vs 0)
one-sided     one-sample t and exact signed-rank vs 0 in the pre-specified direction mean > 0 (Study 3);
              the existing two-sided summaries are unchanged
two-sample    Welch t (unequal variances) and an exact two-sided Mann-Whitney test that enumerates every
              split of the pooled values into the two groups (average ranks for ties, so it stays exact
              with ties, conditional on the observed values)
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


def one_sample_summary(d: Sequence[float]) -> dict[str, Any]:
    """Per-run changes d vs 0: two-sided one-sample t, 95% t interval, exact signed-rank (Study 2)."""
    if len(d) < 2:
        raise ValueError("need >= 2 values")
    out = paired_summary(list(d), [0.0] * len(d))
    for k in ("mean_a", "mean_b", "mean_diff", "sd_diff"):
        out.pop(k)
    out["mean"] = statistics.fmean(d)
    out["sd"] = statistics.stdev(d)
    out["values"] = out.pop("diffs")
    return out


def signed_rank_exact_greater(diffs: Sequence[float], tol: float = 1e-12) -> dict[str, float]:
    """Exact one-sided Wilcoxon signed-rank p for H1 "location > 0": P(W+ >= observed W+) over every sign
    assignment (zeros dropped, average ranks for ties)."""
    d = [x for x in diffs if abs(x) > tol]
    n = len(d)
    if n == 0:
        return {"n_nonzero": 0, "w_plus": 0.0, "p_greater": 1.0}
    ranks = average_ranks([abs(x) for x in d])
    w_plus = sum(r for r, x in zip(ranks, d, strict=True) if x > 0)
    extreme = count = 0
    for signs in itertools.product((0, 1), repeat=n):
        w = sum(r for r, s in zip(ranks, signs, strict=True) if s)
        count += 1
        if w >= w_plus - 1e-9:
            extreme += 1
    return {"n_nonzero": n, "w_plus": w_plus, "p_greater": extreme / count, "assignments": count}


def one_sample_greater(d: Sequence[float]) -> dict[str, Any]:
    """Per-run changes d vs 0, one-sided H1 mean > 0: t, df, P(T_{n-1} >= t), and the exact one-sided
    signed-rank test. Complements one_sample_summary (two-sided p, 95% interval), which is unchanged."""
    if len(d) < 2:
        raise ValueError("need >= 2 values")
    n = len(d)
    mean = statistics.fmean(d)
    se = statistics.stdev(d) / math.sqrt(n)
    if se > 0:
        t = mean / se
        p = float(sps.t.sf(t, n - 1))
    else:
        t = math.inf if mean > 0 else (-math.inf if mean < 0 else 0.0)
        p = 0.0 if mean > 0 else 1.0
    return {"t": t, "df": n - 1, "p_t_greater": p, "wilcoxon_greater": signed_rank_exact_greater(d)}


def mann_whitney_exact(a: Sequence[float], b: Sequence[float], tol: float = 1e-9) -> dict[str, float]:
    """Exact two-sided Mann-Whitney U: every assignment of the pooled values to a group of size len(a)."""
    na, nb = len(a), len(b)
    if na == 0 or nb == 0:
        raise ValueError("both groups need values")
    pooled = list(a) + list(b)
    ranks = average_ranks(pooled)
    centre = na * nb / 2
    u_obs = sum(ranks[:na]) - na * (na + 1) / 2
    extreme = total = 0
    for idx in itertools.combinations(range(na + nb), na):
        u = sum(ranks[i] for i in idx) - na * (na + 1) / 2
        total += 1
        if abs(u - centre) >= abs(u_obs - centre) - tol:
            extreme += 1
    return {"u_a": u_obs, "n_a": na, "n_b": nb, "p_two_sided": extreme / total, "assignments": total}


def two_sample_summary(a: Sequence[float], b: Sequence[float]) -> dict[str, Any]:
    """Group a (treatment) vs group b (control): Welch t on the difference of means, exact Mann-Whitney."""
    if len(a) < 2 or len(b) < 2:
        raise ValueError("need >= 2 values per group")
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    va, vb = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    diff = ma - mb
    if se > 0:
        t = diff / se
        df = se**4 / ((va / len(a)) ** 2 / (len(a) - 1) + (vb / len(b)) ** 2 / (len(b) - 1))
        p = 2 * float(sps.t.sf(abs(t), df))
        half = float(sps.t.ppf(0.975, df)) * se
        ci: list[float] | None = [diff - half, diff + half]
    else:
        t, df, p, ci = (math.inf if diff else 0.0), float("nan"), (0.0 if diff else 1.0), None
    return {
        "n_a": len(a),
        "n_b": len(b),
        "mean_a": ma,
        "mean_b": mb,
        "sd_a": math.sqrt(va),
        "sd_b": math.sqrt(vb),
        "diff_of_means": diff,
        "welch_t": t,
        "welch_df": df,
        "p_welch_two_sided": p,
        "ci95_welch": ci,
        "mann_whitney": mann_whitney_exact(a, b),
    }
