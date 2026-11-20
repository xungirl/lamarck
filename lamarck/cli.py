"""Command line: start the gateway, run planned experiments, schedule them, export results.

lamarck gateway-up [--root runs/main]              build the image, (re)start the gateway container
lamarck eval --domain D --split S --model M        evaluate a harness (seed by default) once
lamarck exp NAME                                    run one planned run (see lamarck.experiments.plan)
lamarck schedule [--group G ...] [--parallel P]     run every planned run whose dependencies are done
lamarck status                                      planned runs and their state
lamarck ledger                                      calls and tokens per role/model so far
lamarck seed-cache --from runs/rev3                 copy the seed harness's cached evaluations (Study 2)

`--plan rev3` (default) is the budget-revised plan (PREREG deviations 3 and 5); `--plan study2` is the
follow-up "Study 2: strong search model" (lamarck/experiments/study2.py), with its own default root
(runs/study2) and its own spend ledger and cap; `--plan study3` is its fixed-n replication "Study 3"
(lamarck/experiments/study3.py, runs/study3, its own ledger and cap).

Offline dry runs: with LAMARCK_BACKEND=local, runs use the subprocess backend and a gateway already listening
on 127.0.0.1:$LAMARCK_GATEWAY_PORT (e.g. `python -m lamarck.gateway.server --simulate ...`) instead of
Docker. Never used for real runs (no sandbox isolation).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

from .sim.memory import SIM
from .stack import Stack

REPO = Path(__file__).resolve().parents[1]
# Experiment root of the revised plan (PREREG deviation 3). runs/main holds the pre-revision runs.
DEFAULT_ROOT = REPO / "runs" / "rev3"
# Env file holding DASHSCOPE_API_KEY; override with LAMARCK_KEYFILE. Never committed (.gitignore).
KEYFILE = Path(os.environ.get("LAMARCK_KEYFILE", REPO / ".env"))
# The RMB spend ledger covers every real call this project makes after the 2026-11-19 budget
# revision (PREREG deviation 3); committed so the spend is auditable.
SPEND_FILE = REPO / "results" / "budget" / "spend_ledger.json"
SPEND_CAP_RMB = 9.50
PLANS = ("rev3", "study2", "study3")


def plan_module(name: str) -> ModuleType:
    """The experiment plan module: build_plan, dependencies, EST_RMB, UNFUNDED_GROUPS, POLICY_PARAMS,
    RUN_BUDGET, SEARCH_EXTRA."""
    if name == "study2":
        from .experiments import study2

        return study2
    if name == "study3":
        from .experiments import study3

        return study3
    if name == "rev3":
        from .experiments import plan

        return plan
    raise SystemExit(f"unknown plan {name!r}")


def plan_money(name: str) -> tuple[Path, float]:
    """(spend ledger file, hard RMB cap) of a plan. Each study has its own ledger."""
    if name in ("study2", "study3"):
        pm = plan_module(name)
        return pm.SPEND_FILE, pm.SPEND_CAP_RMB
    return SPEND_FILE, SPEND_CAP_RMB


def plan_root(name: str) -> Path:
    if name in ("study2", "study3"):
        return Path(plan_module(name).DEFAULT_ROOT)
    return DEFAULT_ROOT


def gateway_port() -> int:
    from .sandbox.docker_backend import GATEWAY_PORT

    return int(os.environ.get("LAMARCK_GATEWAY_PORT", GATEWAY_PORT))


def local_mode() -> bool:
    return os.environ.get("LAMARCK_BACKEND", "docker") == "local"


def attach(root: Path) -> Stack:
    """A Stack bound to the already-running gateway (does not restart it): the gateway container and the
    Docker backend, or with LAMARCK_BACKEND=local a local gateway process and the subprocess backend."""
    secret = (root / "gateway-state" / "admin_secret").read_text().strip()
    url = f"http://127.0.0.1:{gateway_port()}"
    if local_mode():
        from .sandbox.backends import LocalBackend

        return Stack(url, secret, LocalBackend(url), root)
    from .sandbox import docker_backend, image

    tag = image.build(root / "build" / "ctx")
    backend = docker_backend.DockerBackend(tag, root / "slots", warm=1, memory_mb=1024)
    return Stack(url, secret, backend, root)


def cmd_gateway_up(a: argparse.Namespace) -> None:
    from .sandbox import docker_backend, image

    spend_file, cap = plan_money(a.plan)
    tag = image.build(a.root / "build" / "ctx")
    url = docker_backend.start_gateway(
        tag,
        a.root / "gateway-state",
        KEYFILE if not a.offline else None,
        fake=True,
        spend_file=spend_file if not a.offline else None,
        spend_cap=cap,
    )
    print(
        f"gateway {url} image {tag} spend ledger {spend_file if not a.offline else 'off'} cap RMB {cap:.2f}"
    )


def run_spec(name: str, root: Path, plan_name: str = "rev3") -> dict[str, Any]:
    from .engine.loop import EvolutionRun

    pm = plan_module(plan_name)
    POLICY_PARAMS, RUN_BUDGET, SEARCH_EXTRA = pm.POLICY_PARAMS, pm.RUN_BUDGET, pm.SEARCH_EXTRA
    plan = pm.build_plan()
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
    print(json.dumps(run_spec(a.name, a.root, a.plan), indent=1))


def state_of(root: Path, name: str) -> str:
    p = root / "runs" / name / "state.json"
    return json.loads(p.read_text()).get("status", "?") if p.exists() else "pending"


def cmd_status(a: argparse.Namespace) -> None:
    for name, spec in plan_module(a.plan).build_plan().items():
        print(f"{name:18s} {spec.group:15s} {state_of(a.root, name)}")


def money_gate(
    order: list[str],
    pending: list[str],
    running: list[str],
    est: dict[str, float],
    spend: dict[str, Any],
    reserve: float = 0.0,
    bundles: dict[str, list[str]] | None = None,
) -> set[str]:
    """Runs that may be launched now without putting a higher-priority run at risk.

    left = cap - spent - in-flight reservations - (est - spent so far) of every running run.
    Walking the priority order, every pending run (launchable now or still waiting for its
    dependencies) claims its estimate; a run is allowed only if its claim still fits.
    So a cheap low-priority run never takes money that a higher-priority run will need.
    `reserve` is headroom that is never planned for; `bundles` maps a run to the runs that depend on it
    and are claimed together with it.
    """
    by = spend.get("by", {})
    left = float(spend.get("cap_rmb", 0.0)) - float(spend.get("spent_rmb", 0.0)) - reserve
    left -= float(spend.get("in_flight_rmb", 0.0))
    for n in running:
        left -= max(0.0, est[n] - float(by.get(f"run:{n}", {}).get("rmb", 0.0)))
    claimed: set[str] = set()
    allowed: set[str] = set()
    for n in order:
        if n not in pending or n in claimed:
            continue
        # a run is launched together with the claim of its pending dependents (e.g. a source run and
        # its two paired arms), so a unit is started only if all of it fits
        unit = [n] + [d for d in (bundles or {}).get(n, []) if d in pending and d not in claimed]
        need = sum(est[u] for u in unit)
        if need <= left:
            allowed.add(n)
        left -= need
        claimed.update(unit)
    return allowed


def cmd_schedule(a: argparse.Namespace) -> None:
    """Run planned runs in dependency and priority order, P at a time, each in its own process.
    With a real gateway, a run is launched only while the money gate allows it (see money_gate)."""
    pm = plan_module(a.plan)
    EST_RMB, UNFUNDED_GROUPS, dependencies = pm.EST_RMB, pm.UNFUNDED_GROUPS, pm.dependencies
    plan = pm.build_plan()
    todo = [
        n
        for n, s in plan.items()
        if (s.group in a.group if a.group else s.group not in UNFUNDED_GROUPS) and (not a.only or n in a.only)
    ]
    est = {n: EST_RMB[plan[n].group] for n in plan}
    secret = (a.root / "gateway-state" / "admin_secret").read_text().strip()
    running: dict[str, subprocess.Popen[bytes]] = {}
    failed: set[str] = set()
    logs = a.root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    held: set[str] = set()
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
                if st in ("budget_stop", "infra_error"):
                    print(f"{st}: not launching more runs", flush=True)
                    todo = []
        pending = [n for n in todo if state_of(a.root, n) != "done" and n not in running and n not in failed]
        ready = [n for n in pending if all(state_of(a.root, d) == "done" for d in dependencies(plan[n]))]
        if ready and len(running) < a.parallel and not a.no_money_gate:
            from .gateway.client import ledger_snapshot

            spend = ledger_snapshot(f"http://127.0.0.1:{a.port}", secret).get("spend")
            if spend is not None:
                bundles = {n: [m for m in todo if n in dependencies(plan[m])] for n in todo}
                ok = money_gate(todo, pending, list(running), est, spend, a.reserve_rmb, bundles)
                for n in ready:
                    if n not in ok and n not in held:
                        print(f"{time.strftime('%H:%M:%S')} holding {n}: money gate", flush=True)
                        held.add(n)
                ready = [n for n in ready if n in ok]
        while ready and len(running) < a.parallel:
            n = ready.pop(0)
            with open(logs / f"{n}.log", "a") as log:
                running[n] = subprocess.Popen(
                    [sys.executable, "-m", "lamarck.cli", "--plan", a.plan, "exp", n, "--root", str(a.root)],
                    stdout=log,
                    stderr=log,
                    cwd=REPO,
                )
            print(f"{time.strftime('%H:%M:%S')} started {n}", flush=True)
            # the next launch decision must see this run as in flight
            break
        if not running and not ready:
            blocked = [n for n in pending if n not in failed]
            print(f"done. failed={sorted(failed)} not run={blocked}", flush=True)
            return
        time.sleep(10)


def cmd_eval(a: argparse.Namespace) -> None:
    from .domains.registry import get_domain
    from .engine.evaluator import Evaluator

    POLICY_PARAMS = plan_module(a.plan).POLICY_PARAMS

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
    snap = ledger_snapshot(f"http://127.0.0.1:{gateway_port()}", secret)
    print(json.dumps({"pools": snap["pools"], "models": snap["models"], "caps": snap["caps"]}, indent=1))


def seed_cache(root: Path, source: Path, plan_name: str) -> dict[str, Any]:
    """Copy the cached evaluations of the domain seed harness from an earlier experiment root.

    Only cache entries whose key matches exactly are copied: same domain, split (the plan's evolve and held-out
    splits of runs that start from the seed harness, and their parent splits), policy model, seed-harness tree
    and rollout. Existing files are never overwritten and the source is only read. A provenance file records
    what was copied and the sha256 of each file."""
    import hashlib
    import shutil

    from .domains.registry import get_domain

    plan = plan_module(plan_name).build_plan()
    keys: set[tuple[str, str, str]] = set()
    for spec in plan.values():
        if spec.source or spec.pooled_sim_from:
            continue
        dom = get_domain(spec.cfg.domain)
        for split in (spec.cfg.evolve_split, spec.cfg.heldout_split):
            for sp in (split, dom.parent_split(split)):
                if sp:
                    keys.add((spec.cfg.domain, sp, spec.cfg.policy_model))
    copied: list[dict[str, Any]] = []
    trees: dict[str, str] = {}
    for domain, split, model in sorted(keys):
        if domain not in trees:
            trees[domain] = harness_tree(get_domain(domain).seed_harness, root / "work")
        tree = trees[domain]
        rel = Path("cache") / "evals" / domain / split / model.replace("/", "_")
        for f in sorted((source / rel).glob(f"{tree}__r*.json")):
            dst = root / rel / f.name
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            rec = json.loads(dst.read_text())
            copied.append(
                {
                    "file": str(rel / f.name),
                    "sha256": hashlib.sha256(dst.read_bytes()).hexdigest(),
                    "score": rec.get("score"),
                    "projected_from": rec.get("projected_from"),
                }
            )
    prov = {
        "source_root": str(source),
        "seed_trees": trees,
        "copied": copied,
        "copied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if copied:
        p = root / "cache" / "seed_cache_provenance.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as fh:
            fh.write(json.dumps(prov) + "\n")
    return prov


def cmd_seed_cache(a: argparse.Namespace) -> None:
    prov = seed_cache(a.root, Path(a.source).resolve(), a.plan)
    print(json.dumps(prov, indent=1))


def main() -> None:
    p = argparse.ArgumentParser(prog="lamarck")
    p.add_argument("--plan", choices=PLANS, default="rev3")
    p.add_argument("--root", type=Path, default=None)
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
    s.add_argument("--port", type=int, default=gateway_port())
    s.add_argument("--no-money-gate", action="store_true", help="offline/fake runs only")
    s.add_argument("--reserve-rmb", type=float, default=0.0, help="headroom under the cap never planned for")
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
    c = sub.add_parser("seed-cache")
    c.add_argument("--from", dest="source", required=True, help="earlier experiment root (read only)")
    c.set_defaults(fn=cmd_seed_cache)
    # allow `--root` after the subcommand too
    for sp in (g, e, s, v, c):
        sp.add_argument("--root", type=Path, default=argparse.SUPPRESS)
    a = p.parse_args()
    a.root = Path(a.root or plan_root(a.plan)).resolve()
    a.fn(a)


if __name__ == "__main__":
    main()
