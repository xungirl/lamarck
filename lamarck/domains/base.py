"""The Domain interface: everything the engine needs from a benchmark environment.

The engine never runs an agent, reads a database or grades an answer itself.
A domain supplies:

  tasks(split)        task records; `public` is all the harness may see, `private`
                      (gold SQL, expected state, ...) stays on the host and is never mounted
  data_mounts(split)  read-only host directories the sandbox needs for that split only
  runtime             module executed INSIDE the sandbox (loads the harness, calls the policy)
  score(...)          host-side grading of the sandbox's raw outputs -> TaskOutcome per task
  guards(...)         non-compensatory checks (e.g. unauthorized writes must not increase)
  critic_patterns     deterministic leakage denylist for the screen
  brief / contract    texts that make the proposer and selector speak the domain's language
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..types import EvalResult, TaskOutcome


@dataclass
class Task:
    id: str
    public: dict[str, Any]  # visible to the harness inside the sandbox
    private: dict[str, Any] = field(default_factory=dict)  # host-only (gold answers, expectations)


class Domain:
    name: str = "base"
    seed_harness: Path
    runtime: str = ""  # dotted module path of the in-sandbox runtime
    editable: tuple[str, ...] = ("*.md", "*.json", "*.py")  # files the proposer may create/change
    max_file_chars: int = 20_000
    brief: str = ""
    contract: str = ""
    max_calls_per_task: int = 4

    # ---------------------------------------------------------------- tasks
    def splits(self) -> list[str]:
        raise NotImplementedError

    def tasks(self, split: str) -> list[Task]:
        raise NotImplementedError

    def data_mounts(self, split: str) -> dict[str, Path]:
        """{name: host directory}; mounted read-only at /data/<name> in the sandbox."""
        return {}

    def parent_split(self, split: str) -> str | None:
        """The split a sub-split was sampled from (budget-revision sub-splits), else None."""
        from .subsplits import parent_of

        return parent_of(self.name, split)

    def job_extra(self, split: str) -> dict[str, Any]:
        """Extra non-secret settings for the runtime (e.g. which store a task uses)."""
        return {}

    # -------------------------------------------------------------- scoring
    def score(
        self, split: str, tasks: list[Task], raw: dict[str, dict[str, Any]], out_dir: Path
    ) -> dict[str, TaskOutcome]:
        raise NotImplementedError

    def guards(self, incumbent: EvalResult, candidate: EvalResult) -> list[str]:
        return []

    def critic_patterns(self) -> list[tuple[str, str]]:
        """[(regex, reason)] rejected in a candidate diff before any evaluation is spent."""
        return []

    # --------------------------------------------------------------- helpers
    def is_editable(self, rel: str) -> bool:
        if rel.startswith("/") or ".." in Path(rel).parts or rel.startswith("."):
            return False
        return any(fnmatch.fnmatch(Path(rel).name, pat) for pat in self.editable)

    def describe_task(self, task: Task) -> str:
        """One line describing a task for the proposer (public fields only)."""
        return " ".join(f"{k}={v}" for k, v in task.public.items() if k != "id")[:300]
