"""Collect finished runs into results/ (raw files + aggregate tables) and write results/RESULTS.md.

Everything here is computed from files the runs wrote; no number is typed by hand.
"""

from __future__ import annotations

import json
import shutil
import statistics
from pathlib import Path
from typing import Any

from ..engine.gitops import HarnessRepo
from ..sim.memory import SIM
from ..types import EvalResult
from .plan import N_SQL_SOURCES, build_plan
from .stats import paired_summary, wilson_ci

PRICES_NOTE = "see results/cost.json"


def pts(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.2f}"


def load_run(root: Path, name: str) -> dict[str, Any] | None:
    d = root / "runs" / name
    if not (d / "summary.json").exists():
        return None
    s: dict[str, Any] = json.loads((d / "summary.json").read_text())
    s["state"] = json.loads((d / "state.json").read_text())
    hist = (
        [json.loads(x) for x in (d / "history.jsonl").read_text().splitlines()]
        if (d / "history.jsonl").exists()
        else []
    )
    s["history"] = hist
    sim = SIM.load(d / "sim.json")
    own = [
        a for p in sim.problems for a in p.attempts if a.domain == f"{s['evolve_split']}/{s['policy_model']}"
    ]
    s["own_attempts"] = len(own)
    s["own_predicted"] = sum(len(a.predicted) for a in own)
    s["own_fixed"] = sum(len(a.fixed) for a in own)
    s["cited_existing"] = sum(1 for r in hist if r.get("cite"))
    return s


def export_run(root: Path, out: Path, name: str) -> None:
    src = root / "runs" / name
    dst = out / "runs" / name
    dst.mkdir(parents=True, exist_ok=True)
    for f in (
        "summary.json",
        "state.json",
        "config.json",
        "history.jsonl",
        "sim.json",
        "heldout.jsonl",
        "calibration.json",
        "events.log",
    ):
        if (src / f).exists():
            shutil.copy2(src / f, dst / f)
    for done in sorted(src.glob("rounds/r*/done.json")):
        rd = dst / "rounds" / done.parent.name
        rd.mkdir(parents=True, exist_ok=True)
        for jf in done.parent.glob("*.json"):
            shutil.copy2(jf, rd / jf.name)
        for m in done.parent.glob("s*/manifest.json"):
            (rd / m.parent.name).mkdir(exist_ok=True)
            shutil.copy2(m, rd / m.parent.name / "manifest.json")
    repo = HarnessRepo(src / "repo")
    (dst / "lineage.json").write_text(json.dumps(repo.lineage(), indent=1))
    final = repo.read_files("evolve")
    (dst / "final_harness").mkdir(exist_ok=True)
    for rel, text in final.items():
        p = dst / "final_harness" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def ledger_by_run(root: Path) -> dict[str, Any]:
    p = root / "gateway-state" / "ledger.json"
    return json.loads(p.read_text()) if p.exists() else {}


def run_cost(ledger: dict[str, Any], name: str) -> dict[str, int]:
    r = ledger.get("runs", {}).get(name, {})
    pol = r.get("policy", {})
    search = [r.get(k, {}) for k in ("proposer", "selector", "critic")]
    return {
        "policy_calls": int(pol.get("calls", 0)),
        "policy_tokens": int(pol.get("prompt_tokens", 0) + pol.get("completion_tokens", 0)),
        "search_calls": sum(int(x.get("calls", 0)) for x in search),
        "search_tokens": sum(int(x.get("prompt_tokens", 0) + x.get("completion_tokens", 0)) for x in search),
    }


def build(root: Path, out: Path) -> dict[str, Any]:
    plan = build_plan()
    ledger = ledger_by_run(root)
    runs = {n: s for n in plan if (s := load_run(root, n)) is not None}
    for n in runs:
        export_run(root, out, n)
        runs[n]["cost"] = run_cost(ledger, n)
    agg: dict[str, Any] = {
        "runs": {n: {k: v for k, v in s.items() if k != "history"} for n, s in runs.items()}
    }

    # ---- source RSI
    agg["source"] = [
        {"run": n, "domain": s["domain"], "evolve_start": s["evolve_start"], "evolve_end": s["evolve_end"],
         "heldout_start": s["heldout_start"], "heldout_end": s["heldout_end"], "accepted": s["accepted"],
         "sim_entries": s["sim_entries"], **s["cost"]}
        for n, s in runs.items() if plan[n].group in ("sql-source", "so-source")
    ]  # fmt: skip

    # ---- paired study
    pairs = []
    for i in range(1, N_SQL_SOURCES + 1):
        f, t = runs.get(f"sql-full-{i}"), runs.get(f"sql-transfer-{i}")
        if f and t and f["state"]["status"] == "done" and t["state"]["status"] == "done":
            pairs.append({"source": i, "start": f["heldout_start"], "no_sim": t["heldout_end"], "sim": f["heldout_end"],
                          "delta": f["heldout_end"] - t["heldout_end"],
                          "evolve_no_sim": t["evolve_end"], "evolve_sim": f["evolve_end"]})  # fmt: skip
    agg["paired"] = {"pairs": pairs}
    if len(pairs) >= 2:
        st = paired_summary([p["sim"] * 100 for p in pairs], [p["no_sim"] * 100 for p in pairs])
        agg["paired"]["stats"] = st
        evo = paired_summary(
            [p["evolve_sim"] * 100 for p in pairs], [p["evolve_no_sim"] * 100 for p in pairs]
        )
        agg["paired"]["evolve_stats"] = evo

    # ---- four conditions
    def cond_table(prefix: str, names: dict[str, str]) -> list[dict[str, Any]]:
        rows = []
        for label, n in names.items():
            s = runs.get(n)
            if s is None:
                continue
            rows.append({"condition": label, "run": n, "before": s["heldout_start"], "after": s["heldout_end"],
                         "curve": [(p["after_round"], p["score"]) for p in s["heldout_curve"]],
                         "evolve_before": s["evolve_start"], "evolve_after": s["evolve_end"],
                         "accepted": s["accepted"], **s["cost"]})  # fmt: skip
        return rows

    agg["conditions"] = {
        "sql": cond_table("sql", {"Cold Start": "sql-cold", "Harness Transfer": "sql-transfer-1",
                                  "Problem-Only Memory": "sql-problems-1", "Full SIM": "sql-full-1"}),
        "supportops": cond_table("so", {"Cold Start": "so-cold", "Harness Transfer": "so-transfer",
                                        "Problem-Only Memory": "so-problems", "Full SIM": "so-full"}),
        "crossmodel": cond_table("xm", {"Cold Start": "xm-cold", "Harness Transfer": "xm-transfer",
                                        "Problem-Only Memory": "xm-problems", "Full SIM": "xm-full"}),
    }  # fmt: skip
    agg["retrieval"] = cond_table(
        "ret", {f"{arm} seed {s}": f"ret-{arm}-{s}" for s in (1, 2) for arm in ("full", "top")}
    )

    # ---- evaluation variance (cached repeated evaluations of the same harness)
    var = []
    for r0 in sorted((root / "cache" / "evals").glob("*/*/*/*__r0.json")):
        reps = sorted(r0.parent.glob(r0.name.replace("__r0.json", "__r*.json")))
        if len(reps) >= 2:
            scores = [EvalResult.load(rp).score * 100 for rp in reps]
            var.append({"domain": r0.parts[-4], "split": r0.parts[-3], "model": r0.parts[-2], "tree": r0.name.split("__")[0],
                        "scores": scores, "mean": statistics.fmean(scores), "sd": statistics.stdev(scores)})  # fmt: skip
    agg["eval_variance"] = var
    agg["ledger"] = {"pools": ledger.get("pools", {}), "models": ledger.get("models", {})}
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(agg, indent=1))
    (out / "RESULTS.md").write_text(render_markdown(agg))
    return agg


def render_markdown(agg: dict[str, Any]) -> str:
    L = ["# Results (generated by `lamarck report`; do not edit by hand)", ""]
    L += ["All scores are 0-100 on the held-out split unless marked evolve. One rollout per task, policy at",
          "temperature 0. Raw per-run files are under `results/runs/<run>/`.", ""]  # fmt: skip
    if agg["source"]:
        L += ["## Source RSI", "", "| run | evolve before → after | held-out before → after | accepted edits | SIM entries | policy calls | search calls |",
              "|---|---|---|---|---|---|---|"]  # fmt: skip
        for r in agg["source"]:
            L.append(f"| {r['run']} | {pts(r['evolve_start'])} → {pts(r['evolve_end'])} | {pts(r['heldout_start'])} → "
                     f"{pts(r['heldout_end'])} | {r['accepted']} | {r['sim_entries']} | {r['policy_calls']} | {r['search_calls']} |")  # fmt: skip
        L.append("")
    p = agg["paired"]
    if p["pairs"]:
        L += ["## Paired study: Spider → BIRD, Full SIM vs Harness Transfer (RQ1)", "",
              "| source run | start (H_S on BIRD held-out) | No SIM | SIM | Δ (SIM − No SIM) |", "|---|---|---|---|---|"]  # fmt: skip
        for r in p["pairs"]:
            L.append(
                f"| {r['source']} | {pts(r['start'])} | {pts(r['no_sim'])} | {pts(r['sim'])} | {r['delta'] * 100:+.2f} |"
            )
        if "stats" in p:
            s = p["stats"]
            L += ["", f"Mean Δ = {s['mean_diff']:+.2f} points (95% CI [{s['ci95'][0]:+.2f}, {s['ci95'][1]:+.2f}]), "
                  f"paired t({s['df']}) = {s['t']:.2f}, p = {s['p_t_two_sided']:.3f}; exact signed-rank p = "
                  f"{s['wilcoxon']['p_two_sided']:.3f}; d_z = {s['d_z']:.2f}; SIM wins {s['wins']}, loses {s['losses']}, ties {s['ties']}."]  # fmt: skip
            e = p["evolve_stats"]
            L += [
                f"On the evolve split (the optimization target) the mean Δ is {e['mean_diff']:+.2f} (p = {e['p_t_two_sided']:.3f})."
            ]
        L.append("")
    for key, title in (("sql", "Four conditions: Spider → BIRD (qwen3.8-flash)"),
                       ("supportops", "Four conditions: support-ops standard → shifted policy (qwen3.8-flash)"),
                       ("crossmodel", "Four conditions: cross-model on BIRD (qwen3.8-flash → deepseek-v4.1-flash)")):  # fmt: skip
        rows = agg["conditions"][key]
        if not rows:
            continue
        L += [f"## {title}", "", "| condition | before | after | gain | evolve before → after | accepted | policy calls | search calls |",
              "|---|---|---|---|---|---|---|---|"]  # fmt: skip
        for r in rows:
            gain = (
                (r["after"] - r["before"]) * 100
                if r["after"] is not None and r["before"] is not None
                else float("nan")
            )
            L.append(f"| {r['condition']} | {pts(r['before'])} | {pts(r['after'])} | {gain:+.2f} | {pts(r['evolve_before'])} → "
                     f"{pts(r['evolve_after'])} | {r['accepted']} | {r['policy_calls']} | {r['search_calls']} |")  # fmt: skip
        L.append("")
    if agg["retrieval"]:
        L += ["## Retrieval ablation (pooled SIM of all Spider source runs, continued on BIRD)", "",
              "| arm | before | after | evolve after | search tokens |", "|---|---|---|---|---|"]  # fmt: skip
        for r in agg["retrieval"]:
            L.append(
                f"| {r['condition']} | {pts(r['before'])} | {pts(r['after'])} | {pts(r['evolve_after'])} | {r['search_tokens']} |"
            )
        L.append("")
    if agg["eval_variance"]:
        L += ["## Repeat-evaluation variance (same harness, same split, different seeds)", "",
              "| domain | split | model | harness tree | scores | sd |", "|---|---|---|---|---|---|"]  # fmt: skip
        for v in agg["eval_variance"]:
            L.append(
                f"| {v['domain']} | {v['split']} | {v['model']} | {v['tree']} | {', '.join(f'{x:.2f}' for x in v['scores'])} | {v['sd']:.2f} |"
            )
        L.append("")
    pools = agg["ledger"].get("pools", {})
    if pools:
        L += [
            "## Budget used (gateway ledger)",
            "",
            "| pool | calls | failed | prompt tokens | completion tokens |",
            "|---|---|---|---|---|",
        ]
        for k, v in pools.items():
            L.append(
                f"| {k} | {v['calls']} | {v['failed']} | {v['prompt_tokens']} | {v['completion_tokens']} |"
            )
        L += ["", "| role \\| model | calls | prompt tokens | completion tokens |", "|---|---|---|---|"]
        for k, v in sorted(agg["ledger"]["models"].items()):
            L.append(f"| {k} | {v['calls']} | {v['prompt_tokens']} | {v['completion_tokens']} |")
        L.append("")
    _ = wilson_ci
    return "\n".join(L) + "\n"
