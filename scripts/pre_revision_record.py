"""Freeze the pre-revision record (PREREG deviation 3) from runs/main into results/:

  results/validity.json              the 2026-11-19 provider outage: when it started, which runs and
                                     evaluations saw 403s, what was set aside and why sql-src-4 is clean
  results/pre_revision/summary.json  sql-src-4 (the one clean pre-revision run), repeat-evaluation
                                     variance of the seed harnesses, final call/token ledger
  results/pre_revision/runs/sql-src-4/  raw run files (as `lamarck report` exports them)

Reads only local files (no network). Run once: uv run python scripts/pre_revision_record.py
"""

from __future__ import annotations

import collections
import json
import shutil
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lamarck.experiments.report import export_run, load_run, run_cost
from lamarck.types import EvalResult

REPO = Path(__file__).resolve().parents[1]
MAIN = REPO / "runs" / "main"
OUT = REPO / "results"
FLASH_RMB = (0.80, 2.70)  # qwen3.8-flash, RMB per 1M input / output tokens (see lamarck/gateway/spend.py)


def outage() -> dict[str, object]:
    first_403: str | None = None
    by_run: collections.Counter[str] = collections.Counter()
    by_role: collections.Counter[str] = collections.Counter()
    last_ok: dict[str, str] = {}
    status: collections.Counter[int] = collections.Counter()
    message = ""
    for line in (MAIN / "gateway-state" / "audit.jsonl").read_text().splitlines():
        r = json.loads(line)
        status[int(r["status"])] += 1
        if r["status"] == 403:
            first_403 = first_403 or r["ts"]
            by_run[r["run"]] += 1
            by_role[r["role"]] += 1
        elif r["status"] == 200:
            last_ok[r["run"]] = r["ts"]
    for log in sorted((MAIN / "runs").glob("*/events.log")) + sorted(
        (MAIN / "aborted" / "outage-20261119").rglob("events.log")
    ):
        for ln in log.read_text().splitlines():
            if "AccessDenied" in ln and not message:
                message = ln[ln.index("{") :][:200] if "{" in ln else ln[:200]
    finished = {
        d.name
        for d in (MAIN / "runs").iterdir()
        if (d / "state.json").exists() and json.loads((d / "state.json").read_text()).get("status") == "done"
    }
    clean = sorted(r for r in finished if r not in by_run)
    return {
        "what": "The provider account ran out of credit mid-run on 2026-11-19 (HTTP 403 AccessDenied.Unpurchased). "
        "Before commit 9cbc0c2 the evaluator scored a 403 as a failed task, so every evaluation that saw one "
        "measured the infrastructure, not the harness.",
        "first_403_utc": first_403,
        "provider_message": message,
        "audit_status_counts": dict(sorted(status.items())),
        "calls_with_403_by_run": dict(sorted(by_run.items())),
        "calls_with_403_by_role": dict(sorted(by_role.items())),
        "finished_runs_without_403": {r: {"last_call_utc": last_ok[r]} for r in clean},
        "set_aside": {
            "runs/main/aborted/outage-20261119": "target runs sql-full-1..5, sql-transfer-1..4 and the tainted "
            "held-out files of sql-src-1,2,3,5,6",
            "runs/main/runs/sql-src-{1,3,5,6}": "round 3 candidate evaluations saw 403s (0 admissible); kept on disk, "
            "not reported",
            "runs/main/runs/sql-src-2": "rounds 0-3 finished before the outage but its final held-out evaluation "
            "saw 403s (54.17 on 120); not reported",
        },
        "consequence": "Every run is re-done under PREREG deviation 3 in runs/rev3; only sql-src-4 is reported "
        "from before the revision, separately (results/pre_revision/).",
    }


def variance() -> list[dict[str, object]]:
    out = []
    for r0 in sorted((MAIN / "cache" / "evals").glob("*/*/*/*__r0.json")):
        reps = sorted(r0.parent.glob(r0.name.replace("__r0.json", "__r*.json")))
        if len(reps) >= 2:
            scores = [EvalResult.load(p).score * 100 for p in reps]
            out.append({"domain": r0.parts[-4], "split": r0.parts[-3], "model": r0.parts[-2],
                        "tree": r0.name.split("__")[0], "scores": scores,
                        "mean": statistics.fmean(scores), "sd": statistics.stdev(scores)})  # fmt: skip
    return out


def main() -> None:
    (OUT / "validity.json").write_text(json.dumps(outage(), indent=1) + "\n")
    ledger = json.loads((MAIN / "gateway-state" / "ledger.json").read_text())
    pre = OUT / "pre_revision"
    if (OUT / "runs" / "sql-src-4").exists():
        shutil.rmtree(OUT / "runs" / "sql-src-4")
    export_run(MAIN, pre, "sql-src-4")
    run = load_run(MAIN, "sql-src-4")
    assert run is not None
    run.pop("history", None)
    run["cost"] = run_cost(ledger, "sql-src-4")
    models = {}
    for key, v in sorted(ledger["models"].items()):
        model = key.split("|", 1)[1]
        est = (
            (v["prompt_tokens"] * FLASH_RMB[0] + v["completion_tokens"] * FLASH_RMB[1]) / 1e6
            if model == "qwen3.8-flash"
            else None
        )
        models[key] = {**v, "rmb_estimate": est}  # fmt: skip
    doc = {
        "note": "Pre-revision runs (PREREG as written: qwen4-max search, full splits, N=4, k=3). Only sql-src-4 "
        "finished before the outage (results/validity.json). The call/token ledger is the gateway's final "
        "count for runs/main, including pilot/calibration evaluations, discarded attempts and the 403s. "
        "rmb_estimate prices qwen3.8-flash tokens at RMB 0.80 / 2.70 per 1M (no cached-token discount); "
        "no price is known for qwen4-max or deepseek-v4.1-flash, so the pre-revision spend is not known exactly.",
        "runs": {"sql-src-4": run},
        "eval_variance": variance(),
        "ledger": {"pools": ledger["pools"], "models": models},
    }
    (pre / "summary.json").write_text(json.dumps(doc, indent=1) + "\n")
    print(json.dumps({"validity": str(OUT / "validity.json"), "pre_revision": str(pre)}))


if __name__ == "__main__":
    main()
