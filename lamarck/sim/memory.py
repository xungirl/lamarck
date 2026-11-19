"""Self-Improvement Memory (SIM), following Yu et al., arXiv 2611.18856, Sec. 3.2 and App. D.

SIM is memory for the *harness optimizer* (proposer and selector), not for the
task-solving policy. It groups selected harness edits by the recurring harness
problem (root-cause hypothesis) they address:

    m_i = (c_i, [(a_ij, o_ij, p_ij)]_j)                       (paper Eq. 2)

  c_i    root-cause text                       -> Problem.name
  a_ij   modification and rationale            -> Attempt.what, Attempt.files
  o_ij   predicted and observed task outcomes  -> Attempt.predicted / fixed / missed / passed / total
  p_ij   provenance (domain, benchmark, policy, round) -> Attempt.domain, Attempt.round

Update rule M_{t+1} = U(M_t, e_t): after selection only the chosen candidate e_t
is recorded, including its measured outcome even if it regressed or missed its
predictions. The record is appended to the problem the proposer cited, or a new
entry is created. Entries with identical root-cause text are merged
deterministically without dropping attempts, and the merged identifier is kept
in `aliases` (App. D.1). If nothing is accepted, e_t is empty and M is unchanged.

The on-disk format is the JSON of App. D.3:

    {"next_id": 10, "carried_from": {"arm", "domain", "into"} | null,
     "aliases": {"P6": "P5"}, "problems": [{id, name, opened_round, attempts[]}]}

    attempt = {round, sample, accepted, backfilled, what, files,
               predicted, fixed, missed, passed, total, domain}

`backfilled` is not defined in the paper's text. Here it marks an attempt that
was reconstructed from the run history after the fact (e.g. when a crashed run
is resumed and its SIM update is replayed) instead of being written live.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

WHAT_MAX_CHARS = 280  # the paper's records are stored truncated ("..."), see Figs. 9-10


def normalize_root_cause(text: str) -> str:
    """Identity used for deterministic merging: case- and whitespace-insensitive, otherwise exact."""
    return re.sub(r"\s+", " ", text).strip().casefold()


def truncate(text: str, limit: int = WHAT_MAX_CHARS) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def files_field(files: list[str], shown: int = 3) -> str:
    """The paper's compact `files` string, e.g. "a.py, b.py, c.md +1 more"."""
    files = sorted(dict.fromkeys(files))
    head = ", ".join(files[:shown])
    return head + (f" +{len(files) - shown} more" if len(files) > shown else "")


@dataclass
class Attempt:
    round: int
    sample: str
    accepted: bool
    backfilled: bool
    what: str
    files: str
    predicted: list[str]
    fixed: list[str]
    missed: list[str]
    passed: int
    total: int
    domain: str

    def __post_init__(self) -> None:
        if set(self.fixed) & set(self.missed):
            raise ValueError("a task cannot be both fixed and missed")
        if not set(self.fixed) | set(self.missed) <= set(self.predicted):
            raise ValueError("fixed and missed must be subsets of predicted")
        if not 0 <= self.passed <= self.total:
            raise ValueError("need 0 <= passed <= total")


@dataclass
class Problem:
    id: str
    name: str
    opened_round: int
    attempts: list[Attempt] = field(default_factory=list)


@dataclass
class SIM:
    next_id: int = 1
    carried_from: dict[str, Any] | None = None
    aliases: dict[str, str] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)

    # ---------------------------------------------------------------- lookup
    def resolve(self, pid: str) -> str | None:
        """Follow aliases (P6 -> P5 -> ...) to a live problem id, or None."""
        seen: set[str] = set()
        while pid in self.aliases and pid not in seen:
            seen.add(pid)
            pid = self.aliases[pid]
        return pid if any(p.id == pid for p in self.problems) else None

    def get(self, pid: str) -> Problem | None:
        real = self.resolve(pid)
        return next((p for p in self.problems if p.id == real), None)

    def by_name(self, name: str) -> Problem | None:
        key = normalize_root_cause(name)
        return next((p for p in self.problems if normalize_root_cause(p.name) == key), None)

    @property
    def n_attempts(self) -> int:
        return sum(len(p.attempts) for p in self.problems)

    # ---------------------------------------------------------------- update
    def update(self, attempt: Attempt, *, cite: str | None, name: str, opened_round: int) -> str:
        """U(M_t, e_t): append the selected candidate's attempt; return the problem id it went to.

        Resolution order: a valid citation (aliases followed), then an existing entry
        with identical root-cause text, then a new entry P<next_id>.
        """
        target = self.get(cite) if cite else None
        if target is None and name.strip():
            target = self.by_name(name)
        if target is None:
            if not name.strip():
                raise ValueError("a new problem entry needs a root-cause name")
            target = Problem(id=f"P{self.next_id}", name=name.strip(), opened_round=opened_round)
            self.next_id += 1
            self.problems.append(target)
        target.attempts.append(attempt)
        self.merge_identical()
        resolved = self.resolve(target.id)
        assert resolved is not None
        return resolved

    def merge_identical(self) -> list[tuple[str, str]]:
        """Merge entries whose root-cause text is identical. The earliest entry (list order)
        keeps its id; later duplicates are appended to it attempt by attempt and become aliases.
        Returns the (merged, kept) pairs. Idempotent and deterministic."""
        merged: list[tuple[str, str]] = []
        keep: dict[str, Problem] = {}
        survivors: list[Problem] = []
        for p in self.problems:
            key = normalize_root_cause(p.name)
            if key in keep:
                k = keep[key]
                k.attempts.extend(p.attempts)
                k.opened_round = min(k.opened_round, p.opened_round)
                self.aliases[p.id] = k.id
                merged.append((p.id, k.id))
            else:
                keep[key] = p
                survivors.append(p)
        self.problems = survivors
        # flatten alias chains so every alias points at a live entry
        for a in list(self.aliases):
            real = self.resolve(a)
            if real is not None:
                self.aliases[a] = real
        return merged

    # ------------------------------------------------------------- transfer
    def carried(self, arm: str, domain: str, into: str) -> SIM:
        """A copy of this memory to carry into a target run (Full SIM condition)."""
        out = copy.deepcopy(self)
        out.carried_from = {"arm": arm, "domain": domain, "into": into}
        return out

    def problems_only(self) -> SIM:
        """Problem-Only Memory: root causes kept, modifications/outcomes/provenance dropped."""
        out = copy.deepcopy(self)
        for p in out.problems:
            p.attempts = []
        return out

    @classmethod
    def pooled(cls, memories: list[SIM], labels: list[str]) -> SIM:
        """Concatenate several runs' memories into one (ids renumbered, identical root causes
        merged). Used by the retrieval extension, where memory grows across runs."""
        out = cls()
        for mem, label in zip(memories, labels, strict=True):
            for p in mem.problems:
                q = copy.deepcopy(p)
                old = q.id
                q.id = f"P{out.next_id}"
                out.next_id += 1
                out.problems.append(q)
                out.aliases[f"{label}:{old}"] = q.id
        out.merge_identical()
        return out

    # ------------------------------------------------------------------- IO
    def to_json(self) -> dict[str, Any]:
        return {
            "next_id": self.next_id,
            "carried_from": self.carried_from,
            "aliases": dict(self.aliases),
            "problems": [
                {
                    "id": p.id,
                    "name": p.name,
                    "opened_round": p.opened_round,
                    "attempts": [asdict(a) for a in p.attempts],
                }
                for p in self.problems
            ],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SIM:
        return cls(
            next_id=int(d.get("next_id", 1)),
            carried_from=d.get("carried_from"),
            aliases=dict(d.get("aliases", {})),
            problems=[
                Problem(
                    id=p["id"],
                    name=p["name"],
                    opened_round=int(p["opened_round"]),
                    attempts=[Attempt(**a) for a in p.get("attempts", [])],
                )
                for p in d.get("problems", [])
            ],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=1, ensure_ascii=False))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> SIM:
        return cls.from_json(json.loads(path.read_text())) if path.exists() else cls()

    def check_invariants(self) -> None:
        """Raise AssertionError if the memory is malformed (used by tests and on load)."""
        ids = [p.id for p in self.problems]
        assert len(ids) == len(set(ids)), "duplicate problem ids"
        names = [normalize_root_cause(p.name) for p in self.problems]
        assert len(names) == len(set(names)), "unmerged identical root causes"
        nums = [int(i[1:]) for i in ids if re.fullmatch(r"P\d+", i)]
        assert all(n < self.next_id for n in nums), "next_id not above every id"
        for a, b in self.aliases.items():
            assert a not in ids, f"alias {a} shadows a live entry"
            assert self.resolve(a) is not None, f"dangling alias {a} -> {b}"
