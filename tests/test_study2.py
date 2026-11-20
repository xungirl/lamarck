"""Study 2 (strong search model): plan identical to the controls except search model and seeds, routing of
the real model names, the simulated upstream for dry runs, seed-cache reuse, money gate with the run bound,
and the analysis."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from lamarck.cli import money_gate, plan_money, seed_cache
from lamarck.config import RunConfig
from lamarck.experiments import plan as rev3
from lamarck.experiments import study2
from lamarck.gateway.ledger import Ledger
from lamarck.gateway.providers import FakeProvider, Router, UpstreamError
from lamarck.gateway.server import Gateway, GatewayConfig, Grant, build_gateway
from lamarck.gateway.simulated import SimulatedDashScope
from lamarck.gateway.spend import SpendLedger, price_of
from lamarck.stack import Stack


# ------------------------------------------------------------------ plan
def test_plan_identical_to_controls_except_search_model_and_seed() -> None:
    plan = study2.build_plan()
    controls = rev3.build_plan()
    assert list(plan) == [f"s2-src-{i}" for i in range(1, 11)]
    assert list(study2.PRIMARY_RUNS) + list(study2.EXTENSION_RUNS) == list(plan)
    seeds = [s.cfg.seed for s in plan.values()]
    assert seeds == list(range(11, 21))
    assert [plan[n].cfg.seed for n in study2.PRIMARY_RUNS] == [11, 12, 13, 14, 15]
    assert not set(seeds) & {controls[f"sql-src-{i}"].cfg.seed for i in range(1, 8)}
    ref = asdict(controls["sql-src-1"].cfg)
    for spec in plan.values():
        cfg = asdict(spec.cfg)
        diff = {k for k in cfg if cfg[k] != ref[k]}
        assert diff <= {"run_id", "seed", "search_model"}
        assert spec.cfg.search_model == "qwen4-max" and spec.cfg.policy_model == "qwen3.8-flash"
        assert (spec.cfg.rounds, spec.cfg.k) == (3, 2)
        assert (spec.cfg.evolve_split, spec.cfg.heldout_split) == ("spider_evolve_s30", "spider_heldout_s40")
        assert spec.source is None and not spec.pooled_sim_from and spec.group in study2.EST_RMB
    # policy and search request settings are those of the controls (thinking off for both roles)
    assert study2.POLICY_PARAMS == rev3.POLICY_PARAMS
    assert study2.SEARCH_EXTRA == {"enable_thinking": False}
    from lamarck.engine.loop import SearchLLM

    assert SearchLLM.max_tokens == study2.SEARCH_MAX_TOKENS == 8192


def test_separate_ledger_and_cap() -> None:
    f1, c1 = plan_money("rev3")
    f2, c2 = plan_money("study2")
    assert f1 != f2 and f2.parent.parent.name == "study2"
    assert (c1, c2) == (9.50, 9.00)


def test_run_bound_covers_high_estimate_and_gate_uses_it() -> None:
    est = study2.estimate()
    sc = est["scenarios"]
    assert sc["low"]["run"] < sc["central"]["run"] < sc["high"]["run"] <= study2.RUN_BOUND_RMB
    # five runs at the bound do not fit under the cap: the gate decides how many start
    names = list(study2.build_plan())
    e = {n: study2.RUN_BOUND_RMB for n in names}
    spend = {"cap_rmb": 9.0, "spent_rmb": 0.001, "in_flight_rmb": 0.0, "by": {}}
    assert money_gate(names, names, [], e, spend) == set(names[:4])
    # after four runs that cost 1.70 each, the fifth fits (9.0 - 6.8 >= 2.1); at 1.75 each it does not
    done = {"cap_rmb": 9.0, "spent_rmb": 6.8, "in_flight_rmb": 0.0, "by": {}}
    assert money_gate(names, names[4:], [], e, done) == {names[4]}
    done["spent_rmb"] = 7.0
    assert money_gate(names, names[4:], [], e, done) == set()


# --------------------------------------------------------------- routing
def test_routing_real_models_go_upstream_fakes_stay_local(tmp_path: Path) -> None:
    sim = SimulatedDashScope("toy")
    router = Router(upstream=sim, fake=FakeProvider({"fake-echo": lambda b: "ok"}))
    msg = [{"role": "user", "content": "kind=x item=abc"}]
    assert router.complete({"model": "fake-echo", "messages": msg})["id"] == "fake"
    assert sim.calls == 0
    out = router.complete({"model": "qwen3.8-flash", "messages": msg, "max_tokens": 50})
    assert out["id"] == "sim" and sim.calls == 1
    with pytest.raises(UpstreamError):
        router.complete({"model": "deepseek-v4-pro", "messages": msg})
    with pytest.raises(UpstreamError):
        Router(upstream=None, fake=None).complete({"model": "qwen4-max", "messages": msg})


def test_simulated_usage_is_dashscope_shaped_and_within_reservation_bounds(tmp_path: Path) -> None:
    sim = SimulatedDashScope("toy")
    body = {"model": "qwen4-max", "messages": [{"role": "user", "content": "x" * 5000}], "max_tokens": 64}
    u = sim.complete(body)["usage"]
    assert set(u) >= {"prompt_tokens", "completion_tokens", "prompt_tokens_details"}
    assert u["prompt_tokens"] <= 5000 + 100 and u["completion_tokens"] <= 64
    assert 0 < u["prompt_tokens_details"]["cached_tokens"] <= u["prompt_tokens"]


def test_simulated_gateway_prices_real_models_and_refuses_key_with_simulation(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        build_gateway(
            tmp_path / "a", tmp_path / "k.env", False, spend_file=tmp_path / "s.json", simulate="toy"
        )
    with pytest.raises(SystemExit):
        build_gateway(tmp_path / "b", None, False, simulate="toy")  # no spend ledger
    gw = build_gateway(
        tmp_path / "c", None, True, spend_file=tmp_path / "c" / "s.json", spend_cap=1.0, simulate="toy"
    )
    tok = gw.create_grant(Grant("s2-src-1", ["policy", "proposer"], ["*"]))
    st, out = gw.chat(
        tok, "proposer", {"model": "qwen4-max", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert st == 200
    u = out["usage"]
    assert gw.spend is not None
    assert gw.spend.spent == pytest.approx(price_of("qwen4-max", u["prompt_tokens"], u["completion_tokens"]))
    st, _ = gw.chat(
        tok, "policy", {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "x"}]}
    )
    assert st == 429  # unknown model: no price, refused before any upstream call (fail closed)
    assert gw.spend.snapshot()["calls"] == 1
    gw.spend.close()


# ----------------------------------------------------------- seed cache
def test_seed_cache_copies_only_matching_seed_entries(tmp_path: Path) -> None:
    from lamarck.cli import harness_tree
    from lamarck.domains.text2sql.domain import Text2SQLDomain

    tree = harness_tree(Text2SQLDomain.seed_harness, tmp_path / "w")
    prev = tmp_path / "prev"
    base = prev / "cache" / "evals" / "text2sql"
    wanted = {
        f"spider_evolve_s30/qwen3.8-flash/{tree}__r0.json",
        f"spider_evolve_s30/qwen3.8-flash/{tree}__r2.json",
        f"spider_heldout_s40/qwen3.8-flash/{tree}__r0.json",
        f"spider_evolve/qwen3.8-flash/{tree}__r1.json",
    }
    unwanted = {
        "spider_evolve_s30/qwen3.8-flash/0123456789ab__r0.json",  # an evolved harness
        f"bird_evolve_s30/qwen3.8-flash/{tree}__r0.json",  # another split
        f"spider_evolve_s30/deepseek-v4.1-flash/{tree}__r0.json",  # another policy model
    }
    for rel in wanted | unwanted:
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text(json.dumps({"score": 0.8, "rel": rel}))
    root = tmp_path / "study2"
    existing = root / "cache" / "evals" / "text2sql" / f"spider_evolve_s30/qwen3.8-flash/{tree}__r2.json"
    existing.parent.mkdir(parents=True)
    existing.write_text('{"score": 0.5}')
    before = {p: p.read_bytes() for p in prev.rglob("*.json")}
    prov = seed_cache(root, prev, "study2")
    got = {c["file"].removeprefix("cache/evals/text2sql/") for c in prov["copied"]}
    assert got == wanted - {f"spider_evolve_s30/qwen3.8-flash/{tree}__r2.json"}
    assert json.loads(existing.read_text()) == {"score": 0.5}  # never overwritten
    assert before == {p: p.read_bytes() for p in prev.rglob("*.json")}  # source untouched
    assert (root / "cache" / "seed_cache_provenance.jsonl").exists()


# ------------------------------------------------------ end to end (toy)
def test_study2_settings_end_to_end_on_toy_with_simulated_upstream(tmp_path: Path) -> None:
    """Study 2's run settings (search model qwen4-max, request params, run budgets) through a gateway whose
    simulated upstream answers the real model names: calls are priced at the real prices and the cap holds."""
    from lamarck.gateway.client import create_grant
    from lamarck.gateway.server import start_in_thread
    from lamarck.sandbox.backends import LocalBackend

    spend = SpendLedger(tmp_path / "budget" / "spend.json", cap_rmb=study2.SPEND_CAP_RMB)
    gw = Gateway(
        Router(upstream=SimulatedDashScope("toy"), fake=None),
        Ledger(tmp_path / "gw" / "ledger.json"),
        tmp_path / "gw",
        GatewayConfig(default_rate=1000.0, backoff_base_s=0.01),
        spend=spend,
    )
    _srv, url = start_in_thread(gw)
    stack = Stack(url, gw.admin_secret, LocalBackend(url), tmp_path)

    def grant(run_id: str, roles: list[str], budget: dict[str, int] | None = None) -> str:
        b = budget or {r: study2.RUN_BUDGET[r] for r in roles if r in study2.RUN_BUDGET}
        return create_grant(url, gw.admin_secret, run_id, roles, budget=b)

    stack.grant = grant  # type: ignore[method-assign]
    spec = study2.build_plan()["s2-src-1"]
    cfg = RunConfig(
        **{**asdict(spec.cfg), "domain": "toy", "evolve_split": "evolve", "heldout_split": "heldout"}
    )
    run = stack.make_run(
        cfg, policy_params=study2.POLICY_PARAMS[cfg.policy_model], search_extra=study2.SEARCH_EXTRA
    )
    s = run.run()
    assert run.state()["status"] == "done" and s["heldout_end"] is not None
    snap = spend.snapshot()
    by: dict[str, Any] = snap["by"]
    assert by["model:qwen4-max"]["calls"] > 0 and by["model:qwen3.8-flash"]["calls"] > 0
    assert by["run:s2-src-1"]["rmb"] == pytest.approx(snap["spent_rmb"]) and 0 < snap["spent_rmb"] <= 9.0
    assert set(by) >= {"role:proposer", "role:selector", "role:policy"}
    spend.close()


def test_analysis_primary_and_secondary() -> None:
    from lamarck.experiments.stats import one_sample_summary, two_sample_summary
    from lamarck.experiments.study2_report import analyse

    def row(h: float | None, e: float, acc: int) -> dict[str, Any]:
        return {"heldout_change": h, "evolve_change": e, "accepted": acc, "rmb": 1.5}

    study = [row(5.0, 10.0, 2), row(2.5, 6.7, 1), row(7.5, 10.0, 2), row(None, 3.3, 0)]  # 4th: stopped
    controls = [row(x, 6.7, 1) for x in (-2.5, -2.5, -2.5, 2.5, 0.0, 0.0, 5.0)]
    a = analyse(study, controls)
    assert a["n"] == 3 and a["n_controls"] == 7
    p = a["primary"]
    assert p["mean"] == pytest.approx(5.0) and p["control_mean"] == pytest.approx(0.0)
    assert p["one_sample_vs_0"] == one_sample_summary([5.0, 2.5, 7.5])
    assert p["two_sample_vs_controls"] == two_sample_summary([5.0, 2.5, 7.5], p["control_values"])
    assert a["secondary"]["accepted"] == [2, 1, 2, 0]
    assert analyse(study[:1], controls)["primary"]["one_sample_vs_0"] is None  # n < 2: no test
