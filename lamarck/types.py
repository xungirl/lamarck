"""Plain data types shared by the engine, the domains and the sandbox layer.

Scores are fractions in [0, 1] internally (1.0 = every task solved) and are
shown as 0-100 points only in reports. Cost is the mean number of policy tokens
(prompt + completion) per task, the quantity RRSI's cost rule is defined on.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TaskOutcome:
    """The scored result of one task in one evaluation."""

    task_id: str
    reward: float  # in [0, 1]; a crashed or missing task is 0, never dropped
    tokens: int = 0  # policy prompt + completion tokens spent on this task
    calls: int = 0  # policy calls spent on this task
    error: str = ""  # runner/harness error, if any
    trace: str = ""  # short, human-readable summary the proposer may read (evolve set only)
    detail: dict[str, Any] = field(default_factory=dict)  # domain extras (e.g. unauthorized_write)


@dataclass
class EvalResult:
    """One evaluation of one harness on one task split with one policy model."""

    harness_tree: str  # git tree hash of the harness directory
    split: str
    model: str
    rollout: int
    outcomes: dict[str, TaskOutcome]
    wall_s: float = 0.0
    projected_from: str = ""  # parent split this result was projected from ("" = evaluated directly)

    @property
    def score(self) -> float:
        """S: mean reward over all tasks of the split (missing tasks count 0)."""
        if not self.outcomes:
            return 0.0
        return sum(o.reward for o in self.outcomes.values()) / len(self.outcomes)

    @property
    def cost(self) -> float:
        """C: mean policy tokens per task."""
        if not self.outcomes:
            return 0.0
        return sum(o.tokens for o in self.outcomes.values()) / len(self.outcomes)

    @property
    def calls(self) -> int:
        return sum(o.calls for o in self.outcomes.values())

    @property
    def passed(self) -> list[str]:
        return sorted(t for t, o in self.outcomes.items() if o.reward >= 1.0)

    @property
    def failed(self) -> list[str]:
        return sorted(t for t, o in self.outcomes.items() if o.reward < 1.0)

    def count(self, detail_key: str) -> int:
        """Number of tasks whose detail flag `detail_key` is truthy (used by guards)."""
        return sum(1 for o in self.outcomes.values() if o.detail.get(detail_key))

    def to_json(self) -> dict[str, Any]:
        return {
            "harness_tree": self.harness_tree,
            "split": self.split,
            "model": self.model,
            "rollout": self.rollout,
            "wall_s": round(self.wall_s, 2),
            "score": round(self.score, 6),
            "cost": round(self.cost, 1),
            "calls": self.calls,
            **({"projected_from": self.projected_from} if self.projected_from else {}),
            "outcomes": {t: asdict(o) for t, o in sorted(self.outcomes.items())},
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> EvalResult:
        return cls(
            harness_tree=d["harness_tree"],
            split=d["split"],
            model=d["model"],
            rollout=int(d["rollout"]),
            outcomes={t: TaskOutcome(**o) for t, o in d["outcomes"].items()},
            wall_s=float(d.get("wall_s", 0.0)),
            projected_from=str(d.get("projected_from", "")),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=1))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> EvalResult:
        return cls.from_json(json.loads(path.read_text()))


def write_json(path: Path, obj: Any) -> None:
    """Atomic JSON write (write to a temp file, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=False))
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())
