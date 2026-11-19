"""The paired statistics reproduce the SIM paper's Table 3 numbers from its per-run values."""

from __future__ import annotations

import pytest
from scipy import stats as sps

from lamarck.experiments.stats import average_ranks, paired_summary, signed_rank_exact, wilson_ci

# Table 3 of arXiv 2611.18856: final scores with SIM and without SIM for ten source runs
SIM = [22.42, 19.39, 20.61, 23.64, 20.61, 23.64, 21.82, 19.39, 23.03, 22.42]
NO_SIM = [20.61, 17.58, 20.00, 18.18, 18.79, 20.00, 20.00, 20.00, 18.18, 20.00]


def test_reproduces_paper_table3() -> None:
    s = paired_summary(SIM, NO_SIM)
    assert s["mean_diff"] == pytest.approx(2.36, abs=0.01)
    assert s["t"] == pytest.approx(4.06, abs=0.02)
    assert s["p_t_two_sided"] == pytest.approx(0.003, abs=0.0006)
    assert s["ci95"][0] == pytest.approx(1.05, abs=0.01) and s["ci95"][1] == pytest.approx(3.68, abs=0.01)
    assert s["d_z"] == pytest.approx(1.28, abs=0.01)
    assert s["wilcoxon"]["p_two_sided"] == pytest.approx(0.006, abs=0.001)
    assert (s["wins"], s["losses"]) == (9, 1)


def test_signed_rank_matches_scipy_without_ties() -> None:
    d = [1.5, -0.3, 2.2, 0.9, 3.1, -1.1, 0.4]
    ours = signed_rank_exact(d)["p_two_sided"]
    ref = sps.wilcoxon(d, method="exact").pvalue
    assert ours == pytest.approx(ref, abs=1e-12)


def test_average_ranks_and_zero_handling() -> None:
    assert average_ranks([3, 1, 3, 2]) == [3.5, 1.0, 3.5, 2.0]
    assert signed_rank_exact([0.0, 0.0])["p_two_sided"] == 1.0


def test_wilson() -> None:
    lo, hi = wilson_ci(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-3) and hi == pytest.approx(0.5962, abs=1e-3)
