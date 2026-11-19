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
from .plan import N_RET_SEEDS, N_SQL_SOURCES, UNFUNDED_GROUPS, build_plan
from .stats import paired_summary, wilson_ci

PRICES_NOTE = "see results/cost.json"
SPEND_FILE = Path(__file__).resolve().parents[2] / "results" / "budget" / "spend_ledger.json"


def rmb(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


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


def run_cost(ledger: dict[str, Any], name: str, spend: dict[str, Any] | None = None) -> dict[str, Any]:
    r = ledger.get("runs", {}).get(name, {})
    pol = r.get("policy", {})
    search = [r.get(k, {}) for k in ("proposer", "selector", "critic")]
    return {
        "policy_calls": int(pol.get("calls", 0)),
        "policy_tokens": int(pol.get("prompt_tokens", 0) + pol.get("completion_tokens", 0)),
        "search_calls": sum(int(x.get("calls", 0)) for x in search),
        "search_tokens": sum(int(x.get("prompt_tokens", 0) + x.get("completion_tokens", 0)) for x in search),
        "rmb": None if spend is None else float(spend.get("by", {}).get(f"run:{name}", {}).get("rmb", 0.0)),
    }


def spend_summary(spend: dict[str, Any]) -> dict[str, Any]:
    by = spend.get("by", {})

    def group(prefix: str) -> dict[str, Any]:
        return {k[len(prefix) :]: v for k, v in sorted(by.items()) if k.startswith(prefix)}

    return {
        "cap_rmb": spend.get("cap_rmb"),
        "spent_rmb": spend.get("spent_rmb"),
        "calls": spend.get("calls"),
        "failed": spend.get("failed"),
        "prices_rmb_per_million": spend.get("prices_rmb_per_million"),
        "by_role": group("role:"),
        "by_model": group("model:"),
        "by_run": group("run:"),
    }


def build(root: Path, out: Path, spend_file: Path = SPEND_FILE) -> dict[str, Any]:
    plan = build_plan()
    ledger = ledger_by_run(root)
    spend = json.loads(spend_file.read_text()) if spend_file.exists() else None
    runs = {n: s for n in plan if (s := load_run(root, n)) is not None}
    if (out / "runs").exists():  # drop exports of runs that are no longer in this root
        for d in (out / "runs").iterdir():
            if d.name not in runs:
                shutil.rmtree(d)
    for n in runs:
        export_run(root, out, n)
        runs[n]["cost"] = run_cost(ledger, n, spend)
    agg: dict[str, Any] = {
        "runs": {n: {k: v for k, v in s.items() if k != "history"} for n, s in runs.items()}
    }

    # ---- source RSI
    agg["source"] = [
        {"run": n, "domain": s["domain"], "evolve_start": s["evolve_start"], "evolve_end": s["evolve_end"],
         "heldout_start": s["heldout_start"], "heldout_end": s["heldout_end"], "accepted": s["accepted"],
         "evolve_split": s["evolve_split"], "heldout_split": s["heldout_split"], "status": s["state"]["status"],
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
            rows.append({"condition": label, "run": n, "status": s["state"]["status"],
                         "before": s["heldout_start"], "after": s["heldout_end"],
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
        "ret",
        {f"{arm} seed {s}": f"ret-{arm}-{s}" for s in range(1, N_RET_SEEDS + 1) for arm in ("full", "top")},
    )
    agg["not_run"] = [
        {"run": n, "group": spec.group, "state": runs[n]["state"]["status"] if n in runs else "not started"}
        for n, spec in plan.items()
        if spec.group not in UNFUNDED_GROUPS and (n not in runs or runs[n]["state"]["status"] != "done")
    ]
    agg["unfunded"] = [n for n, spec in plan.items() if spec.group in UNFUNDED_GROUPS]

    # ---- evaluation variance (cached repeated evaluations of the same harness)
    var = []
    for r0 in sorted((root / "cache" / "evals").glob("*/*/*/*__r0.json")):
        reps = sorted(r0.parent.glob(r0.name.replace("__r0.json", "__r*.json")))
        if len(reps) >= 2:
            loaded = [EvalResult.load(rp) for rp in reps]
            scores = [e.score * 100 for e in loaded]
            var.append({"domain": r0.parts[-4], "split": r0.parts[-3], "model": r0.parts[-2], "tree": r0.name.split("__")[0],
                        "scores": scores, "mean": statistics.fmean(scores), "sd": statistics.stdev(scores),
                        "projected_from": loaded[0].projected_from})  # fmt: skip
    agg["eval_variance"] = var
    agg["ledger"] = {"pools": ledger.get("pools", {}), "models": ledger.get("models", {})}
    agg["spend"] = spend_summary(spend) if spend is not None else None
    pre = out / "pre_revision" / "summary.json"
    agg["pre_revision"] = json.loads(pre.read_text()) if pre.exists() else None
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(agg, indent=1))
    (out / "RESULTS.md").write_text(render_markdown(agg))
    return agg


def render_markdown(agg: dict[str, Any]) -> str:
    L = ["# Results (generated by `make report`; do not edit by hand)", ""]
    L += ["Budget-revised plan (PREREG.md, deviation 3): `qwen3.8-flash` for every role, N = 3 rounds, k = 2",
          "candidates, sub-splits of 15-40 tasks. All scores are 0-100 on the held-out split unless marked evolve;",
          "one rollout per task, policy at temperature 0. Raw per-run files are under `results/runs/<run>/`;",
          "RMB comes from the gateway's spend ledger (`results/budget/spend_ledger.json`).", ""]  # fmt: skip
    if agg["source"]:
        L += ["## Source RSI", "",
              "| run | evolve split | evolve before → after | held-out before → after | accepted edits | SIM entries | policy calls | search calls | RMB |",
              "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
        for r in agg["source"]:
            L.append(f"| {r['run']} | {r['evolve_split']} | {pts(r['evolve_start'])} → {pts(r['evolve_end'])} | {pts(r['heldout_start'])} → "
                     f"{pts(r['heldout_end'])} | {r['accepted']} | {r['sim_entries']} | {r['policy_calls']} | {r['search_calls']} | {rmb(r.get('rmb'))} |")  # fmt: skip
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
                f"On the evolve split (the optimization target) the mean Δ is {e['mean_diff']:+.2f} (p = {e['p_t_two_sided']:.3f}).",
                f"With n = {s['n']} pairs these statistics are descriptive: the smallest attainable exact signed-rank p "
                f"is {2 / 2 ** s['n']:.3f}, and one held-out task is {100 / 40:.1f} points on the 40-task split.",
            ]
        L.append("")
    for key, title in (("sql", "Four conditions: Spider → BIRD (qwen3.8-flash)"),
                       ("supportops", "Four conditions: support-ops standard → shifted policy (qwen3.8-flash)"),
                       ("crossmodel", "Four conditions: cross-model on BIRD (qwen3.8-flash → deepseek-v4.1-flash)")):  # fmt: skip
        rows = agg["conditions"][key]
        if not rows:
            continue
        L += [f"## {title}", "", "| condition | run | before | held-out after each round | after | gain | evolve before → after | accepted | RMB |",
              "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
        for r in rows:
            gain = (
                (r["after"] - r["before"]) * 100
                if r["after"] is not None and r["before"] is not None
                else float("nan")
            )
            curve = ", ".join(f"r{a}: {sc * 100:.1f}" for a, sc in r["curve"] if a >= 0) or "-"
            status = "" if r.get("status") == "done" else f" ({r.get('status')})"
            L.append(f"| {r['condition']}{status} | {r['run']} | {pts(r['before'])} | {curve} | {pts(r['after'])} | {gain:+.2f} | {pts(r['evolve_before'])} → "
                     f"{pts(r['evolve_after'])} | {r['accepted']} | {rmb(r.get('rmb'))} |")  # fmt: skip
        L += ["", "Held-out is evaluated only when the retained harness changes (round -1 is the start).", ""]
    if agg["retrieval"]:
        L += ["## Retrieval ablation (pooled SIM of all Spider source runs, continued on BIRD)", "",
              "| arm | before | after | evolve after | search tokens | RMB |", "|---|---|---|---|---|---|"]  # fmt: skip
        for r in agg["retrieval"]:
            L.append(
                f"| {r['condition']} | {pts(r['before'])} | {pts(r['after'])} | {pts(r['evolve_after'])} | {r['search_tokens']} | {rmb(r.get('rmb'))} |"
            )
        L.append("")
    if agg.get("not_run") or agg.get("unfunded"):
        L += ["## Planned but not run / not finished", ""]
        for r in agg.get("not_run", []):
            L.append(f"- `{r['run']}` ({r['group']}): {r['state']}")
        if agg.get("unfunded"):
            L.append(
                f"- unfunded under deviation 3 (offline fake-model test only): {', '.join(f'`{n}`' for n in agg['unfunded'])}"
            )
        L.append("")
    if agg["eval_variance"]:
        L += ["## Repeat-evaluation variance (same harness, same split, different seeds)", "",
              "| domain | split | model | harness tree | scores | sd | taken from |", "|---|---|---|---|---|---|---|"]  # fmt: skip
        for v in agg["eval_variance"]:
            src = f"cached {v['projected_from']} evaluation" if v.get("projected_from") else "direct"
            L.append(
                f"| {v['domain']} | {v['split']} | {v['model']} | {v['tree']} | {', '.join(f'{x:.2f}' for x in v['scores'])} | {v['sd']:.2f} | {src} |"
            )
        L.append("")
    sp = agg.get("spend")
    if sp:
        L += ["## Money (RMB spend ledger, every call of the revised plan)", "",
              f"Spent **RMB {sp['spent_rmb']:.4f}** of the RMB {sp['cap_rmb']:.2f} hard cap in {sp['calls']} calls "
              f"({sp['failed']} failed upstream, not billed). Prices (RMB per 1M input / output / cached input): "
              + "; ".join(f"{m} {v[0]} / {v[1]} / {v[2]}" for m, v in (sp.get("prices_rmb_per_million") or {}).items()) + ".", "",
              "| role | calls | prompt tokens | cached | completion tokens | RMB |", "|---|---|---|---|---|---|"]  # fmt: skip
        for k, v in sp["by_role"].items():
            L.append(
                f"| {k} | {v['calls']} | {v['prompt_tokens']} | {v['cached_tokens']} | {v['completion_tokens']} | {v['rmb']:.4f} |"
            )
        L.append("")
    pools = agg["ledger"].get("pools", {})
    if pools:
        L += ["## Calls (gateway call ledger of the revised plan's root)", "",
              "| pool | calls | failed | prompt tokens | completion tokens |", "|---|---|---|---|---|"]  # fmt: skip
        for k, v in pools.items():
            L.append(
                f"| {k} | {v['calls']} | {v['failed']} | {v['prompt_tokens']} | {v['completion_tokens']} |"
            )
        L.append("")
    pre = agg.get("pre_revision")
    if pre:
        L += ["## Pre-revision (PREREG as written; reported separately, not pooled)", "", pre["note"], ""]
        L += ["| run | search model | evolve split | evolve before → after | held-out before → after | accepted | policy calls | search calls |",
              "|---|---|---|---|---|---|---|---|"]  # fmt: skip
        for n, r in pre["runs"].items():
            L.append(f"| {n} | {r.get('search_model', 'qwen4-max')} | {r['evolve_split']} | {pts(r['evolve_start'])} → {pts(r['evolve_end'])} | "
                     f"{pts(r['heldout_start'])} → {pts(r['heldout_end'])} | {r['accepted']} | {r['cost']['policy_calls']} | {r['cost']['search_calls']} |")  # fmt: skip
        L += [
            "",
            "| role \\| model | calls | failed | prompt tokens | completion tokens | RMB (estimate) |",
            "|---|---|---|---|---|---|",
        ]
        for k, v in pre["ledger"]["models"].items():
            L.append(
                f"| {k} | {v['calls']} | {v['failed']} | {v['prompt_tokens']} | {v['completion_tokens']} | {rmb(v['rmb_estimate'])} |"
            )
        L += ["", "Raw files: `results/pre_revision/`; the outage: `results/validity.json`.", ""]
    _ = wilson_ci
    return "\n".join(L) + "\n"
