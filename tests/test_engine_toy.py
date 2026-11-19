"""End-to-end RSI and CRSI on the deterministic toy domain (fake models, subprocess sandbox)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lamarck.config import RunConfig
from lamarck.engine.evaluator import BudgetStop
from lamarck.gateway.client import ledger_snapshot
from lamarck.stack import Stack


def src_cfg(**kw: object) -> RunConfig:
    return RunConfig(run_id="src", domain="toy", rounds=3, k=2, **kw)  # type: ignore[arg-type]


def tgt_cfg(memory: str, run_id: str) -> RunConfig:
    return RunConfig(
        run_id=run_id,
        domain="toy",
        evolve_split="t_evolve",
        heldout_split="t_heldout",
        rounds=2,
        k=2,
        memory=memory,  # type: ignore[arg-type]
    )


def prompts(run_dir: Path) -> list[str]:
    out = []
    for p in sorted(run_dir.glob("rounds/r*/s*/prompt.json")):
        out.append("\n".join(m["content"] for m in json.loads(p.read_text())))
    return out


def test_source_run_improves_and_records_sim(stack: Stack) -> None:
    run = stack.make_run(src_cfg())
    s = run.run()
    assert s["evolve_end"] > s["evolve_start"]
    sim = run.sim()
    sim.check_invariants()
    assert sim.n_attempts == s["accepted"] >= 1
    for p in sim.problems:
        for a in p.attempts:
            assert a.accepted and set(a.fixed) <= set(a.predicted) and a.domain == "evolve/fake-policy"


def test_predictions_are_sealed_before_evaluation(stack: Stack) -> None:
    run = stack.make_run(src_cfg())
    run.run()
    for m in run.dir.glob("rounds/r*/s*/manifest.json"):
        man = json.loads(m.read_text())
        if "tree" not in man:
            continue
        cache = run.evaluator.cache_path("evolve", "fake-policy", man["tree"], 0)
        assert cache.exists()
        assert man["sealed_at"] <= cache.stat().st_mtime


def test_heldout_never_reaches_the_search(stack: Stack) -> None:
    run = stack.make_run(src_cfg(heldout_curve=True))
    run.run()
    texts = prompts(run.dir)
    assert texts and not any("heldout-" in t for t in texts)


def test_memory_conditions_control_what_the_proposer_sees(stack: Stack) -> None:
    src = stack.make_run(src_cfg())
    src.run()
    full = stack.make_run(tgt_cfg("full", "full"), source=src)
    probs = stack.make_run(tgt_cfg("problems", "problems"), source=src)
    none = stack.make_run(tgt_cfg("none", "transfer"), source=src)
    for r in (full, probs, none):
        r.run()
    f0 = prompts(full.dir)[0]
    assert "SIM JSON" in f0 and '"attempts"' in f0 and '"carried_from"' in f0
    p0 = prompts(probs.dir)[0]
    assert "KNOWN HARNESS PROBLEMS" in p0 and '"attempts"' not in p0 and "SIM JSON" not in p0
    n0 = prompts(none.dir)[0]
    assert "SELF-IMPROVEMENT MEMORY" not in n0
    # the three transfer arms start from the same harness and share its cached evaluation
    starts = {
        json.loads((r.dir / "heldout.jsonl").read_text().splitlines()[0])["tree"] for r in (full, probs, none)
    }
    assert starts == {src.repo.tree("evolve")}
    sim = full.sim()
    assert sim.carried_from == {"arm": "src", "domain": "evolve/fake-policy", "into": "full"}
    n_target = sum(a.domain == "t_evolve/fake-policy" for p in sim.problems for a in p.attempts)
    assert n_target == json.loads((full.dir / "summary.json").read_text())["accepted"]
    assert sim.n_attempts == src.sim().n_attempts + n_target  # source attempts are kept


def test_resume_spends_no_extra_search_calls(stack: Stack, tmp_path: Path) -> None:
    run = stack.make_run(src_cfg())
    run.run()
    calls = ledger_snapshot(stack.gateway_url, stack.admin_secret)["pools"]["search"]["calls"]
    # simulate a crash after round 1 finished: rewind the state and rerun
    run.save_state(next_round=1, status="running")
    sim_before = run.sim().n_attempts
    run.run()
    after = ledger_snapshot(stack.gateway_url, stack.admin_secret)["pools"]["search"]["calls"]
    assert after == calls
    assert run.sim().n_attempts == sim_before  # SIM update is idempotent


def test_budget_stop_is_clean(tmp_path: Path) -> None:
    from lamarck.stack import local_stack

    st = local_stack(tmp_path, caps={"policy": 40, "search": 3000})
    run = st.make_run(src_cfg())
    with pytest.raises(BudgetStop):
        run.run()
    assert run.state()["status"] == "budget_stop"
