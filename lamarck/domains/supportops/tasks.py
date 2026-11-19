"""Eval task schema (pydantic) and YAML loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

Category = Literal["lookup", "multi_step", "write", "refusal", "ambiguous", "injection", "read_only"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo in the YAML is an error, not a silently skipped check


class ArgCheck(_Strict):
    tool: str
    match: dict[str, Any]


class StateCheck(_Strict):
    sql: str
    equals: Any


class Expect(_Strict):
    tools_required: list[str] = Field(default_factory=list)
    tools_forbidden: list[str] = Field(default_factory=list)
    args: list[ArgCheck] = Field(default_factory=list)
    state: list[StateCheck] = Field(default_factory=list)
    answer_contains_any: list[list[str]] = Field(default_factory=list)
    answer_not_contains: list[str] = Field(default_factory=list)
    writable_tables: list[str] = Field(default_factory=list)


class GoldCall(_Strict):
    tool: str
    args: dict[str, Any]


Store = Literal["core", "evolve", "heldout", "ood"]


class Task(_Strict):
    id: str
    category: Category
    prompt: str
    read_only: bool = False
    store: Store = "core"  # which seeded database the task runs against (see server/stores.py)
    expect: Expect
    gold: list[GoldCall] = Field(default_factory=list)
    gold_answer: str


def load_tasks(path: str | Path, only: set[str] | None = None) -> list[Task]:
    raw = yaml.safe_load(Path(path).read_text())
    tasks = [Task.model_validate(item) for item in raw]
    ids = [t.id for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task ids in suite")
    if only:
        missing = only - set(ids)
        if missing:
            raise ValueError(f"unknown task ids: {sorted(missing)}")
        tasks = [t for t in tasks if t.id in only]
    return tasks
