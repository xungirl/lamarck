"""Provider outages are never scored as task failures; sub-splits reuse cached parent evaluations;
the RMB cap stops a run cleanly."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lamarck.config import RunConfig
from lamarck.domains.base import Task
from lamarck.domains.toy.domain import ToyDomain
from lamarck.engine.evaluator import BudgetStop, Evaluator, InfraFailure, infra_tainted
from lamarck.gateway import fakes
from lamarck.gateway.providers import UpstreamError
from lamarck.stack import Stack, local_stack
from lamarck.types import EvalResult, TaskOutcome


def denied(body: dict[str, Any]) -> str:
    raise UpstreamError('upstream 403: {"code":"AccessDenied.Unpurchased"}', 403, transient=False)


class HalfToy(ToyDomain):
    """The toy domain plus a sub-split `evolve_half` of `evolve`."""

    def parent_split(self, split: str) -> str | None:
        return "evolve" if split == "evolve_half" else None

    def tasks(self, split: str) -> list[Task]:
        if split == "evolve_half":
            return super().tasks("evolve")[::2]
        return super().tasks(split)

    def data_mounts(self, split: str) -> dict[str, Path]:
        return super().data_mounts("evolve" if split == "evolve_half" else split)


def evaluator(stack: Stack, domain: ToyDomain, tmp_path: Path) -> Evaluator:
    return Evaluator(
        domain, stack.backend, tmp_path / "cache", tmp_path / "work", stack.grant("ev", ["policy"])
    )


def test_outage_raises_and_is_not_cached(
    stack: Stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(fakes.RESPONDERS, "fake-down", denied)
    dom = ToyDomain()
    ev = evaluator(stack, dom, tmp_path)
    with pytest.raises(InfraFailure, match="403"):
        ev.evaluate(dom.seed_harness, "seedtree", "evolve", "fake-down")
    assert not ev.cache_path("evolve", "fake-down", "seedtree", 0).exists()


def test_run_pauses_on_outage_and_resumes(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = RunConfig(run_id="r", domain="toy", rounds=2, k=2, policy_model="fake-down")
    monkeypatch.setitem(fakes.RESPONDERS, "fake-down", denied)
    run = stack.make_run(cfg)
    with pytest.raises(InfraFailure):
        run.run()
    assert run.state()["status"] == "infra_error"
    monkeypatch.setitem(fakes.RESPONDERS, "fake-down", fakes.RESPONDERS["fake-policy"])
    s = run.run()  # provider is back: the run resumes and finishes
    assert run.state()["status"] == "done" and s["evolve_end"] >= s["evolve_start"]
    assert s["heldout_end"] is not None


def test_tainted_cache_file_is_set_aside(stack: Stack, tmp_path: Path) -> None:
    dom = ToyDomain()
    ev = evaluator(stack, dom, tmp_path)
    path = ev.cache_path("evolve", "fake-policy", "t1", 0)
    bad = EvalResult(
        "t1",
        "evolve",
        "fake-policy",
        0,
        {"x": TaskOutcome("x", 0.0, error="GatewayError: gateway 403: upstream 403")},
    )
    bad.save(path)
    assert infra_tainted(bad) == ["x"]
    good = ev.evaluate(dom.seed_harness, "t1", "evolve", "fake-policy")
    assert not infra_tainted(good) and len(good.outcomes) > 1
    assert path.with_name(path.name + ".infra-tainted").exists()


def test_subsplit_is_projected_from_cached_parent(stack: Stack, tmp_path: Path) -> None:
    dom = HalfToy()
    ev = evaluator(stack, dom, tmp_path)
    full = ev.evaluate(dom.seed_harness, "seedtree", "evolve", "fake-policy")
    half = ev.evaluate(dom.seed_harness, "seedtree", "evolve_half", "fake-policy")
    assert half.projected_from == "evolve"
    assert set(half.outcomes) == {t.id for t in dom.tasks("evolve_half")}
    assert all(half.outcomes[t] == full.outcomes[t] for t in half.outcomes)
    again = EvalResult.load(ev.cache_path("evolve_half", "fake-policy", "seedtree", 0))
    assert again.projected_from == "evolve"


def test_spend_cap_stops_run_cleanly(tmp_path: Path) -> None:
    # fake models cost nothing, so a zero cap must still let them run ...
    st = local_stack(tmp_path, spend_file=tmp_path / "spend.json", spend_cap=0.0)
    st.make_run(RunConfig(run_id="free", domain="toy", rounds=1, k=1)).run()
    # ... while any priced model is refused before it is sent
    st2 = local_stack(tmp_path / "b", spend_file=tmp_path / "b" / "spend.json", spend_cap=0.0)
    run = st2.make_run(RunConfig(run_id="priced", domain="toy", rounds=1, k=1, policy_model="qwen3.8-flash"))
    with pytest.raises(BudgetStop):
        run.run()
    assert run.state()["status"] == "budget_stop"


def test_parallel_processes_share_one_evaluation(tmp_path: Path) -> None:
    """Regression (PREREG deviation 4): two processes evaluating the same (split, model, tree, rollout)
    at the same time must produce one evaluation, not two with the later overwriting the earlier."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path
        from lamarck.domains.registry import get_domain
        from lamarck.engine.evaluator import Evaluator
        from lamarck.types import EvalResult

        calls = Path({str(tmp_path / "calls")!r})
        class Ev(Evaluator):
            def _run(self, harness_dir, tree, split, model, rollout):
                with open(calls, "a") as f:
                    f.write(sys.argv[1] + "\\n")
                time.sleep(1.0)
                return EvalResult(tree, split, model, rollout, {{}}, wall_s=0.0)
        ev = Ev(get_domain("toy"), None, Path({str(tmp_path / "cache")!r}), Path({str(tmp_path / "work")!r}), "t")
        ev.evaluate(Path("."), "tree1", "evolve", "fake-policy", 0)
        """
    )
    procs = [subprocess.Popen([sys.executable, "-c", script, str(i)]) for i in range(2)]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    assert len((tmp_path / "calls").read_text().splitlines()) == 1


def test_resume_after_outage_in_final_heldout_point_measures_it(stack: Stack) -> None:
    """An outage during the held-out evaluation after the last round (round already saved, next_round ==
    rounds) must not leave the run without its end point: the resumed run evaluates it instead of reporting
    the start as the end (Study 3, s3-src-5)."""
    import json

    cfg = RunConfig(run_id="r-end", domain="toy", rounds=2, k=2)
    run = stack.make_run(cfg)
    orig = run.heldout_point
    calls = {"n": 0}

    def flaky(after_round: int) -> None:
        if after_round == cfg.rounds - 1 and calls["n"] == 0:
            calls["n"] += 1
            raise InfraFailure("heldout: gateway 403: upstream 403 AccessDenied.Unpurchased")
        orig(after_round)

    run.heldout_point = flaky  # type: ignore[method-assign]
    with pytest.raises(InfraFailure):
        run.run()
    assert run.state()["status"] == "infra_error" and run.state()["next_round"] == cfg.rounds
    s = run.run()
    pts = [json.loads(x) for x in (run.dir / "heldout.jsonl").read_text().splitlines()]
    assert sorted(p["after_round"] for p in pts) == [-1, cfg.rounds - 1]
    assert max(p["after_round"] for p in s["heldout_curve"]) == cfg.rounds - 1
    assert run.state()["status"] == "done"
