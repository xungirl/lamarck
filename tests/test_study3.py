"""Study 3 (fixed-n replication of Study 2): plan equal to Study 2's except run ids and seeds, own ledger and
cap, analysis of exactly s3-src-1..10 with the pre-registered verdict rule; and Study 2's report output is
unchanged by the generalization (byte-identical to the committed results)."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from lamarck.cli import money_gate, plan_module, plan_money, plan_root
from lamarck.experiments import study2, study3
from lamarck.experiments.stats import one_sample_greater, one_sample_summary, two_sample_summary

REPO = Path(__file__).resolve().parents[1]


def _assert_close(a: Any, b: Any, path: str = "") -> None:
    """Deep equality with floats compared at rel 1e-9 (cross-platform libm/scipy last-bit differences)."""
    if isinstance(a, float) or isinstance(b, float):
        assert isinstance(a, (int, float)) and isinstance(b, (int, float)), path
        assert a == pytest.approx(b, rel=1e-9, abs=1e-12), path
    elif isinstance(a, dict):
        assert isinstance(b, dict) and a.keys() == b.keys(), path
        for k in a:
            _assert_close(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert isinstance(b, list) and len(a) == len(b), path
        for i, (x, y) in enumerate(zip(a, b)):
            _assert_close(x, y, f"{path}[{i}]")
    else:
        assert a == b, path


# ------------------------------------------------------------------ plan
def test_plan_equals_study2_except_run_ids_and_seeds() -> None:
    p3, p2 = study3.build_plan(), study2.build_plan()
    assert list(p3) == [f"s3-src-{i}" for i in range(1, 11)] == list(study3.PRIMARY_RUNS)
    assert [s.cfg.seed for s in p3.values()] == list(range(21, 31))
    assert not {s.cfg.seed for s in p3.values()} & {s.cfg.seed for s in p2.values()}
    for s3, s2 in zip(p3.values(), p2.values(), strict=True):
        a, b = asdict(s3.cfg), asdict(s2.cfg)
        assert {k for k in a if a[k] != b[k]} == {"run_id", "seed"}
        assert (s3.source, s3.pooled_sim_from) == (s2.source, s2.pooled_sim_from)
    # request settings, call caps and the gate's bound are Study 2's
    assert study3.POLICY_PARAMS == study2.POLICY_PARAMS
    assert study3.SEARCH_EXTRA == study2.SEARCH_EXTRA == {"enable_thinking": False}
    assert study3.SEARCH_MAX_TOKENS == study2.SEARCH_MAX_TOKENS == 8192
    assert study3.RUN_BUDGET == study2.RUN_BUDGET
    assert study3.RUN_BOUND_RMB == study2.RUN_BOUND_RMB == 2.10
    assert study3.CONTROLS == study2.CONTROLS == tuple(f"sql-src-{i}" for i in range(1, 8))
    assert {s.group for s in p3.values()} == set(study3.EST_RMB)


def test_cli_wiring_and_separate_ledger() -> None:
    assert plan_module("study3") is study3
    f2, c2 = plan_money("study2")
    f3, c3 = plan_money("study3")
    assert f3 == REPO / "results" / "study3" / "budget" / "spend_ledger.json" and f3 != f2
    assert (c2, c3) == (15.00, 15.00)
    assert plan_root("study3") == REPO / "runs" / "study3" != plan_root("study2")


def test_money_gate_admits_at_most_seven_bounds_at_once() -> None:
    names = list(study3.build_plan())
    e = {n: study3.RUN_BOUND_RMB for n in names}
    spend = {"cap_rmb": study3.SPEND_CAP_RMB, "spent_rmb": 0.0004, "in_flight_rmb": 0.0, "by": {}}
    assert money_gate(names, names, [], e, spend) == set(names[:7])
    # the last run starts only if about RMB 12.90 or less is spent or bound by running runs
    spend["spent_rmb"] = 12.89
    assert money_gate(names, names[9:], [], e, spend) == {names[9]}
    spend["spent_rmb"] = 12.91
    assert money_gate(names, names[9:], [], e, spend) == set()


def test_estimate_from_study2_ledger() -> None:
    est = study3.estimate()
    assert len(est["study2_rmb_per_run"]) == 10
    assert est["max_rmb_per_run"] < study3.RUN_BOUND_RMB
    assert 10 * est["mean_rmb_per_run"] == pytest.approx(est["ten_runs_at_mean"], abs=0.01)
    assert est["ten_runs_at_mean"] < study3.SPEND_CAP_RMB


# -------------------------------------------------------------- analysis
def _rows(names: list[str], vals: list[float | None]) -> list[dict[str, Any]]:
    from lamarck.experiments.study2_report import _row
    from lamarck.experiments.study3_report import _batch

    rows = []
    for name, v in zip(names, vals, strict=True):
        r: dict[str, Any] = {"run": name, "heldout_start": 0.8, "evolve_start": 0.8, "evolve_end": 0.9}
        r["accepted"] = 1
        if v is not None:
            r["heldout_end"] = 0.8 + v / 100
        rows.append(_row(r, 1.1, _batch))
    return rows


CONTROLS = [
    {"heldout_change": x, "evolve_change": 5.0, "accepted": 1} for x in (-2.5, -2.5, -2.5, 2.5, 0, 0, 5)
]


def test_analysis_uses_exactly_s3_src_1_to_10() -> None:
    from lamarck.experiments.study3_report import analysis

    s3 = list(study3.PRIMARY_RUNS)
    vals: list[float | None] = [0.0, 2.5, 5.0, None, 0.0, 2.5, 7.5, 0.0, -2.5, 2.5]
    rows = _rows(s3, vals)
    # rows of other studies' runs (and an unplanned s3 name) must be ignored
    noise = _rows(["s2-src-1", "s2-src-6", "sql-src-1", "s3-src-11"], [50.0, -50.0, 40.0, 30.0])
    a = analysis(list(reversed(noise + rows)), CONTROLS)
    assert a["runs_analysed"] == [n for n, v in zip(s3, vals, strict=True) if v is not None]
    assert a["runs_not_finished"] == ["s3-src-4"]  # not imputed, not replaced
    d = [v for v in vals if v is not None]
    assert a["n"] == 9 and a["primary"]["values"] == pytest.approx(d)
    vals_r = a["primary"]["values"]
    assert a["primary"]["one_sided_vs_0"] == one_sample_greater(vals_r)
    assert a["primary"]["one_sample_vs_0"] == one_sample_summary(vals_r)
    assert a["primary"]["two_sample_vs_controls"] == two_sample_summary(
        vals_r, a["primary"]["control_values"]
    )
    assert a["alpha_one_sided"] == 0.05
    assert all(r["batch"] == "study3" for r in rows)


def test_one_sided_tests_match_scipy() -> None:
    from scipy import stats as sps

    d = [0.0, 0.0, 5.0, 5.0, 2.5, 7.5, 0.0, 2.5, 0.0, 0.0]
    g = one_sample_greater(d)
    ref = sps.ttest_1samp(d, 0.0, alternative="greater")
    assert g["t"] == pytest.approx(float(ref.statistic)) and g["p_t_greater"] == pytest.approx(
        float(ref.pvalue)
    )
    nz = [x for x in d if x]
    w = sps.wilcoxon(nz, alternative="greater", method="exact")
    assert g["wilcoxon_greater"]["p_greater"] == pytest.approx(float(w.pvalue))
    assert one_sample_greater([-1.0, -2.0, -1.5])["p_t_greater"] > 0.5


def test_verdict_rule() -> None:
    from lamarck.experiments.study3_report import analysis, verdict

    assert verdict(0.0499) == "replicated"
    assert verdict(0.05) == verdict(0.2) == verdict(None) == "not replicated"
    s3 = list(study3.PRIMARY_RUNS)
    pos = analysis(_rows(s3, [2.5, 5.0, 2.5, 0.0, 5.0, 2.5, 7.5, 2.5, 0.0, 2.5]), CONTROLS)
    assert pos["primary"]["one_sided_vs_0"]["p_t_greater"] < 0.05 and pos["verdict"] == "replicated"
    neg = analysis(_rows(s3, [0.0, -2.5, 2.5, 0.0, 0.0, 2.5, -2.5, 0.0, 0.0, 2.5]), CONTROLS)
    assert neg["verdict"] == "not replicated"
    one = analysis(_rows(s3, [5.0] + [None] * 9), CONTROLS)
    assert one["primary"]["one_sided_vs_0"] is None and one["verdict"] == "not replicated"  # n < 2: no test


# ------------------------------------------- Study 2 output unchanged
def test_study2_report_unchanged_by_generalization() -> None:
    """The committed Study 2 RESULTS.md / summary.json are reproduced exactly from the committed rows."""
    from lamarck.experiments.study2_report import analyses, render_markdown

    d = REPO / "results" / "study2"
    summ = json.loads((d / "summary.json").read_text())
    assert render_markdown(summ) == (d / "RESULTS.md").read_text()
    both = json.loads(json.dumps(analyses(summ["runs"], summ["controls"])))
    # Floats are compared to 1e-9 relative: scipy's t/CI values differ in the last bits between macOS arm64
    # (where the committed summary was produced) and Linux x86 CI. Structure, keys and strings must match exactly.
    _assert_close(both["primary"], summ["analysis"])
    _assert_close(both["extended"], summ["analysis_extended"])
    # plan unchanged
    p2 = study2.build_plan()
    assert list(p2) == [f"s2-src-{i}" for i in range(1, 11)]
    assert [s.cfg.seed for s in p2.values()] == list(range(11, 21))
    assert {s.group for s in p2.values()} == {"s2-source"}
    assert [r["batch"] for r in summ["runs"]] == ["primary"] * 5 + ["extension"] * 5


def test_stop_reasons_of_unfinished_runs(tmp_path: Path) -> None:
    from lamarck.experiments.study3_report import stop_reasons

    for name, st in (("s3-src-1", "done"), ("s3-src-5", "infra_error")):
        d = tmp_path / "runs" / name
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"status": st, "stop_reason": "gateway 403: " + "x" * 500}))
    status = {"s3-src-1": "done", "s3-src-5": "infra_error", "s3-src-8": "not run"}
    got = stop_reasons(tmp_path, status)
    assert (
        list(got) == ["s3-src-5"]
        and got["s3-src-5"].startswith("gateway 403")
        and len(got["s3-src-5"]) == 240
    )


def test_interim_analysis_uses_exactly_the_five_runs_finished_at_the_outage() -> None:
    from lamarck.experiments.study3_report import analysis, interim

    assert study3.INTERIM_RUNS == ("s3-src-1", "s3-src-2", "s3-src-3", "s3-src-4", "s3-src-6")
    s3 = list(study3.PRIMARY_RUNS)
    vals: list[float | None] = [0.0, 2.5, 5.0, 2.5, 0.0, 7.5, 2.5, -2.5, 5.0, 0.0]
    rows = _rows(s3, vals)
    x = interim(rows, CONTROLS)
    assert x["runs_analysed"] == list(study3.INTERIM_RUNS) and x["n"] == 5
    assert x["primary"]["values"] == pytest.approx([0.0, 2.5, 5.0, 2.5, 7.5])
    assert "verdict" not in x and x["label"].startswith("interim")
    full = analysis(rows, CONTROLS)
    assert full["n"] == 10 and full["runs_analysed"] == s3  # the primary analysis is all ten
