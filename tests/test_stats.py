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


# Study 2: the seven flash-proposer controls (results/summary.json `source`, held-out after - before)
CONTROLS = [-2.5, -2.5, -2.5, 2.5, 0.0, 0.0, 5.0]


def test_one_sample_matches_scipy() -> None:
    from lamarck.experiments.stats import one_sample_summary

    d = [5.0, 2.5, 7.5, -2.5, 10.0]
    s = one_sample_summary(d)
    ref = sps.ttest_1samp(d, 0.0)
    assert s["mean"] == pytest.approx(4.5) and s["n"] == 5
    assert s["t"] == pytest.approx(float(ref.statistic)) and s["p_t_two_sided"] == pytest.approx(ref.pvalue)
    assert s["wilcoxon"]["p_two_sided"] == pytest.approx(sps.wilcoxon(d, method="exact").pvalue)
    c = one_sample_summary(CONTROLS)
    assert c["mean"] == pytest.approx(0.0) and c["p_t_two_sided"] == pytest.approx(1.0)


def test_two_sample_welch_and_exact_mann_whitney() -> None:
    from lamarck.experiments.stats import mann_whitney_exact, two_sample_summary

    a = [5.0, 7.5, 2.5, 10.0, 5.0]
    s = two_sample_summary(a, CONTROLS)
    ref = sps.ttest_ind(a, CONTROLS, equal_var=False)
    assert s["welch_t"] == pytest.approx(float(ref.statistic))
    assert s["p_welch_two_sided"] == pytest.approx(ref.pvalue)
    assert s["diff_of_means"] == pytest.approx(6.0)
    assert s["mann_whitney"]["assignments"] == 792  # C(12, 5)
    # without ties the enumeration equals scipy's exact distribution
    x, y = [1.0, 4.0, 6.0], [2.0, 3.0, 5.0, 7.0, 8.0]
    assert mann_whitney_exact(x, y)["p_two_sided"] == pytest.approx(
        sps.mannwhitneyu(x, y, method="exact").pvalue
    )
    # identical groups: nothing is more extreme than the centre
    assert mann_whitney_exact([1.0, 2.0], [1.0, 2.0])["p_two_sided"] == 1.0
    # zero variance in both groups does not crash
    z = two_sample_summary([0.0, 0.0], [0.0, 0.0])
    assert z["p_welch_two_sided"] == 1.0 and z["ci95_welch"] is None
