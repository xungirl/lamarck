"""Gateway: auth, budgets (global, per run, per role), retries, rate limiting, audit log, HTTP layer."""

from __future__ import annotations

import json
import threading
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from lamarck.gateway.client import GatewayClient, GatewayError, create_grant, ledger_snapshot
from lamarck.gateway.ledger import BudgetExceeded, Ledger
from lamarck.gateway.providers import FakeProvider, Router, UpstreamError
from lamarck.gateway.ratelimit import TokenBucket
from lamarck.gateway.server import Gateway, GatewayConfig, Grant, start_in_thread

BODY = {"model": "fake-echo", "messages": [{"role": "user", "content": "SECRET-PROMPT-TEXT"}]}


def echo(body: dict[str, Any]) -> str:
    return "ok"


class Flaky:
    """Fails with a transient error `n` times, then answers."""

    name = "flaky"

    def __init__(self, n: int, transient: bool = True) -> None:
        self.n, self.transient, self.calls = n, transient, 0

    def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self.n:
            raise UpstreamError("boom", 503 if self.transient else 400, self.transient)
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }


def make(tmp_path: Path, caps: dict[str, int] | None = None, upstream: Any = None) -> Gateway:
    router = Router(upstream=upstream, fake=FakeProvider({"fake-echo": echo}))
    return Gateway(
        router,
        Ledger(tmp_path / "ledger.json", caps),
        tmp_path,
        GatewayConfig(default_rate=1e6),
        sleep=lambda s: None,
    )


def test_auth_role_and_model_checks(tmp_path: Path) -> None:
    gw = make(tmp_path)
    tok = gw.create_grant(Grant("r1", ["policy"], ["fake-echo"]))
    assert gw.chat("nope", "policy", BODY)[0] == 401
    assert gw.chat(tok, "proposer", BODY)[0] == 403
    assert gw.chat(tok, "policy", {**BODY, "model": "qwen4-max"})[0] == 403
    assert gw.chat(tok, "wizard", BODY)[0] == 400
    assert gw.chat(tok, "policy", BODY)[0] == 200


def test_run_and_global_budgets(tmp_path: Path) -> None:
    gw = make(tmp_path, caps={"policy": 5, "search": 2})
    a = gw.create_grant(Grant("a", ["policy"], ["*"], budget={"policy": 3}))
    b = gw.create_grant(Grant("b", ["policy"], ["*"]))
    statuses = [gw.chat(a, "policy", BODY)[0] for _ in range(4)]
    assert statuses == [200, 200, 200, 429]
    assert [gw.chat(b, "policy", BODY)[0] for _ in range(3)] == [200, 200, 429]  # global cap 5
    status, body = gw.chat(b, "policy", BODY)
    assert status == 429 and body["budget_exceeded"]
    assert gw.ledger.used("policy") == 5


def test_transient_errors_are_retried_and_each_attempt_is_budgeted(tmp_path: Path) -> None:
    up = Flaky(2)
    gw = make(tmp_path, upstream=up)
    tok = gw.create_grant(Grant("r", ["policy"], ["*"]))
    status, _body = gw.chat(tok, "policy", {**BODY, "model": "qwen3.8-flash"})
    assert status == 200 and up.calls == 3
    snap = gw.ledger.snapshot()
    assert snap["pools"]["policy"]["calls"] == 3 and snap["pools"]["policy"]["failed"] == 2
    assert snap["models"]["policy|qwen3.8-flash"]["prompt_tokens"] == 5


def test_permanent_errors_are_not_retried(tmp_path: Path) -> None:
    up = Flaky(5, transient=False)
    gw = make(tmp_path, upstream=up)
    tok = gw.create_grant(Grant("r", ["policy"], ["*"]))
    assert gw.chat(tok, "policy", {**BODY, "model": "x"})[0] == 400 and up.calls == 1


def test_audit_log_has_no_content(tmp_path: Path) -> None:
    gw = make(tmp_path)
    tok = gw.create_grant(Grant("r", ["policy"], ["*"]))
    gw.chat(tok, "policy", BODY)
    text = (tmp_path / "audit.jsonl").read_text()
    rec = json.loads(text.splitlines()[0])
    assert "SECRET-PROMPT-TEXT" not in text and rec["run"] == "r" and len(rec["request_sha256_16"]) == 16


def test_ledger_survives_restart(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "l.json", {"policy": 2, "search": 1})
    led.reserve("r", "policy", "m")
    led.reserve("r", "policy", "m")
    led2 = Ledger(tmp_path / "l.json", {"policy": 2, "search": 1})
    with pytest.raises(BudgetExceeded):
        led2.reserve("r", "policy", "m")


def test_ledger_never_overshoots_under_concurrency(tmp_path: Path) -> None:
    led = Ledger(None, {"policy": 100, "search": 1})
    ok = []

    def worker() -> None:
        for _ in range(50):
            try:
                led.reserve("r", "policy", "m")
                ok.append(1)
            except BudgetExceeded:
                pass

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 100 and led.used("policy") == 100


def test_token_bucket_with_fake_clock() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    b = TokenBucket(rate=2.0, burst=2.0, clock=lambda: now[0], sleep=sleep)
    assert b.acquire() and b.acquire()  # burst
    assert b.try_acquire() == pytest.approx(0.5)
    assert b.acquire()
    assert slept == [pytest.approx(0.5)]
    assert not b.acquire(n=1.0, timeout=0.1)


def test_http_round_trip_and_admin_secret(tmp_path: Path) -> None:
    gw = make(tmp_path)
    server, url = start_in_thread(gw)
    try:
        with pytest.raises(urllib.error.HTTPError):
            create_grant(url, "wrong-secret", "r", ["policy"])
        tok = create_grant(url, gw.admin_secret, "r", ["policy"], budget={"policy": 1})
        client = GatewayClient(url, tok)
        assert client.chat("policy", "fake-echo", BODY["messages"]).content == "ok"
        with pytest.raises(GatewayError) as exc:
            client.chat("policy", "fake-echo", BODY["messages"])
        assert exc.value.status == 429 and exc.value.budget_exceeded
        assert ledger_snapshot(url, gw.admin_secret)["runs"]["r"]["policy"]["calls"] == 1
    finally:
        server.shutdown()
