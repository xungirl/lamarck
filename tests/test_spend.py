"""RMB spend ledger: pricing from returned usage, cached tokens, hard cap without overshoot,
persistence across restarts, single owner, and the gateway integration."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from lamarck.gateway.ledger import BudgetExceeded, Ledger
from lamarck.gateway.providers import FakeProvider, Router, UpstreamError
from lamarck.gateway.server import Gateway, GatewayConfig, Grant
from lamarck.gateway.spend import LedgerLocked, SpendLedger, price_of, worst_case

MODEL = "qwen3.8-flash"


def body(text: str = "hello", max_tokens: int = 100) -> dict[str, Any]:
    return {"model": MODEL, "messages": [{"role": "user", "content": text}], "max_tokens": max_tokens}


class Upstream:
    """Answers like DashScope: fixed usage, optionally with cached prompt tokens."""

    name = "up"

    def __init__(self, prompt: int = 1000, completion: int = 100, cached: int | None = None) -> None:
        self.prompt, self.completion, self.cached = prompt, completion, cached
        self.calls = 0

    def complete(self, b: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        usage: dict[str, Any] = {"prompt_tokens": self.prompt, "completion_tokens": self.completion}
        if self.cached is not None:
            usage["prompt_tokens_details"] = {"cached_tokens": self.cached}
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": usage}


def test_price_formula_and_cached_tokens() -> None:
    assert price_of(MODEL, 1_000_000, 0) == pytest.approx(0.80)
    assert price_of(MODEL, 0, 1_000_000) == pytest.approx(2.70)
    # 400k of 1M prompt tokens served from cache: 0.6 * 0.8 + 0.4 * 0.1
    assert price_of(MODEL, 1_000_000, 0, cached_tokens=400_000) == pytest.approx(0.52)
    assert price_of("fake-policy", 10**9, 10**9) == 0.0


def test_worst_case_bounds_actual_price() -> None:
    b = body("x" * 4000, max_tokens=500)
    # real usage never exceeds 1 token per character and max_tokens output
    assert worst_case(MODEL, b) >= price_of(MODEL, 4000, 500)


def test_hard_cap_refuses_before_crossing(tmp_path: Path) -> None:
    led = SpendLedger(tmp_path / "s.json", cap_rmb=0.01)
    n = 0
    with pytest.raises(BudgetExceeded):
        while True:
            r = led.reserve(MODEL, body(max_tokens=1000))
            led.settle(r, "run", "policy", {"prompt_tokens": 2000, "completion_tokens": 1000})
            n += 1
    assert n > 0
    assert led.spent <= 0.01  # never overshoots


def test_unknown_model_fails_closed(tmp_path: Path) -> None:
    led = SpendLedger(tmp_path / "s.json")
    for model in ("deepseek-v4.1-flash", "deepseek-v4-pro", "qwen4-max-latest"):
        with pytest.raises(BudgetExceeded, match="no RMB price"):
            led.reserve(model, {**body(), "model": model})
    assert led.snapshot()["in_flight_rmb"] == 0.0


def test_qwen4_max_price_cached_input_not_discounted(tmp_path: Path) -> None:
    m = "qwen4-max"
    assert price_of(m, 1_000_000, 0) == pytest.approx(10.0)
    assert price_of(m, 0, 1_000_000) == pytest.approx(30.0)
    # no confirmed cache discount: cached prompt tokens cost the full input price
    assert price_of(m, 1_000_000, 0, cached_tokens=900_000) == pytest.approx(10.0)
    led = SpendLedger(tmp_path / "s.json", cap_rmb=1.0)
    r = led.reserve(m, {**body(max_tokens=8192), "model": m})
    assert r.amount >= 8192 * 30 / 1e6  # worst case covers a full-length answer
    usage = {
        "prompt_tokens": 4932,
        "completion_tokens": 3698,
        "prompt_tokens_details": {"cached_tokens": 4096},
    }
    assert led.settle(r, "s2-src-1", "proposer", usage) == pytest.approx((4932 * 10 + 3698 * 30) / 1e6)
    assert led.snapshot()["by"]["model:qwen4-max"]["cached_tokens"] == 4096


def test_persists_across_restarts_and_single_owner(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    led = SpendLedger(path, cap_rmb=1.0)
    r = led.reserve(MODEL, body())
    led.settle(r, "sql-cold", "proposer", {"prompt_tokens": 10_000, "completion_tokens": 1_000})
    with pytest.raises(LedgerLocked):
        SpendLedger(path)
    led.close()
    again = SpendLedger(path, cap_rmb=1.0)
    assert again.spent == pytest.approx((10_000 * 0.8 + 1_000 * 2.7) / 1e6)
    snap = again.snapshot()
    assert snap["by"]["run:sql-cold"]["calls"] == 1
    assert snap["by"]["role:proposer"]["rmb"] == pytest.approx(again.spent)
    assert snap["by"]["run_role:sql-cold|proposer"]["completion_tokens"] == 1_000
    again.close()


def test_concurrent_reservations_cannot_overshoot(tmp_path: Path) -> None:
    led = SpendLedger(tmp_path / "s.json", cap_rmb=0.05)
    errors: list[str] = []

    def worker() -> None:
        for _ in range(50):
            try:
                r = led.reserve(MODEL, body(max_tokens=2000))
            except BudgetExceeded:
                errors.append("cap")
                return
            led.settle(r, "r", "policy", {"prompt_tokens": 500, "completion_tokens": 2000})

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors and led.spent <= 0.05


def gateway(tmp_path: Path, upstream: Any, cap: float) -> Gateway:
    return Gateway(
        Router(upstream=upstream, fake=FakeProvider({"fake-echo": lambda b: "ok"})),
        Ledger(tmp_path / "ledger.json"),
        tmp_path,
        GatewayConfig(default_rate=1e6),
        sleep=lambda s: None,
        spend=SpendLedger(tmp_path / "budget" / "spend.json", cap_rmb=cap),
    )


def test_gateway_charges_returned_usage_and_stops_at_cap(tmp_path: Path) -> None:
    up = Upstream(prompt=1000, completion=100, cached=200)
    gw = gateway(tmp_path, up, cap=0.002)
    tok = gw.create_grant(Grant("pilot", ["policy"], ["*"]))
    statuses = [gw.chat(tok, "policy", body(max_tokens=100))[0] for _ in range(10)]
    assert 200 in statuses and statuses[-1] == 429
    per_call = price_of(MODEL, 1000, 100, cached_tokens=200)
    assert gw.spend is not None
    assert gw.spend.spent == pytest.approx(per_call * statuses.count(200))
    assert gw.spend.spent <= 0.002
    status, out = gw.chat(tok, "policy", body())
    assert status == 429 and out["budget_exceeded"] and out["spend_cap"]
    audit = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert audit[0]["rmb"] == pytest.approx(per_call, abs=1e-6)
    assert up.calls == statuses.count(200)  # refused calls never reach the provider


def test_failed_calls_cost_nothing_but_timeouts_charge_worst_case(tmp_path: Path) -> None:
    class Failing:
        name = "f"

        def __init__(self, status: int) -> None:
            self.status = status

        def complete(self, b: dict[str, Any]) -> dict[str, Any]:
            raise UpstreamError("x", self.status, transient=False)

    gw = gateway(tmp_path / "a", Failing(403), cap=1.0)
    tok = gw.create_grant(Grant("r", ["policy"], ["*"]))
    assert gw.chat(tok, "policy", body())[0] == 403
    assert gw.spend is not None and gw.spend.spent == 0.0
    assert gw.spend.snapshot()["failed"] == 1

    gw2 = gateway(tmp_path / "b", Failing(504), cap=1.0)
    tok2 = gw2.create_grant(Grant("r", ["policy"], ["*"]))
    assert gw2.chat(tok2, "policy", body())[0] == 504
    assert gw2.spend is not None
    assert gw2.spend.spent == pytest.approx(worst_case(MODEL, body()))


def test_call_cap_refusal_releases_money_reservation(tmp_path: Path) -> None:
    gw = Gateway(
        Router(upstream=Upstream(), fake=None),
        Ledger(tmp_path / "ledger.json", {"policy": 1, "search": 1}),
        tmp_path,
        GatewayConfig(default_rate=1e6),
        sleep=lambda s: None,
        spend=SpendLedger(tmp_path / "spend.json", cap_rmb=1.0),
    )
    tok = gw.create_grant(Grant("r", ["policy"], ["*"]))
    assert gw.chat(tok, "policy", body())[0] == 200
    assert gw.chat(tok, "policy", body())[0] == 429  # call cap
    assert gw.spend is not None
    assert gw.spend.remaining() == pytest.approx(1.0 - gw.spend.spent)  # nothing left in flight


def test_reasoning_tokens_are_billed_as_output(tmp_path: Path) -> None:
    led = SpendLedger(tmp_path / "s.json", cap_rmb=1.0)
    r = led.reserve(MODEL, body())
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 30,
        "completion_tokens_details": {"reasoning_tokens": 26},
    }
    assert led.settle(r, "probe", "policy", usage) == pytest.approx(price_of(MODEL, 10, 30))
    r = led.reserve(MODEL, body())
    odd = {"prompt_tokens": 10, "completion_tokens": 4, "completion_tokens_details": {"reasoning_tokens": 26}}
    assert led.settle(r, "probe", "policy", odd) == pytest.approx(price_of(MODEL, 10, 30))
    assert led.snapshot()["by"]["role:policy"]["reasoning_tokens"] == 52


def test_cap_holds_across_models_under_concurrency(tmp_path: Path) -> None:
    """Study 2 mixes a cheap policy model and an expensive search model behind one ledger: concurrent calls
    of both through the gateway never cross the cap, and refused calls never reach the provider."""
    # usage within the bounds a real provider respects: prompt tokens <= characters, output <= max_tokens
    up = Upstream(prompt=3000, completion=2000, cached=1024)
    gw = gateway(tmp_path, up, cap=0.5)
    tok_p = gw.create_grant(Grant("s2-src-1", ["policy"], ["qwen3.8-flash"]))
    tok_s = gw.create_grant(Grant("s2-src-1", ["proposer", "selector"], ["qwen4-max"]))
    statuses: list[int] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        for _ in range(40):
            if i % 2:
                st = gw.chat(tok_p, "policy", {**body("p" * 4000, 2048), "model": "qwen3.8-flash"})[0]
            else:
                st = gw.chat(tok_s, "proposer", {**body("s" * 4000, 4096), "model": "qwen4-max"})[0]
            with lock:
                statuses.append(st)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert gw.spend is not None
    snap = gw.spend.snapshot()
    assert 429 in statuses and gw.spend.spent <= 0.5
    assert snap["in_flight_rmb"] == pytest.approx(0.0, abs=1e-12)
    assert up.calls == statuses.count(200) == snap["calls"]
    by = snap["by"]
    assert by["model:qwen4-max"]["calls"] > 0 and by["model:qwen3.8-flash"]["calls"] > 0
    expected = by["model:qwen4-max"]["calls"] * price_of("qwen4-max", 3000, 2000, 1024) + by[
        "model:qwen3.8-flash"
    ]["calls"] * price_of("qwen3.8-flash", 3000, 2000, 1024)
    assert gw.spend.spent == pytest.approx(expected)
