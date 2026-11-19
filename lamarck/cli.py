"""Command line: start the gateway, run planned experiments, schedule them, export results.

lamarck gateway-up [--root runs/main]              build the image, (re)start the gateway container
lamarck eval --domain D --split S --model M        evaluate a harness (seed by default) once
lamarck exp NAME                                    run one planned run (see lamarck.experiments.plan)
lamarck schedule [--group G ...] [--parallel P]     run every planned run whose dependencies are done
lamarck status                                      planned runs and their state
lamarck ledger                                      calls and tokens per role/model so far
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .sim.memory import SIM
from .stack import Stack

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO / "runs" / "main"
KEYFILE = Path("/Users/xunxun/flowbeat/backend/.env")


def attach(root: Path) -> Stack:
    """A Stack bound to the already-running gateway container (does not restart it)."""
    from .sandbox import docker_backend, image

    tag = image.build(root / "build" / "ctx")
    secret = (root / "gateway-state" / "admin_secret").read_text().strip()
    backend = docker_backend.DockerBackend(tag, root / "slots", warm=1, memory_mb=1024)
    return Stack(f"http://127.0.0.1:{docker_backend.GATEWAY_PORT}", secret, backend, root)


def cmd_gateway_up(a: argparse.Namespace) -> None:
    from .sandbox import docker_backend, image

    tag = image.build(a.root / "build" / "ctx")
    url = docker_backend.start_gateway(
        tag, a.root / "gateway-state", KEYFILE if not a.offline else None, fake=True
    )
    print(f"gateway {url} image {tag}")


def run_spec(name: str, root: Path) -> dict[str, Any]:
    from .engine.loop import EvolutionRun
    from .experiments.plan import POLICY_PARAMS, RUN_BUDGET, SEARCH_EXTRA, build_plan

    plan = build_plan()
    spec = plan[name]
    stack = attach(root)
    orig_grant = stack.grant

    def budgeted(run_id: str, roles: list[str], budget: dict[str, int] | None = None) -> str:
        return orig_grant(run_id, roles, budget or {r: RUN_BUDGET[r] for r in roles if r in RUN_BUDGET})

    stack.grant = budgeted  # type: ignore[method-assign]
    source: EvolutionRun | None = None
    if spec.source:
        source = stack.make_run(
            plan[spec.source].cfg, policy_params=POLICY_PARAMS[plan[spec.source].cfg.policy_model]
        )
        if source.state().get("status") != "done":
            raise SystemExit(f"source {spec.source} is not done")
    fresh = not (root / "runs" / name / "state.json").exists()
    run = stack.make_run(
        spec.cfg, source=source, policy_params=POLICY_PARAMS[spec.cfg.policy_model], search_extra=SEARCH_EXTRA
    )
    if fresh and spec.pooled_sim_from:
        mems = [SIM.load(root / "runs" / s / "sim.json") for s in spec.pooled_sim_from]
        pooled = SIM.pooled(mems, spec.pooled_sim_from)
        pooled.carried_from = {
            "arm": "pooled:" + ",".join(spec.pooled_sim_from),
            "domain": "spider_evolve/qwen3.8-flash",
            "into": name,
        }
        pooled.save(run.sim_path)
    if spec.cfg.memory == "retrieved":
        from .sim import embed, retrieval

        embed.enable_bge()
        (run.dir / "embedder.txt").write_text(retrieval.embedder_name() + "\n")
    try:
        return run.run()
    finally:
        stack.backend.close()


def cmd_exp(a: argparse.Namespace) -> None:
    print(json.dumps(run_spec(a.name, a.root), indent=1))


def state_of(root: Path, name: str) -> str:
    p = root / "runs" / name / "state.json"
    return json.loads(p.read_text()).get("status", "?") if p.exists() else "pending"


def cmd_status(a: argparse.Namespace) -> None:
    from .experiments.plan import build_plan

    for name, spec in build_plan().items():
        print(f"{name:18s} {spec.group:15s} {state_of(a.root, name)}")


def cmd_schedule(a: argparse.Namespace) -> None:
    """Run planned runs in dependency order, P at a time, each in its own process."""
    from .experiments.plan import build_plan, dependencies

    plan = build_plan()
    todo = [n for n, s in plan.items() if (not a.group or s.group in a.group) and (not a.only or n in a.only)]
    running: dict[str, subprocess.Popen[bytes]] = {}
    failed: set[str] = set()
    logs = a.root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    while True:
        for n, p in list(running.items()):
            if p.poll() is not None:
                del running[n]
                st = state_of(a.root, n)
                print(
                    f"{time.strftime('%H:%M:%S')} finished {n}: exit {p.returncode}, state {st}", flush=True
                )
                if st != "done":
                    failed.add(n)
                if st == "budget_stop":
                    print("budget stop reached; not launching more runs", flush=True)
                    todo = []
        pending = [n for n in todo if state_of(a.root, n) != "done" and n not in running and n not in failed]
        ready = [n for n in pending if all(state_of(a.root, d) == "done" for d in dependencies(plan[n]))]
        while ready and len(running) < a.parallel:
            n = ready.pop(0)
            with open(logs / f"{n}.log", "a") as log:
                running[n] = subprocess.Popen(
                    [sys.executable, "-m", "lamarck.cli", "exp", n, "--root", str(a.root)],
                    stdout=log,
                    stderr=log,
                    cwd=REPO,
                )
            print(f"{time.strftime('%H:%M:%S')} started {n}", flush=True)
        if not running and not ready:
            blocked = [n for n in pending if n not in failed]
            print(f"done. failed={sorted(failed)} blocked={blocked}", flush=True)
            return
        time.sleep(10)


def cmd_eval(a: argparse.Namespace) -> None:
    from .domains.registry import get_domain
    from .engine.evaluator import Evaluator
    from .experiments.plan import POLICY_PARAMS

    stack = attach(a.root)
    dom = get_domain(a.domain)
    harness = Path(a.harness) if a.harness else dom.seed_harness
    token = stack.grant(f"eval-{a.domain}-{a.split}", ["policy"], {"policy": 3000})
    ev = Evaluator(
        dom,
        stack.backend,
        a.root / "cache" / "evals",
        a.root / "work",
        token,
        task_parallel=8,
        policy_params=POLICY_PARAMS[a.model],
    )
    tree = a.tree or harness_tree(harness, a.root / "work")
    try:
        for r in a.rollouts:
            res = ev.evaluate(harness, tree, a.split, a.model, rollout=r)
            print(
                json.dumps(
                    {
                        "split": a.split,
                        "model": a.model,
                        "rollout": r,
                        "score": round(res.score * 100, 2),
                        "passed": len(res.passed),
                        "n": len(res.outcomes),
                        "cost": round(res.cost, 1),
                        "calls": res.calls,
                        "wall_s": round(res.wall_s, 1),
                    }
                )
            )
    finally:
        stack.backend.close()


def harness_tree(harness: Path, work: Path) -> str:
    """The same content hash a run's repo would give this directory (so cached evals are shared)."""
    import shutil
    import tempfile

    from .engine.gitops import HarnessRepo

    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as tmp:
        tree = HarnessRepo.create(Path(tmp) / "repo", harness).tree()
        shutil.rmtree(tmp, ignore_errors=True)
    return tree


def cmd_ledger(a: argparse.Namespace) -> None:
    from .gateway.client import ledger_snapshot

    secret = (a.root / "gateway-state" / "admin_secret").read_text().strip()
    snap = ledger_snapshot("http://127.0.0.1:18080", secret)
    print(json.dumps({"pools": snap["pools"], "models": snap["models"], "caps": snap["caps"]}, indent=1))


def main() -> None:
    p = argparse.ArgumentParser(prog="lamarck")
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gateway-up")
    g.add_argument("--offline", action="store_true")
    g.set_defaults(fn=cmd_gateway_up)
    e = sub.add_parser("exp")
    e.add_argument("name")
    e.set_defaults(fn=cmd_exp)
    s = sub.add_parser("schedule")
    s.add_argument("--group", nargs="*", default=[])
    s.add_argument("--only", nargs="*", default=[])
    s.add_argument("--parallel", type=int, default=4)
    s.set_defaults(fn=cmd_schedule)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("ledger").set_defaults(fn=cmd_ledger)
    v = sub.add_parser("eval")
    v.add_argument("--domain", required=True)
    v.add_argument("--split", required=True)
    v.add_argument("--model", default="qwen3.8-flash")
    v.add_argument("--harness", default="")
    v.add_argument("--tree", default="")
    v.add_argument("--rollouts", type=int, nargs="+", default=[0])
    v.set_defaults(fn=cmd_eval)
    # allow `--root` after the subcommand too
    for sp in (g, e, s, v):
        sp.add_argument("--root", type=Path, default=argparse.SUPPRESS)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
