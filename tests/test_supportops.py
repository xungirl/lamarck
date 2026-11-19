"""Support-ops domain: every gold trajectory scores 100%, splits match the pre-registration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lamarck.domains.supportops.domain import SPLIT_FILE, TASK_FILES, SupportOpsDomain
from lamarck.domains.supportops.gold import replay_all
from lamarck.domains.supportops.tasks import load_tasks


@pytest.mark.parametrize("name", sorted(TASK_FILES))
def test_gold_replay_scores_100(name: str, tmp_path: Path) -> None:
    tasks = load_tasks(TASK_FILES[name])
    results = replay_all(tasks, SupportOpsDomain.seed_harness, tmp_path)
    failures = {
        tid: [f"{s.name}: {s.detail}" for s in scores if s.passed is False] for tid, scores in results.items()
    }
    failures = {k: v for k, v in failures.items() if v}
    assert not failures, failures


def test_splits_cover_each_store_once() -> None:
    splits = json.loads(SPLIT_FILE.read_text())["splits"]
    all_ids = [i for ids in splits.values() for i in ids]
    assert len(all_ids) == len(set(all_ids)) == 110
    dom = SupportOpsDomain()
    for split in splits:
        for t in dom.tasks(split):
            assert set(t.public) == {"id", "prompt", "read_only"}  # nothing else enters the sandbox


def test_guard_counts_unauthorized_writes() -> None:
    from lamarck.types import EvalResult, TaskOutcome

    def ev(flags: list[bool]) -> EvalResult:
        return EvalResult(
            "t",
            "s",
            "m",
            0,
            {
                f"x{i}": TaskOutcome(f"x{i}", 1.0, detail={"unauthorized_write": f})
                for i, f in enumerate(flags)
            },
        )

    dom = SupportOpsDomain()
    assert dom.guards(ev([False, False]), ev([True, False]))
    assert not dom.guards(ev([True, False]), ev([False, True]))


def test_split_file_matches_preregistration() -> None:
    import hashlib

    prereg = (Path(__file__).resolve().parents[1] / "PREREG.md").read_text()
    assert hashlib.sha256(SPLIT_FILE.read_bytes()).hexdigest() in prereg
    for path in TASK_FILES.values():
        assert hashlib.sha256(path.read_bytes()).hexdigest() in prereg
