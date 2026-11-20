"""Study 2 report: primary outcome (Spider held-out after - before per run) against 0 and against the seven
flash-proposer controls, secondary outcomes (evolve gain, accepted edits, money), from run files only.

Two analyses (PREREG.md, Study 2 deviation 1): the **primary** analysis uses exactly the pre-registered runs
s2-src-1..5 (study2.PRIMARY_RUNS) and is never replaced; the **extended** analysis (secondary, added after the
n = 5 result was seen: optional stopping) uses every finished run of s2-src-1..10, against the same controls.
summary.json: `analysis` is the primary analysis, `analysis_extended` the extended one.

    uv run python -m lamarck.experiments.study2_report            # runs/study2 -> results/study2
    uv run python -m lamarck.experiments.study2_report --root R --out O --spend-file F

The controls are read from the first study's exported summary (results/summary.json, key `source`), which
this module never writes.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from . import study2
from .plan import build_plan as build_rev3_plan
from .report import export_run, load_run
from .stats import one_sample_summary, two_sample_summary

REPO = Path(__file__).resolve().parents[2]
CONTROLS_FILE = REPO / "results" / "summary.json"
LIMITATIONS = (
    "The controls (sql-src-1..7) ran earlier (2026-11-19) under the same protocol, splits and cached "
    "seed-harness evaluations; Study 2's runs were neither randomized against nor interleaved with them, so the "
    "two-sample comparison is not a randomized comparison (time, provider state and model versions may differ). "
    "n is small (5, or at most 10 in the extended analysis, vs 7), scores move in steps of 2.5 points on 40 "
    "held-out tasks, and the repeat-evaluation noise of one harness at temperature 0 is of the same order "
    "(results/summary.json `eval_variance`). The extended analysis adds runs chosen after the primary result was "
    "seen (optional stopping): its p-values are not confirmatory. Runs 1-5 ran on an EC2 Linux arm64 host and "
    "runs 6-10 on macOS arm64 (colima), hours apart: host and batch are confounded (PREREG Study 2 deviation 1)."
)
PRIMARY_LABEL = "primary (pre-registered): runs s2-src-1..5"
EXTENDED_LABEL = (
    "extended (secondary, PREREG Study 2 deviation 1): runs s2-src-1..10 that finished; runs 6-10 were added "
    "after the primary result was seen (optional stopping)"
)


def _pts(x: float | None) -> float | None:
    return None if x is None else round(x * 100, 4)


def _study2_batch(name: str | None) -> str | None:
    return (
        "primary" if name in study2.PRIMARY_RUNS else ("extension" if name in study2.EXTENSION_RUNS else None)
    )


def _row(
    r: dict[str, Any], rmb: float | None, batch_of: Callable[[str | None], str | None] = _study2_batch
) -> dict[str, Any]:
    hs, he, es, ee = r.get("heldout_start"), r.get("heldout_end"), r.get("evolve_start"), r.get("evolve_end")
    name = r.get("run_id") or r.get("run")
    return {
        "run": name,
        "batch": batch_of(name),
        "seed": r.get("seed"),
        "search_model": r.get("search_model"),
        "heldout_start": _pts(hs),
        "heldout_end": _pts(he),
        "heldout_change": None if hs is None or he is None else round((he - hs) * 100, 4),
        "evolve_start": _pts(es),
        "evolve_end": _pts(ee),
        "evolve_change": None if es is None or ee is None else round((ee - es) * 100, 4),
        "accepted": r.get("accepted"),
        "rmb": rmb,
    }


def analyse(study: list[dict[str, Any]], controls: list[dict[str, Any]]) -> dict[str, Any]:
    """study / controls: rows of _row(). Rows without a held-out change (not finished) are not analysed."""
    d = [r["heldout_change"] for r in study if r["heldout_change"] is not None]
    c = [r["heldout_change"] for r in controls if r["heldout_change"] is not None]
    ev = [r["evolve_change"] for r in study if r["evolve_change"] is not None]
    ev_c = [r["evolve_change"] for r in controls if r["evolve_change"] is not None]
    out: dict[str, Any] = {
        "n": len(d),
        "n_controls": len(c),
        "primary": {
            "values": d,
            "control_values": c,
            "mean": statistics.fmean(d) if d else None,
            "control_mean": statistics.fmean(c) if c else None,
            "one_sample_vs_0": one_sample_summary(d) if len(d) >= 2 else None,
            "two_sample_vs_controls": two_sample_summary(d, c) if len(d) >= 2 and len(c) >= 2 else None,
        },
        "secondary": {
            "evolve_change": ev,
            "evolve_change_mean": statistics.fmean(ev) if ev else None,
            "control_evolve_change": ev_c,
            "control_evolve_change_mean": statistics.fmean(ev_c) if ev_c else None,
            "accepted": [r["accepted"] for r in study],
            "control_accepted": [r["accepted"] for r in controls],
            "rmb_per_run": [r["rmb"] for r in study],
        },
    }
    return out


def analyses(rows: list[dict[str, Any]], controls: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The primary analysis (exactly study2.PRIMARY_RUNS, whatever else is in `rows`) and the extended one
    (every row of the plan), each with the runs it is computed from and a label."""
    by_run = {r["run"]: r for r in rows}  # rows in plan order, whatever the input order
    prim = [by_run[n] for n in study2.PRIMARY_RUNS if n in by_run]
    ext = [by_run[n] for n in study2.PRIMARY_RUNS + study2.EXTENSION_RUNS if n in by_run]
    extension_only = [
        by_run[n]["heldout_change"]
        for n in study2.EXTENSION_RUNS
        if n in by_run and by_run[n]["heldout_change"] is not None
    ]
    return {
        "primary": {
            "label": PRIMARY_LABEL,
            "runs_analysed": [r["run"] for r in prim if r["heldout_change"] is not None],
            **analyse(prim, controls),
        },
        "extended": {
            "label": EXTENDED_LABEL,
            "runs_analysed": [r["run"] for r in ext if r["heldout_change"] is not None],
            **analyse(ext, controls),
            # descriptive only: the extension batch on its own (no test)
            "extension_runs_only": {
                "values": extension_only,
                "mean": statistics.fmean(extension_only) if extension_only else None,
            },
        },
    }


def collect(
    pm: ModuleType,
    root: Path,
    out: Path,
    spend_file: Path,
    controls_file: Path = CONTROLS_FILE,
    batch_of: Callable[[str | None], str | None] = _study2_batch,
) -> dict[str, Any]:
    """Run rows (in plan order, finished runs exported to `out`), statuses, control rows and the spend ledger
    of a source-run study plan `pm` (study2 or study3): the part of the report the studies share."""
    spend = json.loads(spend_file.read_text()) if spend_file.exists() else {}
    by = spend.get("by", {})
    plan = pm.build_plan()
    rows: list[dict[str, Any]] = []
    status: dict[str, str] = {}
    for name in plan:
        st_path = root / "runs" / name / "state.json"
        status[name] = json.loads(st_path.read_text()).get("status", "?") if st_path.exists() else "not run"
        rmb = by.get(f"run:{name}", {}).get("rmb")
        r = load_run(root, name) if status[name] == "done" else None
        if r is not None:
            export_run(root, out, name)
            rows.append(
                _row(
                    {**r, "seed": plan[name].cfg.seed, "search_model": plan[name].cfg.search_model},
                    rmb,
                    batch_of,
                )
            )
        else:
            rows.append(
                {**_row({"run": name, "seed": plan[name].cfg.seed}, rmb, batch_of), "status": status[name]}
            )
    ctrl_src = json.loads(controls_file.read_text())["source"]
    ctrl_plan = build_rev3_plan()
    controls = [
        _row(
            {**r, "seed": ctrl_plan[r["run"]].cfg.seed, "search_model": ctrl_plan[r["run"]].cfg.search_model},
            r.get("rmb"),
            batch_of,
        )
        for r in ctrl_src
        if r["run"] in pm.CONTROLS
    ]
    return {"spend": spend, "by": by, "plan": plan, "rows": rows, "status": status, "controls": controls}


def build(root: Path, out: Path, spend_file: Path, controls_file: Path = CONTROLS_FILE) -> dict[str, Any]:
    c = collect(study2, root, out, spend_file, controls_file)
    spend, by, plan, rows, status, controls = (
        c[k] for k in ("spend", "by", "plan", "rows", "status", "controls")
    )
    both = analyses(rows, controls)
    agg = {
        "study": "Study 2: strong search model (PREREG.md)",
        "root": str(root),
        "status": status,
        "runs": rows,
        "controls": controls,
        "controls_source": f"{controls_file.name} key `source`",
        "analysis": both["primary"],
        "analysis_extended": both["extended"],
        "limitations": LIMITATIONS,
        "money": {
            "cap_rmb": spend.get("cap_rmb"),
            "spent_rmb": spend.get("spent_rmb"),
            "calls": spend.get("calls"),
            "failed": spend.get("failed"),
            "by_model": {k[6:]: v for k, v in sorted(by.items()) if k.startswith("model:")},
            "by_role": {k[5:]: v for k, v in sorted(by.items()) if k.startswith("role:")},
            "outside_runs": {
                k[4:]: v["rmb"] for k, v in sorted(by.items()) if k.startswith("run:") and k[4:] not in plan
            },
            "by_batch_rmb": {
                b: sum(by.get(f"run:{n}", {}).get("rmb", 0.0) for n in runs)
                for b, runs in (("primary", study2.PRIMARY_RUNS), ("extension", study2.EXTENSION_RUNS))
            },
            "run_bound_rmb": study2.RUN_BOUND_RMB,
            "prices_rmb_per_million": spend.get("prices_rmb_per_million"),
        },
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(agg, indent=1))
    (out / "RESULTS.md").write_text(render_markdown(agg))
    return agg


def _f(x: Any, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _render_analysis(title: str, a: dict[str, Any]) -> list[str]:
    p = a["primary"]
    L = [
        f"**{title}** (n = {a['n']}: {', '.join(a['runs_analysed']) or 'none'}): mean held-out change {_f(p['mean'])} points."
    ]
    o, t = p["one_sample_vs_0"], p["two_sample_vs_controls"]
    if o:
        L.append(
            f"- vs 0: t({o['df']}) = {_f(o['t'])}, p = {_f(o['p_t_two_sided'], 3)}, 95% CI "
            f"[{_f(o['ci95'][0])}, {_f(o['ci95'][1])}]; exact signed-rank p = {_f(o['wilcoxon']['p_two_sided'], 3)}"
        )
    if t:
        ci = t["ci95_welch"]
        L.append(
            f"- vs controls: difference of means {_f(t['diff_of_means'])}, Welch t = {_f(t['welch_t'])} "
            f"(df {_f(t['welch_df'], 1)}), p = {_f(t['p_welch_two_sided'], 3)}"
            + (f", 95% CI [{_f(ci[0])}, {_f(ci[1])}]" if ci else "")
            + f"; exact Mann-Whitney p = {_f(t['mann_whitney']['p_two_sided'], 3)}"
        )
    if not o:
        L.append("- fewer than 2 finished runs: no test")
    s = a["secondary"]
    L.append(
        f"- secondary: evolve change mean {_f(s['evolve_change_mean'])} (controls {_f(s['control_evolve_change_mean'])}); "
        f"accepted edits {s['accepted']} (controls {s['control_accepted']})"
    )
    return L


def render_markdown(agg: dict[str, Any]) -> str:
    a = agg["analysis"]
    p = a["primary"]
    L = [
        "# Study 2 results: strong search model (qwen4-max proposer and selector)",
        "",
        "Generated by `lamarck.experiments.study2_report` from run files; see PREREG.md, section Study 2.",
        "",
        "| run | batch | seed | held-out before → after | change | evolve before → after | accepted | RMB | status |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in agg["runs"]:
        L.append(
            f"| {r['run']} | {r['batch']} | {r['seed']} | {_f(r['heldout_start'], 1)} → {_f(r['heldout_end'], 1)} | "
            f"{_f(r['heldout_change'], 1)} | {_f(r['evolve_start'], 1)} → {_f(r['evolve_end'], 1)} | "
            f"{r['accepted'] if r['accepted'] is not None else 'n/a'} | {_f(r['rmb'], 3)} | "
            f"{r.get('status', 'done')} |"
        )
    L += [
        "",
        f"Controls ({agg['controls_source']}): held-out changes {p['control_values']}, mean {_f(p['control_mean'])}.",
        "",
    ]
    L += _render_analysis("Primary analysis (pre-registered; never replaced)", a)
    L.append("")
    if "analysis_extended" in agg:
        x = agg["analysis_extended"]
        L += _render_analysis(
            "Extended analysis (secondary; PREREG Study 2 deviation 1: runs 6-10 added after the primary "
            "result was seen, optional stopping, not confirmatory; runs 1-5 on EC2 Linux, 6-10 on macOS/colima)",
            x,
        )
        e = x["extension_runs_only"]
        L.append(
            f"- descriptive, runs 6-10 alone: held-out changes {e['values']}, mean {_f(e['mean'])} (no test)"
        )
        L.append("")
    m = agg["money"]
    L += [
        f"Money: RMB {_f(m['spent_rmb'], 3)} of the RMB {_f(m['cap_rmb'])} Study 2 cap, {m['calls']} calls "
        f"({m['failed']} failed); runs 1-5 RMB {_f(m['by_batch_rmb']['primary'], 3)}, runs 6-10 RMB "
        f"{_f(m['by_batch_rmb']['extension'], 3)}; outside runs (API checks): {m['outside_runs'] or 'none'}.",
        "",
        f"Limitations: {agg['limitations']}",
        "",
    ]
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=study2.DEFAULT_ROOT)
    ap.add_argument("--out", type=Path, default=REPO / "results" / "study2")
    ap.add_argument("--spend-file", type=Path, default=study2.SPEND_FILE)
    ap.add_argument("--controls", type=Path, default=CONTROLS_FILE)
    a = ap.parse_args()
    agg = build(a.root.resolve(), a.out.resolve(), a.spend_file.resolve(), a.controls.resolve())
    print(render_markdown(agg))


if __name__ == "__main__":
    main()
