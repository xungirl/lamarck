"""Execution accuracy (EX): does the predicted SQL return the same result as the gold SQL?

Rules (applied identically to every harness, so they cannot favour one candidate):

  * both queries run read-only on the task's SQLite file (pred 30 s, gold 60 s, <= 100,000 rows);
    a predicted query that errors, times out or exceeds the row cap scores 0;
  * cells: numbers compare numerically after rounding floats to 6 decimals (int 3 == 3.0);
    bytes are decoded; NULL is its own value; strings compare exactly;
  * rows compare as a MULTISET unless the gold query has a top-level ORDER BY (then in order);
  * column order may differ: any column permutation is accepted (exhaustive up to 6 columns).
    Column names are never compared. This follows the Spider test-suite execution match; it is
    slightly more lenient than BIRD's official set(pred) == set(gold) on column order and slightly
    stricter on duplicates. (Same rules as the author's rrsi-sql project.)
"""

from __future__ import annotations

import itertools
import re
from collections import Counter
from typing import Any

from .sqlexec import ExecResult, execute

PRED_TIMEOUT, GOLD_TIMEOUT, MAX_ROWS = 30.0, 60.0, 100_000


def _cell(v: Any) -> Any:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return v


def _rows(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return [tuple(_cell(c) for c in r) for r in rows]


def has_top_level_order_by(sql: str) -> bool:
    s = re.sub(r"'(?:[^']|'')*'", "''", sql or "")
    out, depth = [], 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return re.search(r"\border\s+by\b", "".join(out), re.I) is not None


def results_match(pred: list[tuple[Any, ...]], gold: list[tuple[Any, ...]], ordered: bool) -> bool:
    p, g = _rows(pred), _rows(gold)
    if len(p) != len(g):
        return False
    if not g:
        return True
    if len(p[0]) != len(g[0]):
        return False

    def key(rows: list[tuple[Any, ...]]) -> Any:
        return rows if ordered else Counter(rows)

    gk = key(g)
    if key(p) == gk:
        return True
    n = len(p[0])
    if n > 6:
        return False
    return any(key([tuple(r[i] for i in perm) for r in p]) == gk for perm in itertools.permutations(range(n)))


def run_gold(db_path: str, gold_sql: str) -> ExecResult:
    return execute(db_path, gold_sql, MAX_ROWS, GOLD_TIMEOUT)


def score(db_path: str, pred_sql: str, gold_sql: str, gold: ExecResult) -> tuple[float, ExecResult]:
    pred = execute(db_path, pred_sql, MAX_ROWS, PRED_TIMEOUT)
    if not pred.ok or pred.truncated or not gold.ok:
        return 0.0, pred
    return (1.0 if results_match(pred.rows, gold.rows, has_top_level_order_by(gold_sql)) else 0.0), pred
