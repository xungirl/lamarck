"""Helpers shared by the in-sandbox domain runtimes.

`PolicyEnv` is the only door from harness code to the policy model: it caps
the number of policy calls per task, fixes the model and sampling settings
(the harness cannot switch to a different model or raise the temperature),
and counts tokens for the cost rule.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from typing import Any

from ..gateway.client import ChatResult, GatewayClient, GatewayError


class CallBudgetExhausted(RuntimeError):
    pass


class PolicyEnv:
    def __init__(self, client: GatewayClient, model: str, max_calls: int, params: dict[str, Any]) -> None:
        self._client = client
        self._model = model
        self._params = dict(params)
        self.max_calls = max_calls
        self.calls = 0
        self.tokens = 0
        self.budget_hit = False  # the gateway refused because a global/run cap was reached

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        if self.calls >= self.max_calls:
            raise CallBudgetExhausted(f"policy call limit of {self.max_calls} per task reached")
        self.calls += 1
        params = dict(self._params)
        if tools:
            params["tools"] = tools
        if max_tokens is not None:
            params["max_tokens"] = min(int(max_tokens), int(self._params.get("max_tokens", 2048)))
        try:
            res = self._client.chat("policy", self._model, messages, **params)
        except GatewayError as exc:
            if exc.budget_exceeded:
                self.budget_hit = True
            raise
        self.tokens += res.tokens
        return res


_import_lock = threading.Lock()


def load_harness_module(harness_dir: Path, filename: str) -> ModuleType:
    """Import harness_dir/filename as a fresh module (package-relative imports allowed)."""
    with _import_lock:
        pkg = "harness_under_test"
        if pkg not in sys.modules:
            spec_pkg = importlib.util.spec_from_file_location(
                pkg, harness_dir / "__init__.py", submodule_search_locations=[str(harness_dir)]
            )
            if spec_pkg is None or spec_pkg.loader is None:
                raise ImportError("cannot load harness package")
            pkg_mod = importlib.util.module_from_spec(spec_pkg)
            sys.modules[pkg] = pkg_mod
            if (harness_dir / "__init__.py").exists():
                spec_pkg.loader.exec_module(pkg_mod)
        name = f"{pkg}.{Path(filename).stem}"
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, harness_dir / filename)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {filename}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod


def read_text(harness_dir: Path, name: str, default: str = "") -> str:
    p = harness_dir / name
    return p.read_text() if p.exists() else default


def read_config(harness_dir: Path) -> dict[str, Any]:
    p = harness_dir / "config.json"
    if not p.exists():
        return {}
    try:
        cfg = json.loads(p.read_text())
        return cfg if isinstance(cfg, dict) else {}
    except ValueError:
        return {}


def run_tasks(
    tasks: list[dict[str, Any]],
    solve_one: Callable[[dict[str, Any]], dict[str, Any]],
    out_dir: Path,
    workers: int,
) -> None:
    """Run solve_one on every task in a thread pool, appending one JSON line per task.

    A task that raises is recorded with its error (and scores 0 on the host); it is
    never silently dropped from the denominator.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    # One handle for the whole job: on some bind mounts (virtiofs) a file created by the sandbox
    # user is not re-openable by it, so the file is opened exactly once.
    with open(out_dir / "results.jsonl", "a") as sink:

        def wrapped(task: dict[str, Any]) -> None:
            try:
                rec = solve_one(task)
            except Exception as exc:  # untrusted harness code
                rec = {
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "traceback": traceback.format_exc()[-1500:],
                }
            rec["task_id"] = task["id"]
            with lock:
                sink.write(json.dumps(rec, default=str) + "\n")
                sink.flush()

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(wrapped, tasks))
