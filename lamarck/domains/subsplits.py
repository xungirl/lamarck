"""Budget-revision sub-splits (PREREG deviation 3): seeded uniform subsamples of the frozen splits.

data/splits/subsplits.json (committed, sha256 in PREREG.md) maps
  {domain: {sub-split: {"parent": split, "ids": [...]}}}

A sub-split behaves exactly like its parent (same databases / store, same scoring), only with
fewer tasks; the Evaluator can derive its result from a cached evaluation of the parent.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

FILE = Path(__file__).resolve().parents[2] / "data" / "splits" / "subsplits.json"


@lru_cache(maxsize=1)
def _load(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    data: dict[str, Any] = json.loads(p.read_text())["subsplits"]
    return data


def table(domain: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = _load(str(FILE)).get(domain, {})
    return out


def parent_of(domain: str, split: str) -> str | None:
    entry = table(domain).get(split)
    return str(entry["parent"]) if entry else None


def ids_of(domain: str, split: str) -> list[str] | None:
    entry = table(domain).get(split)
    return [str(i) for i in entry["ids"]] if entry else None


def root_split(domain: str, split: str) -> str:
    """The frozen split a (possibly nested) sub-split ultimately comes from."""
    seen = set()
    while (p := parent_of(domain, split)) is not None and split not in seen:
        seen.add(split)
        split = p
    return split
