"""Git plumbing: every harness version is a commit, every candidate a branch.

Design (informed by RRSI's `rrsi/gitops.py`, Apache-2.0, re-implemented):

* Each run owns a small git repository whose only content is `harness/`.
* The incumbent is the tip of branch `evolve`; it is always a commit.
* Candidate j of round t is drafted in its own worktree on branch `cand/r<t>s<j>`
  started from the incumbent, so candidates cannot see each other's edits and can
  be evaluated concurrently.
* Accepting a candidate fast-forwards `evolve` to the candidate's commit.
* A continued (target) run clones the source run's repository, so the lineage of
  every harness version is preserved across a transfer.

The harness tree hash (`git rev-parse <ref>:harness`) identifies a harness by
content; it keys the evaluation cache.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

HARNESS_DIR = "harness"
EVOLVE = "evolve"
_IDENTITY = ["-c", "user.email=lamarck@localhost", "-c", "user.name=lamarck", "-c", "commit.gpgsign=false"]


class GitError(RuntimeError):
    pass


def git(cwd: Path, *args: str, strip: bool = True) -> str:
    """Run git in `cwd` and return stdout; raise GitError with stderr on failure."""
    r = subprocess.run(["git", *_IDENTITY, *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {cwd}: {r.stderr.strip()[-600:]}")
    return r.stdout.strip() if strip else r.stdout


@dataclass
class HarnessRepo:
    root: Path

    # ------------------------------------------------------------ creation
    @classmethod
    def create(cls, root: Path, seed_harness: Path, message: str = "H0: seed harness") -> HarnessRepo:
        """New repo with `seed_harness` copied to harness/ as the first commit on `evolve`."""
        root.mkdir(parents=True, exist_ok=True)
        git(root, "init", "-q", "-b", EVOLVE)
        shutil.copytree(seed_harness, root / HARNESS_DIR, ignore=shutil.ignore_patterns("__pycache__"))
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", message)
        return cls(root)

    @classmethod
    def clone_from(cls, source: HarnessRepo, root: Path, ref: str = EVOLVE) -> HarnessRepo:
        """Clone a source run's repo (full lineage) and point `evolve` at `ref`."""
        root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(source.root), str(root)], check=True)
        repo = cls(root)
        commit = git(source.root, "rev-parse", ref)
        git(root, "checkout", "-q", "-B", EVOLVE, commit)
        # keep the source's candidate branches visible for the lineage view
        for line in git(root, "branch", "-r").splitlines():
            remote = line.strip()
            if "->" in remote or not remote.startswith("origin/cand/"):
                continue
            local = remote.removeprefix("origin/")
            git(root, "branch", "-f", f"source/{local}", remote)
        return repo

    # ------------------------------------------------------------- queries
    def rev(self, ref: str) -> str:
        return git(self.root, "rev-parse", ref)

    def tree(self, ref: str = EVOLVE) -> str:
        """Content hash of harness/ at `ref` (12 hex chars)."""
        return git(self.root, "rev-parse", f"{ref}:{HARNESS_DIR}")[:12]

    def read_files(self, ref: str = EVOLVE) -> dict[str, str]:
        """{relative path: text} of every file under harness/ at `ref`."""
        names = git(self.root, "ls-tree", "-r", "--name-only", ref, "--", HARNESS_DIR).splitlines()
        out = {}
        for name in names:
            rel = name.removeprefix(HARNESS_DIR + "/")
            out[rel] = git(self.root, "show", f"{ref}:{name}", strip=False)
        return out

    def diff(self, base: str, head: str) -> str:
        return git(self.root, "diff", f"{base}..{head}", "--", HARNESS_DIR)

    def changed_files(self, base: str, head: str) -> list[str]:
        out = git(self.root, "diff", "--name-only", f"{base}..{head}", "--", HARNESS_DIR)
        return [p.removeprefix(HARNESS_DIR + "/") for p in out.splitlines() if p]

    def branch_exists(self, name: str) -> bool:
        r = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
            cwd=self.root,
            capture_output=True,
        )
        return r.returncode == 0

    # ------------------------------------------------------------ mutation
    def add_worktree(self, path: Path, branch: str, start: str = EVOLVE) -> Path:
        """Fresh worktree at `path` on a new branch `branch` (an older one is replaced)."""
        self.remove_worktree(path, branch)
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.root, "worktree", "add", "-q", "-b", branch, str(path), start)
        return path

    def remove_worktree(self, path: Path, branch: str | None = None) -> None:
        if path.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(path)], cwd=self.root, capture_output=True
            )
            shutil.rmtree(path, ignore_errors=True)
        git(self.root, "worktree", "prune")
        if branch and self.branch_exists(branch):
            git(self.root, "branch", "-D", branch)

    def commit_worktree(self, worktree: Path, message: str) -> str:
        """Commit everything under harness/ in `worktree`; return the commit hash."""
        git(worktree, "add", "-A", "--", HARNESS_DIR)
        git(worktree, "commit", "-q", "--allow-empty", "-m", message)
        return git(worktree, "rev-parse", "HEAD")

    def fast_forward(self, commit: str, branch: str = EVOLVE) -> None:
        r = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, commit], cwd=self.root, capture_output=True
        )
        if r.returncode != 0:
            raise GitError(f"{commit[:10]} is not a fast-forward of {branch}")
        git(self.root, "update-ref", f"refs/heads/{branch}", commit)
        # keep the main checkout's files in sync with the branch it has checked out
        if git(self.root, "rev-parse", "--abbrev-ref", "HEAD") == branch:
            git(self.root, "reset", "-q", "--hard", branch)

    def export(self, ref: str, dest: Path) -> Path:
        """Materialize harness/ at `ref` into `dest` (used to mount a harness read-only)."""
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for rel, text in self.read_files(ref).items():
            p = dest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return dest

    def lineage(self) -> list[dict[str, str]]:
        """Every commit reachable from any branch: hash, parents, subject (for the dashboard)."""
        fmt = "%H%x1f%P%x1f%s%x1f%D"
        out = git(self.root, "log", "--all", "--topo-order", f"--format={fmt}", strip=False)
        rows = []
        for line in out.splitlines():
            if not line.strip():
                continue
            h, parents, subject, refs = ([*line.split("\x1f"), "", "", ""])[:4]
            rows.append({"commit": h, "parents": parents, "subject": subject, "refs": refs})
        return rows
