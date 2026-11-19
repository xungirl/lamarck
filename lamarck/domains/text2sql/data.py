"""Text-to-SQL task registry: Spider 1.0 dev (source) and BIRD mini-dev SQLite (target).

  data/raw/                 symlink to the downloaded datasets (not committed)
  data/splits/text2sql.json committed, pre-registered: task ids per split + seed (sha256 in PREREG.md)

Task ids: `sp<row>` = row of Spider dev.json, `bd<question_id>` = BIRD mini-dev question id.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
SPIDER = "raw/spider/x/spider_data"
BIRD = "raw/bird/x/minidev/MINIDEV"


def data_root() -> Path:
    return Path(os.environ.get("LAMARCK_DATA_ROOT", str(REPO / "data")))


def split_file() -> Path:
    return data_root() / "splits" / "text2sql.json"


@lru_cache(maxsize=2)
def all_tasks(root: str) -> dict[str, dict[str, Any]]:
    base = Path(root)
    out: dict[str, dict[str, Any]] = {}
    spider = json.loads((base / SPIDER / "dev.json").read_text())
    for i, r in enumerate(spider):
        db = r["db_id"]
        out[f"sp{i:04d}"] = {
            "id": f"sp{i:04d}", "dataset": "spider", "db_id": db, "question": r["question"], "hint": "",
            "gold_sql": r["query"], "db_dir": str(base / SPIDER / "database" / db), "difficulty": "",
        }  # fmt: skip
    bird = json.loads((base / BIRD / "mini_dev_sqlite.json").read_text())
    for r in bird:
        db = r["db_id"]
        tid = f"bd{int(r['question_id']):04d}"
        out[tid] = {
            "id": tid, "dataset": "bird", "db_id": db, "question": r["question"], "hint": r.get("evidence") or "",
            "gold_sql": r["SQL"], "db_dir": str(base / BIRD / "dev_databases" / db), "difficulty": r.get("difficulty", ""),
        }  # fmt: skip
    return out


def tasks() -> dict[str, dict[str, Any]]:
    return all_tasks(str(data_root()))


def splits() -> dict[str, list[str]]:
    """Frozen splits plus the budget-revision sub-splits (lamarck.domains.subsplits)."""
    from .. import subsplits

    data: dict[str, list[str]] = json.loads(split_file().read_text())["splits"]
    for name, entry in subsplits.table("text2sql").items():
        data[name] = [str(i) for i in entry["ids"]]
    return data


def db_file(t: dict[str, Any]) -> str:
    return str(Path(t["db_dir"]) / f"{t['db_id']}.sqlite")
