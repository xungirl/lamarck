"""Study 3 report: the pre-registered fixed-n replication of Study 2 (PREREG.md, section "Study 3").

One analysis, of exactly the runs s3-src-1..10 that finished (study3.PRIMARY_RUNS; rows of any other run are
ignored): primary test one-sided one-sample t of the held-out change vs 0 (H1 mean > 0, alpha 0.05), with the
two-sided p, the 95% t interval and the exact signed-rank test (two- and one-sided) next to it. Verdict, by the
rule fixed in advance: one-sided p < 0.05 -> "replicated", otherwise "not replicated". Secondary (descriptive):
vs the seven flash controls (Welch t, exact Mann-Whitney, both two-sided), evolve change, accepted edits, money.

    uv run python -m lamarck.experiments.study3_report            # runs/study3 -> results/study3
    uv run python -m lamarck.experiments.study3_report --root R --out O --spend-file F

Run collection and the shared statistics are Study 2's (study2_report.collect / analyse); the controls are
read from results/summary.json (key `source`), which this module never writes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import study3
from .stats import one_sample_greater
from .study2_report import CONTROLS_FILE, _f, analyse, collect

REPO = Path(__file__).resolve().parents[2]
REPLICATED, NOT_REPLICATED = "replicated", "not replicated"
LABEL = "primary (pre-registered, fixed n): runs s3-src-1..10 that finished"
LIMITATIONS = (
    "The controls (sql-src-1..7) are not randomized: they ran earlier (2026-11-19), on another date and not "
    "interleaved with Study 3, so the two-sample comparison is not a randomized comparison (time, provider state "
    "and model versions may differ). Held-out scores move in steps of 2.5 points on 40 tasks. Evaluation noise: "
    "repeat evaluations of one harness at temperature 0 differ by about as much as the effect (results/summary.json "
    "`eval_variance`), and every run shares one cached measurement of the seed harness's held-out start. Host: all "
    "Study 3 runs ran on macOS arm64 (colima, gVisor). Power at Study 2's extended estimate is about 0.77 and lower "
    "if that estimate is inflated by its optional stopping, so 'not replicated' does not show the effect is 0. One "
    "model family, one benchmark, 3 rounds."
)


def _batch(name: str | None) -> str | None:
    return "study3" if name in study3.PRIMARY_RUNS else None


def verdict(p_one_sided: float | None) -> str:
    """The interpretation rule fixed in PREREG Study 3."""
    return REPLICATED if p_one_sided is not None and p_one_sided < study3.ALPHA else NOT_REPLICATED


def analysis(rows: list[dict[str, Any]], controls: list[dict[str, Any]]) -> dict[str, Any]:
    """The Study 3 analysis of exactly study3.PRIMARY_RUNS (in plan order), whatever else is in `rows`."""
    by_run = {r["run"]: r for r in rows}
    used = [by_run[n] for n in study3.PRIMARY_RUNS if n in by_run]
    a = analyse(used, controls)
    d = a["primary"]["values"]
    a["primary"]["one_sided_vs_0"] = one_sample_greater(d) if len(d) >= 2 else None
    p1 = a["primary"]["one_sided_vs_0"]["p_t_greater"] if a["primary"]["one_sided_vs_0"] else None
    return {
        "label": LABEL,
        "runs_analysed": [r["run"] for r in used if r["heldout_change"] is not None],
        "runs_not_finished": [
            n for n in study3.PRIMARY_RUNS if n not in by_run or by_run[n]["heldout_change"] is None
        ],
        **a,
        "alpha_one_sided": study3.ALPHA,
        "verdict": verdict(p1),
    }


def build(root: Path, out: Path, spend_file: Path, controls_file: Path = CONTROLS_FILE) -> dict[str, Any]:
    c = collect(study3, root, out, spend_file, controls_file, _batch)
    spend, by, plan = c["spend"], c["by"], c["plan"]
    agg = {
        "study": "Study 3: confirmatory fixed-n replication of Study 2 (PREREG.md)",
        "root": str(root),
        "status": c["status"],
        "runs": c["rows"],
        "controls": c["controls"],
        "controls_source": f"{controls_file.name} key `source`",
        "analysis": analysis(c["rows"], c["controls"]),
        "limitations": LIMITATIONS,
        "money": {
            "cap_rmb": spend.get("cap_rmb"),
            "spent_rmb": spend.get("spent_rmb"),
            "calls": spend.get("calls"),
            "failed": spend.get("failed"),
            "by_model": {k[6:]: v for k, v in sorted(by.items()) if k.startswith("model:")},
            "by_role": {k[5:]: v for k, v in sorted(by.items()) if k.startswith("role:")},
            "runs_rmb": sum(by.get(f"run:{n}", {}).get("rmb", 0.0) for n in plan),
            "outside_runs": {
                k[4:]: v["rmb"] for k, v in sorted(by.items()) if k.startswith("run:") and k[4:] not in plan
            },
            "run_bound_rmb": study3.RUN_BOUND_RMB,
            "prices_rmb_per_million": spend.get("prices_rmb_per_million"),
        },
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(agg, indent=1))
    (out / "RESULTS.md").write_text(render_markdown(agg))
    return agg


def render_markdown(agg: dict[str, Any]) -> str:
    a = agg["analysis"]
    p = a["primary"]
    L = [
        "# Study 3 results: confirmatory replication of Study 2 (qwen4-max proposer and selector)",
        "",
        "Generated by `lamarck.experiments.study3_report` from run files; see PREREG.md, section Study 3.",
        "",
        "| run | seed | held-out before → after | change | evolve before → after | accepted | RMB | status |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in agg["runs"]:
        L.append(
            f"| {r['run']} | {r['seed']} | {_f(r['heldout_start'], 1)} → {_f(r['heldout_end'], 1)} | "
            f"{_f(r['heldout_change'], 1)} | {_f(r['evolve_start'], 1)} → {_f(r['evolve_end'], 1)} | "
            f"{r['accepted'] if r['accepted'] is not None else 'n/a'} | {_f(r['rmb'], 3)} | "
            f"{r.get('status', 'done')} |"
        )
    L += [
        "",
        f"Controls ({agg['controls_source']}): held-out changes {p['control_values']}, mean {_f(p['control_mean'])}.",
        "",
        f"**Primary analysis (pre-registered, fixed n)** (n = {a['n']} of {len(study3.PRIMARY_RUNS)}: "
        f"{', '.join(a['runs_analysed']) or 'none'}; not finished: {', '.join(a['runs_not_finished']) or 'none'}): "
        f"held-out changes {p['values']}, mean {_f(p['mean'])} points.",
    ]
    o1, o, t = p["one_sided_vs_0"], p["one_sample_vs_0"], p["two_sample_vs_controls"]
    if o1 and o:
        L += [
            f"- primary test, one-sided t vs 0 (H1 mean > 0): t({o1['df']}) = {_f(o1['t'])}, "
            f"one-sided p = {_f(o1['p_t_greater'], 4)} (alpha {a['alpha_one_sided']})",
            f"- two-sided p = {_f(o['p_t_two_sided'], 4)}, 95% CI [{_f(o['ci95'][0])}, {_f(o['ci95'][1])}]; exact "
            f"signed-rank p two-sided = {_f(o['wilcoxon']['p_two_sided'], 4)}, one-sided = "
            f"{_f(o1['wilcoxon_greater']['p_greater'], 4)} ({o['wilcoxon']['n_nonzero']} nonzero changes)",
        ]
    else:
        L.append("- fewer than 2 finished runs: no test")
    L += [
        "",
        f"**Verdict (rule fixed in PREREG Study 3: one-sided p < 0.05 → replicated): {a['verdict']}.**",
        "",
        "Secondary (descriptive):",
    ]
    if t:
        ci = t["ci95_welch"]
        L.append(
            f"- vs the 7 flash controls: difference of means {_f(t['diff_of_means'])}, Welch t = {_f(t['welch_t'])} "
            f"(df {_f(t['welch_df'], 1)}), two-sided p = {_f(t['p_welch_two_sided'], 3)}"
            + (f", 95% CI [{_f(ci[0])}, {_f(ci[1])}]" if ci else "")
            + f"; exact Mann-Whitney two-sided p = {_f(t['mann_whitney']['p_two_sided'], 3)}"
        )
    s = a["secondary"]
    L.append(
        f"- evolve change {s['evolve_change']}, mean {_f(s['evolve_change_mean'])} (controls "
        f"{_f(s['control_evolve_change_mean'])}); accepted edits {s['accepted']} (controls {s['control_accepted']})"
    )
    m = agg["money"]
    L += [
        "",
        f"Money: RMB {_f(m['spent_rmb'], 3)} of the RMB {_f(m['cap_rmb'])} Study 3 cap, {m['calls']} calls "
        f"({m['failed']} failed); runs RMB {_f(m['runs_rmb'], 3)}; outside runs (API checks): "
        f"{m['outside_runs'] or 'none'}.",
        "",
        f"Limitations: {agg['limitations']}",
        "",
    ]
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=study3.DEFAULT_ROOT)
    ap.add_argument("--out", type=Path, default=REPO / "results" / "study3")
    ap.add_argument("--spend-file", type=Path, default=study3.SPEND_FILE)
    ap.add_argument("--controls", type=Path, default=CONTROLS_FILE)
    a = ap.parse_args()
    agg = build(a.root.resolve(), a.out.resolve(), a.spend_file.resolve(), a.controls.resolve())
    print(render_markdown(agg))


if __name__ == "__main__":
    main()
