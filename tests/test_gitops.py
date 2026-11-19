from __future__ import annotations

from pathlib import Path

import pytest

from lamarck.engine.gitops import GitError, HarnessRepo


def seed(tmp_path: Path) -> Path:
    d = tmp_path / "seed"
    d.mkdir()
    (d / "prompt.md").write_text("hello\n")
    return d


def test_candidate_branch_and_fast_forward(tmp_path: Path) -> None:
    repo = HarnessRepo.create(tmp_path / "repo", seed(tmp_path))
    base, tree0 = repo.rev("evolve"), repo.tree()
    wt = repo.add_worktree(tmp_path / "wt", "cand/r0s1")
    (wt / "harness" / "prompt.md").write_text("hello world\n")
    commit = repo.commit_worktree(wt, "r0 s1")
    repo.remove_worktree(wt)
    assert repo.changed_files(base, commit) == ["prompt.md"]
    assert repo.tree() == tree0  # incumbent unchanged until accepted
    repo.fast_forward(commit)
    assert repo.read_files()["prompt.md"] == "hello world\n"
    assert repo.tree() != tree0


def test_fast_forward_refuses_divergent_history(tmp_path: Path) -> None:
    repo = HarnessRepo.create(tmp_path / "repo", seed(tmp_path))
    commits = []
    for i in range(2):
        wt = repo.add_worktree(tmp_path / f"wt{i}", f"cand/r0s{i}")
        (wt / "harness" / "prompt.md").write_text(f"v{i}\n")
        commits.append(repo.commit_worktree(wt, f"s{i}"))
        repo.remove_worktree(wt)
    repo.fast_forward(commits[0])
    with pytest.raises(GitError):
        repo.fast_forward(commits[1])


def test_clone_preserves_lineage(tmp_path: Path) -> None:
    src = HarnessRepo.create(tmp_path / "src", seed(tmp_path))
    wt = src.add_worktree(tmp_path / "wt", "cand/r0s1")
    (wt / "harness" / "prompt.md").write_text("evolved\n")
    c = src.commit_worktree(wt, "evolved")
    src.remove_worktree(wt)
    src.fast_forward(c)
    tgt = HarnessRepo.clone_from(src, tmp_path / "tgt")
    assert tgt.rev("evolve") == c
    assert tgt.tree() == src.tree()
    assert any("evolved" in row["subject"] for row in tgt.lineage())
