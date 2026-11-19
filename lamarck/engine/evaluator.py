"""Evaluate(H, split, model): run a harness in a sandbox and score it on the host.

Results are cached by (domain, split, policy model, harness tree hash, rollout),
so an unchanged harness is never evaluated twice under the same key: the
starting harness that several continuation arms share is evaluated once, and a
round that keeps its incumbent spends no held-out calls.

Only the public task fields are written into the job directory; gold answers and
expectations stay in host memory and are used by Domain.score after the sandbox exits.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domains.base import Domain
from ..sandbox.backends import Backend, SandboxJob
from ..types import EvalResult

POLICY_PARAMS_DEFAULT: dict[str, Any] = {"temperature": 0.0, "max_tokens": 2048, "seed": 1234}


class BudgetStop(RuntimeError):
    """The gateway refused a call because a hard cap was reached; stop the run cleanly."""


@dataclass
class Evaluator:
    domain: Domain
    backend: Backend
    cache_dir: Path
    work_dir: Path
    token: str  # policy grant
    task_parallel: int = 8
    policy_params: dict[str, Any] | None = None
    keep_artifacts: bool = False

    def __post_init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def cache_path(self, split: str, model: str, tree: str, rollout: int) -> Path:
        safe_model = model.replace("/", "_")
        return self.cache_dir / self.domain.name / split / safe_model / f"{tree}__r{rollout}.json"

    def cached(self, split: str, model: str, tree: str, rollout: int) -> EvalResult | None:
        p = self.cache_path(split, model, tree, rollout)
        return EvalResult.load(p) if p.exists() else None

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def evaluate(self, harness_dir: Path, tree: str, split: str, model: str, rollout: int = 0) -> EvalResult:
        path = self.cache_path(split, model, tree, rollout)
        with self._lock(str(path)):
            if path.exists():
                return EvalResult.load(path)
            result = self._run(harness_dir, tree, split, model, rollout)
            result.save(path)
            return result

    def _run(self, harness_dir: Path, tree: str, split: str, model: str, rollout: int) -> EvalResult:
        tasks = self.domain.tasks(split)
        job_id = f"{self.domain.name}-{split}-{model.replace('/', '_')}-{tree}-r{rollout}-{int(time.time() * 1000) % 10**8}"
        base = self.work_dir / job_id
        job_dir, out_dir = base / "job", base / "out"
        job_dir.mkdir(parents=True, exist_ok=True)
        params = dict(POLICY_PARAMS_DEFAULT if self.policy_params is None else self.policy_params)
        params["seed"] = int(params.get("seed", 0)) + rollout
        job = {
            "runtime": self.domain.runtime,
            "model": model,
            "split": split,
            "max_calls": self.domain.max_calls_per_task,
            "task_parallel": self.task_parallel,
            "policy_params": params,
            **self.domain.job_extra(split),
        }
        (job_dir / "job.json").write_text(json.dumps(job, indent=1))
        (job_dir / "tasks.jsonl").write_text("".join(json.dumps(t.public) + "\n" for t in tasks))
        sj = SandboxJob(
            job_id=job_id,
            harness_dir=harness_dir,
            job_dir=job_dir,
            out_dir=out_dir,
            data_mounts=self.domain.data_mounts(split),
            token=self.token,
        )
        t0 = time.time()
        jr = self.backend.run(sj)
        raw: dict[str, dict[str, Any]] = {}
        results_file = out_dir / "results.jsonl"
        if results_file.exists():
            for line in results_file.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    raw[str(rec.get("task_id"))] = rec
        if any("gateway 429: budget" in str(r.get("error", "")) for r in raw.values()):
            raise BudgetStop(f"policy budget exhausted during {job_id}")
        if not jr.ok and not raw:
            status = out_dir / "status.json"
            detail = status.read_text()[:800] if status.exists() else jr.log_tail[-800:]
            for t in tasks:
                raw[t.id] = {"error": f"sandbox job failed: {detail}"}
        outcomes = self.domain.score(split, tasks, raw, out_dir)
        result = EvalResult(tree, split, model, rollout, outcomes, wall_s=time.time() - t0)
        if not self.keep_artifacts:
            shutil.rmtree(base, ignore_errors=True)
        return result
