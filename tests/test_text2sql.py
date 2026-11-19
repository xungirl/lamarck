"""Text-to-SQL: execution-match rules, read-only execution, pre-registered splits."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from lamarck.domains.text2sql import scoring, sqlexec
from lamarck.domains.text2sql.data import split_file

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path: Path) -> str:
    p = tmp_path / "t.sqlite"
    with sqlite3.connect(p) as c:
        c.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        c.executemany("INSERT INTO t VALUES (?, ?)", [(1, "x"), (2, "y"), (2, "y"), (3, None)])
    return str(p)


def test_result_matching_rules() -> None:
    assert scoring.results_match([(1, "a"), (2, "b")], [(2, "b"), (1, "a")], ordered=False)
    assert not scoring.results_match([(1, "a"), (2, "b")], [(2, "b"), (1, "a")], ordered=True)
    assert scoring.results_match([("a", 1)], [(1, "a")], ordered=False)  # column permutation
    assert scoring.results_match([(3,)], [(3.0,)], ordered=False)
    assert not scoring.results_match([(1,), (1,)], [(1,)], ordered=False)  # multiset, not set
    assert scoring.has_top_level_order_by("SELECT a FROM t ORDER BY a")
    assert not scoring.has_top_level_order_by("SELECT a FROM (SELECT a FROM t ORDER BY a)")


@pytest.mark.parametrize("sql", [
    "DELETE FROM t", "DROP TABLE t", "INSERT INTO t VALUES (9, 'z')", "UPDATE t SET a = 0",
    "CREATE TABLE u (x)", "ATTACH DATABASE ':memory:' AS m", "PRAGMA writable_schema = 1",
])  # fmt: skip
def test_execution_is_read_only(db: str, sql: str) -> None:
    assert not sqlexec.execute(db, sql).ok
    assert sqlexec.execute(db, "SELECT count(*) FROM t").rows == [(4,)]


def test_timeout_and_row_cap(db: str) -> None:
    slow = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
    r = sqlexec.execute(db, slow, timeout_s=0.5)
    assert not r.ok and "exceeded" in r.error
    many = sqlexec.execute(
        db,
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 50) SELECT x FROM c",
        max_rows=10,
    )
    assert many.truncated and len(many.rows) == 10


def test_score_against_gold(db: str) -> None:
    gold = scoring.run_gold(db, "SELECT b, count(*) FROM t GROUP BY b")
    assert scoring.score(db, "SELECT count(*), b FROM t GROUP BY b", "", gold)[0] == 1.0
    assert scoring.score(db, "SELECT b FROM t", "", gold)[0] == 0.0
    assert scoring.score(db, "SELEC nonsense", "", gold)[0] == 0.0


def test_splits_match_preregistration() -> None:
    prereg = (REPO / "PREREG.md").read_text()
    digest = hashlib.sha256(split_file().read_bytes()).hexdigest()
    assert digest in prereg
    doc = json.loads(split_file().read_text())
    s = doc["splits"]
    assert {k: len(v) for k, v in s.items()} == {
        "spider_evolve": 60,
        "spider_heldout": 120,
        "bird_evolve": 60,
        "bird_heldout": 120,
    }
    assert not set(s["bird_evolve"]) & set(s["bird_heldout"])
    assert not set(doc["spider_evolve_dbs"]) & set(doc["spider_heldout_dbs"])
