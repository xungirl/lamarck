"""In-sandbox runtime for text-to-SQL: loads harness/solve.py and runs it on every task.

The harness sees only public task fields and read-only databases of THIS split, mounted
at /data/<db_id>/. Its answer (a SQL string) is written to results.jsonl and graded on the
host against the gold query, which never enters the sandbox.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from ...gateway.client import GatewayClient
from ...sandbox.runtime_util import (
    PolicyEnv,
    load_harness_module,
    read_config,
    read_text,
    run_tasks,
    runtime_flags,
)
from . import sqlexec


class SQLEnv:
    """What harness code may use. One instance per task (fresh policy-call budget)."""

    def __init__(
        self, policy: PolicyEnv, harness_dir: Path, data_dir: Path, config: dict[str, Any], current_db: str
    ) -> None:
        self._policy = policy
        self._current_db = current_db
        self._harness = harness_dir
        self._data = data_dir
        self.config = config
        self.max_calls = policy.max_calls
        self.log: list[str] = []

    def _db(self, db_id: str) -> str:
        p = self._data / db_id / f"{db_id}.sqlite"
        if not p.exists():
            raise FileNotFoundError(f"unknown database {db_id!r}")
        return str(p)

    def chat(self, messages: list[dict[str, Any]], max_tokens: int | None = None) -> str:
        res = self._policy.chat(messages, max_tokens=max_tokens)
        return res.content

    def schema(self, db_id: str) -> str:
        return sqlexec.schema_text(self._db(db_id))

    def execute(self, sql: str, max_rows: int = 20) -> dict[str, Any]:
        res = sqlexec.execute(
            self._db_for_sql(), sql, max_rows=max(1, min(int(max_rows), 200)), timeout_s=10.0
        )
        self.log.append(f"execute -> ok={res.ok} rows={len(res.rows)} {res.error[:80]}")
        return res.preview(max_rows)

    def _db_for_sql(self) -> str:
        return self._db(self._current_db)

    def descriptions(self, db_id: str) -> str:
        d = self._data / db_id / "database_description"
        if not d.is_dir():
            return ""
        parts = []
        for f in sorted(d.glob("*.csv")):
            raw = f.read_bytes().decode("utf-8", errors="replace")
            rows = list(csv.reader(io.StringIO(raw)))
            lines = [", ".join(c.strip() for c in r if c.strip()) for r in rows[1:]]
            parts.append(f"## {f.stem}\n" + "\n".join(x for x in lines if x))
        return "\n\n".join(parts)[:12000]

    def prompt(self, name: str) -> str:
        return read_text(self._harness, name)


def run_job(
    job: dict[str, Any],
    tasks: list[dict[str, Any]],
    harness_dir: Path,
    data_dir: Path,
    out_dir: Path,
    client: GatewayClient,
) -> None:
    harness = load_harness_module(harness_dir, "solve.py")
    config = read_config(harness_dir)

    def solve_one(task: dict[str, Any]) -> dict[str, Any]:
        policy = PolicyEnv(client, job["model"], job["max_calls"], job["policy_params"])
        env = SQLEnv(policy, harness_dir, data_dir, config, current_db=task["db_id"])
        try:
            out = harness.solve(dict(task), env)
            sql = str(out.get("sql", "")) if isinstance(out, dict) else str(out)
            err = ""
        except Exception as exc:  # untrusted harness code
            sql, err = "", f"{type(exc).__name__}: {exc}"[:300]
        return {
            "sql": sql[:8000],
            "tokens": policy.tokens,
            "calls": policy.calls,
            "error": err,
            "log": env.log[-6:],
            **runtime_flags(policy),
        }

    run_tasks(tasks, solve_one, out_dir, job.get("task_parallel", 8))
