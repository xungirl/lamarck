"""Money ledger: every upstream call is priced in RMB from the token usage the API returns,
and the gateway refuses calls once a hard cumulative cap would be crossed.

Why a second ledger next to `Ledger` (call counts): the call caps protect against runaway
loops; this one protects the wallet. It is a separate file because it outlives experiment
roots: one spend file covers every gateway this project ever starts (``results/budget/``),
so restarting a gateway, switching experiment roots or crashing never resets the count.

Hard stop without overshoot
  Before a call the gateway *reserves* a worst-case price: every character of the request
  counted as one input token (BPE tokens are never shorter than one character, so this is an
  upper bound) plus ``max_tokens`` output tokens. A call is refused if

      spent + reserved_in_flight + worst_case > cap

  After the call the reservation is replaced by the actual price
  ``(prompt - cached) * p_in + cached * p_cached + completion * p_out``. Cached tokens are
  priced at the cached rate only when the API reports them (``prompt_tokens_details.cached_tokens``).
  Failed upstream calls are recorded at zero cost (the provider does not bill them) but counted.

Fail closed
  A model with no price entry is refused (fake-* test models cost nothing). Prices are RMB per
  1M tokens; see PRICES for the source.

Concurrency
  One gateway process owns a spend file at a time: an exclusive ``flock`` on ``<file>.lock`` is
  held for the ledger's lifetime and a second owner fails at start-up. Within the process a
  threading lock guards the state, and the file is rewritten atomically after every change.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from .ledger import BudgetExceeded

# RMB per 1M tokens: (input, output, cached input).
# qwen3.8-flash: Alibaba Cloud Model Studio, China (Beijing) region, as given in the project brief
# (2026-11-19) and cross-checked against alibabacloud.com/help/zh/model-studio/model-pricing
# (page "updated Nov 17, 2026", retrieved 2026-11-19), which displays $0.113 / $0.382 per 1M
# input / output tokens for qwen3.8-flash (= RMB 0.80 / 2.70 at ~7.08 RMB/USD) and states that
# context-cache hits are discounted (RMB 0.10 per 1M from the brief).
# qwen3.7-flash: the same page lists it below qwen3.8-flash ($0.028 / $0.110 for <= 32K input);
# we deliberately charge it at the qwen3.8-flash rate, an upper bound, so the cap stays safe.
# qwen4-max (Study 2 search model, added 2026-11-19 after the first study had finished): China (Beijing)
# region, input RMB 10 / output RMB 30 per 1M tokens, help.aliyun.com/zh/model-studio/model-pricing as
# given in the Study 2 brief (retrieved 2026-11-19). Output includes thinking tokens. No cached-input price
# was confirmed, so cached input is billed at the full input price (conservative); the 1M-token free quota
# for new models is ignored (assumed used up).
PRICES: dict[str, tuple[float, float, float]] = {
    "qwen3.8-flash": (0.80, 2.70, 0.10),
    "qwen3.7-flash": (0.80, 2.70, 0.10),
    "qwen4-max": (10.0, 30.0, 10.0),
}
DEFAULT_CAP_RMB = 9.50
DEFAULT_MAX_TOKENS = 16_384  # assumed output cap when a request does not set max_tokens


class LedgerLocked(RuntimeError):
    """Another process already owns this spend file."""


@dataclass
class Reservation:
    model: str
    amount: float


def price_of(model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float:
    if model.startswith("fake-"):
        return 0.0
    p_in, p_out, p_cached = PRICES[model]
    cached = max(0, min(cached_tokens, prompt_tokens))
    return ((prompt_tokens - cached) * p_in + cached * p_cached + completion_tokens * p_out) / 1e6


def worst_case(model: str, body: dict[str, Any]) -> float:
    """Upper bound on the price of one request (see module docstring)."""
    if model.startswith("fake-"):
        return 0.0
    chars = len(json.dumps(body.get("messages", []), ensure_ascii=False))
    chars += len(json.dumps(body.get("tools", []), ensure_ascii=False)) if body.get("tools") else 0
    max_out = int(body.get("max_tokens") or DEFAULT_MAX_TOKENS)
    return price_of(model, chars, max_out)


def billed_output_tokens(usage: dict[str, Any]) -> int:
    """Output tokens to bill. Thinking ("reasoning") tokens are billed at the output price.
    DashScope reports them inside completion_tokens (completion_tokens_details.reasoning_tokens is
    a breakdown); if a response ever reported more reasoning tokens than completion tokens, they
    cannot have been included, so they are added."""
    completion = int(usage.get("completion_tokens") or 0)
    details = usage.get("completion_tokens_details") or {}
    reasoning = int(details.get("reasoning_tokens") or 0) if isinstance(details, dict) else 0
    return completion if completion >= reasoning else completion + reasoning


def reasoning_tokens_of(usage: dict[str, Any]) -> int:
    details = usage.get("completion_tokens_details") or {}
    return int(details.get("reasoning_tokens") or 0) if isinstance(details, dict) else 0


def cached_tokens_of(usage: dict[str, Any]) -> int:
    details = usage.get("prompt_tokens_details") or {}
    if isinstance(details, dict):
        return int(details.get("cached_tokens") or 0)
    return 0


class SpendLedger:
    def __init__(self, path: Path | None, cap_rmb: float = DEFAULT_CAP_RMB, exclusive: bool = True) -> None:
        self.path = path
        self.cap = float(cap_rmb)
        self._lock = threading.Lock()
        self._inflight = 0.0
        self._lockfile: IO[str] | None = None
        self.state: dict[str, Any] = {"spent_rmb": 0.0, "calls": 0, "failed": 0, "by": {}}
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if exclusive:
                self._lockfile = open(path.with_name(path.name + ".lock"), "w")  # noqa: SIM115
                try:
                    fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    self._lockfile.close()
                    raise LedgerLocked(f"{path} is owned by another process") from exc
                self._lockfile.write(f"{os.getpid()}\n")
                self._lockfile.flush()
            if path.exists() and path.stat().st_size:
                self.state = json.loads(path.read_text())
        self.state.setdefault("by", {})
        self.state["cap_rmb"] = self.cap
        self.state["prices_rmb_per_million"] = {m: list(p) for m, p in PRICES.items()}

    def close(self) -> None:
        if self._lockfile is not None:
            fcntl.flock(self._lockfile, fcntl.LOCK_UN)
            self._lockfile.close()
            self._lockfile = None

    # ------------------------------------------------------------- helpers
    def _flush(self) -> None:
        if self.path is None:
            return
        self.state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True))
        tmp.replace(self.path)

    def _bucket(self, key: str) -> dict[str, Any]:
        b: dict[str, Any] = self.state["by"].setdefault(key, {})
        for f in (
            "calls",
            "failed",
            "prompt_tokens",
            "cached_tokens",
            "completion_tokens",
            "reasoning_tokens",
        ):
            b.setdefault(f, 0)
        b.setdefault("rmb", 0.0)
        return b

    # ----------------------------------------------------------------- API
    @property
    def spent(self) -> float:
        with self._lock:
            return float(self.state["spent_rmb"])

    def remaining(self) -> float:
        with self._lock:
            return self.cap - float(self.state["spent_rmb"]) - self._inflight

    def reserve(self, model: str, body: dict[str, Any]) -> Reservation:
        """Reserve the worst-case price of one call, or raise BudgetExceeded."""
        if not model.startswith("fake-") and model not in PRICES:
            raise BudgetExceeded(f"no RMB price configured for model {model!r}; refusing (fail closed)")
        amount = worst_case(model, body)
        with self._lock:
            spent = float(self.state["spent_rmb"])
            if spent + self._inflight + amount > self.cap:
                raise BudgetExceeded(
                    f"RMB spend cap: spent {spent:.4f} + in flight {self._inflight:.4f} + "
                    f"this call <= {amount:.4f} would exceed {self.cap:.2f}"
                )
            self._inflight += amount
        return Reservation(model, amount)

    def settle(
        self,
        res: Reservation,
        run_id: str,
        role: str,
        usage: dict[str, Any] | None,
        failed: bool = False,
        unknown: bool = False,
    ) -> float:
        """Replace a reservation by the actual price. Returns the price.

        failed   the provider answered with an error status: not billed, price 0
        unknown  the request may have reached the provider but no answer came back (timeout,
                 dropped connection): we cannot know the usage, so the worst case is charged
        """
        u = usage or {}
        prompt = int(u.get("prompt_tokens") or 0)
        completion = billed_output_tokens(u)
        reasoning = reasoning_tokens_of(u)
        cached = cached_tokens_of(u)
        if unknown:
            cost = res.amount
        elif failed:
            cost = 0.0
        else:
            cost = price_of(res.model, prompt, completion, cached)
        if not math.isfinite(cost) or cost < 0:
            cost = res.amount
        with self._lock:
            self._inflight = max(0.0, self._inflight - res.amount)
            self.state["spent_rmb"] = float(self.state["spent_rmb"]) + cost
            self.state["calls"] = int(self.state.get("calls", 0)) + 1
            self.state["failed"] = int(self.state.get("failed", 0)) + int(failed or unknown)
            for key in (f"run:{run_id}", f"role:{role}", f"model:{res.model}", f"run_role:{run_id}|{role}"):
                b = self._bucket(key)
                b["calls"] += 1
                b["failed"] += int(failed or unknown)
                b["prompt_tokens"] += prompt
                b["cached_tokens"] += cached
                b["completion_tokens"] += completion
                b["reasoning_tokens"] += reasoning
                b["rmb"] = float(b["rmb"]) + cost
            self._flush()
        return cost

    def release(self, res: Reservation) -> None:
        """Drop a reservation for a call that was never sent."""
        with self._lock:
            self._inflight = max(0.0, self._inflight - res.amount)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snap: dict[str, Any] = json.loads(json.dumps(self.state))
            snap["in_flight_rmb"] = self._inflight
        return snap
