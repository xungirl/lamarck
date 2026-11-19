"""Read-only, time- and row-limited SQLite execution (used in the sandbox and on the host).

Three independent layers keep every database read-only:

  1. the file is opened with `mode=ro&immutable=1` (URI), so SQLite cannot write or lock it;
  2. `PRAGMA query_only = ON`;
  3. an authorizer that allows only read actions (SELECT, READ, FUNCTION, RECURSIVE).

A progress handler aborts statements that run past the time limit, and rows are
fetched with fetchmany so a runaway cross join cannot exhaust memory.
(The same three-layer design is used in the author's rrsi-sql project.)
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}


@dataclass
class ExecResult:
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    truncated: bool = False
    error: str = ""
    elapsed: float = 0.0

    def preview(self, n: int = 5) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "columns": self.columns,
            "rows": [list(r) for r in self.rows[:n]],
            "n_rows": len(self.rows),
            "truncated": self.truncated,
            "error": self.error,
        }


def connect_ro(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")

    def authorizer(action: int, *_args: Any) -> int:
        return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY

    conn.set_authorizer(authorizer)
    return conn


def execute(db_path: str, sql: str, max_rows: int = 100_000, timeout_s: float = 30.0) -> ExecResult:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        return ExecResult(False, error="empty query")
    t0 = time.monotonic()
    deadline = t0 + timeout_s
    try:
        conn = connect_ro(db_path)
    except sqlite3.Error as exc:
        return ExecResult(False, error=f"cannot open database: {exc}")
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in (cur.description or [])]
        rows: list[tuple[Any, ...]] = []
        truncated = False
        while True:
            batch = cur.fetchmany(1000)
            if not batch:
                break
            rows.extend(tuple(r) for r in batch)
            if len(rows) > max_rows:
                rows, truncated = rows[:max_rows], True
                break
        return ExecResult(True, cols, rows, truncated, elapsed=time.monotonic() - t0)
    except sqlite3.Error as exc:
        msg = str(exc)
        if "interrupted" in msg:
            msg = f"query exceeded {timeout_s:.0f}s"
        return ExecResult(False, error=msg[:300], elapsed=time.monotonic() - t0)
    except (ValueError, OverflowError, MemoryError) as exc:
        return ExecResult(False, error=f"{type(exc).__name__}: {exc}"[:300], elapsed=time.monotonic() - t0)
    finally:
        conn.close()


def schema_text(db_path: str) -> str:
    """CREATE statements of every table (and view) in the database."""
    conn = connect_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type IN ('table', 'view') AND sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()
    return "\n\n".join(r[0].strip() + ";" for r in rows)
