"""Text-to-SQL domain: Spider dev (source) and BIRD mini-dev (target), execution accuracy.

Splits (pre-registered, data/splits/text2sql.json):
  spider_evolve / spider_heldout   database-disjoint halves of Spider dev (source RSI)
  bird_evolve / bird_heldout       disjoint questions of BIRD mini-dev (target RSI, cross-model RSI)

The sandbox gets the public question fields and read-only copies of only this split's
databases. Gold SQL stays on the host. The proposer sees, for each failing evolve task,
the predicted query, its error or a preview of its result, and a preview of the expected
result (never the gold query itself).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from ...types import TaskOutcome
from ..base import Domain, Task
from . import data, scoring
from .sqlexec import ExecResult


class Text2SQLDomain(Domain):
    name = "text2sql"
    seed_harness = Path(__file__).parent / "seed_harness"
    runtime = "lamarck.domains.text2sql.runtime"
    editable = ("*.md", "*.json", "*.py")
    max_calls_per_task = 4
    brief = (
        "Text-to-SQL on SQLite (Spider / BIRD style). Each task gives a database id, a natural-language question "
        "and sometimes a hint (BIRD 'evidence': domain knowledge or formulas). The harness must return one SQLite "
        "query; it is graded by execution accuracy: the query's result must equal the gold query's result "
        "(row multiset; order matters only if the gold query orders; column names are ignored, column order "
        "may differ). Typical failure modes: wrong columns or extra columns, wrong joins, value formatting, "
        "aggregation/grouping mistakes, misread hints, ties and LIMIT handling, NULL handling."
    )
    contract = (
        "solve.py must define solve(task, env) -> {'sql': str}. task = {id, db_id, question, hint}. "
        "env.chat(messages, max_tokens=None) -> str makes ONE policy call (at most env.max_calls = 4 per task, "
        "further calls raise); env.schema(db_id) -> CREATE statements; env.execute(sql, max_rows=20) -> "
        "{ok, columns, rows, n_rows, error} runs a read-only query on the task's database (use it to check a "
        "draft query); env.descriptions(db_id) -> column descriptions when the dataset ships them, else ''; "
        "env.prompt(name) -> text of another harness file; env.config -> parsed config.json. "
        "Only the Python standard library modules re, json, math, itertools, collections, difflib, textwrap, "
        "string, functools may be imported."
    )

    def __init__(self) -> None:
        self._gold: dict[str, ExecResult] = {}
        self._lock = threading.Lock()

    def splits(self) -> list[str]:
        return list(data.splits())

    def tasks(self, split: str) -> list[Task]:
        reg = data.tasks()
        out = []
        for tid in data.splits()[split]:
            t = reg[tid]
            public = {"id": tid, "db_id": t["db_id"], "question": t["question"], "hint": t["hint"]}
            out.append(
                Task(
                    tid,
                    public,
                    {"gold_sql": t["gold_sql"], "db_file": data.db_file(t), "difficulty": t["difficulty"]},
                )
            )
        return out

    def data_mounts(self, split: str) -> dict[str, Path]:
        reg = data.tasks()
        dbs = {reg[tid]["db_id"]: Path(reg[tid]["db_dir"]) for tid in data.splits()[split]}
        return dict(sorted(dbs.items()))

    def describe_task(self, task: Task) -> str:
        hint = f" | hint: {task.public['hint']}" if task.public.get("hint") else ""
        return f"db={task.public['db_id']} | question: {task.public['question']}{hint}"

    def _gold_result(self, task: Task) -> ExecResult:
        with self._lock:
            if task.id not in self._gold:
                self._gold[task.id] = scoring.run_gold(task.private["db_file"], task.private["gold_sql"])
            return self._gold[task.id]

    def score(
        self, split: str, tasks: list[Task], raw: dict[str, dict[str, Any]], out_dir: Path
    ) -> dict[str, TaskOutcome]:
        outcomes = {}
        for t in tasks:
            r = raw.get(t.id, {"error": "missing result"})
            sql = str(r.get("sql") or "")
            gold = self._gold_result(t)
            reward, pred = scoring.score(t.private["db_file"], sql, t.private["gold_sql"], gold)
            if reward:
                trace = f"correct; sql: {sql[:300]}"
            elif not sql:
                trace = f"no query returned. harness error: {r.get('error', '')[:200]}"
            elif not pred.ok:
                trace = f"sql: {sql[:500]} | execution error: {pred.error[:200]}"
            else:
                p, g = pred.preview(3), gold.preview(3)
                trace = (
                    f"sql: {sql[:500]} | got {p['n_rows']} rows x {len(p['columns'])} cols, first {p['rows']} | "
                    f"expected {g['n_rows']} rows x {len(g['columns'])} cols, first {g['rows']}"
                )
            outcomes[t.id] = TaskOutcome(
                task_id=t.id,
                reward=reward,
                tokens=int(r.get("tokens", 0)),
                calls=int(r.get("calls", 0)),
                error=str(r.get("error", ""))[:300],
                trace=trace[:1100],
                detail={"difficulty": t.private.get("difficulty", ""), "valid_sql": bool(pred.ok)},
            )
        return outcomes

    def critic_patterns(self) -> list[tuple[str, str]]:
        reg = data.tasks()
        # only distinctive names (with an underscore); plain words like "singer" would flag generic text
        dbs = sorted(
            {reg[t]["db_id"] for ids in data.splits().values() for t in ids if "_" in reg[t]["db_id"]}
        )
        return [
            (r"\b(?:sp|bd)\d{4}\b", "task id in harness"),
            (r"\b(?:" + "|".join(dbs) + r")\b", "database name in harness (task-specific logic)"),
        ]
