"""The budget ledger: hard caps on model calls, per pool, per run and per role.

Every upstream attempt first *reserves* a call (raising BudgetExceeded once a cap
would be crossed) and then *records* its token usage. Reserving before the
network call means caps can never be overshot, even with many concurrent
requests. Failed attempts count: they cost a request upstream.

Pools (global caps from the project brief):
  policy   the frozen task-solving policy                      <= 80,000 calls
  search   proposer + selector + critic (harness optimizer)     <= 3,000 calls

State is one JSON file, rewritten atomically after every change, so a restarted
gateway continues from the same counts.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POOLS: dict[str, tuple[str, ...]] = {"policy": ("policy",), "search": ("proposer", "selector", "critic")}
DEFAULT_CAPS = {"policy": 80_000, "search": 3_000}


class BudgetExceeded(RuntimeError):
    """A call would exceed a global, per-run or per-role cap."""


def pool_of(role: str) -> str:
    for pool, roles in POOLS.items():
        if role in roles:
            return pool
    raise ValueError(f"unknown role {role!r}")


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


class Ledger:
    def __init__(self, path: Path | None, caps: dict[str, int] | None = None) -> None:
        self.path = path
        self.caps = dict(caps or DEFAULT_CAPS)
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {"pools": {}, "runs": {}, "models": {}}
        if path is not None and path.exists() and path.stat().st_size:
            self.state = json.loads(path.read_text())
        for key in ("pools", "runs", "models"):
            self.state.setdefault(key, {})

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _bucket(d: dict[str, Any], key: str) -> dict[str, int]:
        b: dict[str, int] = d.setdefault(key, {})
        for f in ("calls", "failed", "prompt_tokens", "completion_tokens"):
            b.setdefault(f, 0)
        return b

    def _flush(self) -> None:
        if self.path is None:
            return
        self.state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True))
        tmp.replace(self.path)

    # ----------------------------------------------------------------- API
    def reserve(self, run_id: str, role: str, model: str, run_budget: dict[str, int] | None = None) -> None:
        """Count one upstream attempt, or raise BudgetExceeded without counting it."""
        pool = pool_of(role)
        with self._lock:
            used = self._bucket(self.state["pools"], pool)["calls"]
            if used >= self.caps[pool]:
                raise BudgetExceeded(f"global {pool} cap reached: {used}/{self.caps[pool]} calls")
            run = self.state["runs"].setdefault(run_id, {})
            role_b = self._bucket(run, role)
            limit = (run_budget or {}).get(role)
            if limit is not None and role_b["calls"] >= limit:
                raise BudgetExceeded(f"run {run_id} {role} budget reached: {role_b['calls']}/{limit} calls")
            self._bucket(self.state["pools"], pool)["calls"] += 1
            role_b["calls"] += 1
            self._bucket(self.state["models"], f"{role}|{model}")["calls"] += 1
            self._flush()

    def record(self, run_id: str, role: str, model: str, usage: Usage | None, failed: bool = False) -> None:
        pool = pool_of(role)
        u = usage or Usage()
        with self._lock:
            for b in (
                self._bucket(self.state["pools"], pool),
                self._bucket(self.state["runs"].setdefault(run_id, {}), role),
                self._bucket(self.state["models"], f"{role}|{model}"),
            ):
                b["prompt_tokens"] += u.prompt_tokens
                b["completion_tokens"] += u.completion_tokens
                b["failed"] += int(failed)
            self._flush()

    def used(self, pool: str) -> int:
        with self._lock:
            return int(self._bucket(self.state["pools"], pool)["calls"])

    def run_used(self, run_id: str, role: str) -> int:
        with self._lock:
            return int(self.state["runs"].get(run_id, {}).get(role, {}).get("calls", 0))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snap: dict[str, Any] = json.loads(json.dumps(self.state))
        snap["caps"] = dict(self.caps)
        return snap
