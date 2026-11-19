"""The ordinary run history D_t: one record per candidate per round.

This is the run-local history every condition keeps (paper App. C.3: "All
methods retain the ordinary target-run history D_t"). It is distinct from SIM:
D_t covers only the current run and every candidate, including rejected ones;
SIM covers only selected edits, grouped by root cause, and can be carried
into a later run.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CandidateRecord:
    round: int
    sample: str
    branch: str = ""
    commit: str = ""
    tree: str = ""
    cite: str | None = None
    root_cause: str = ""
    what: str = ""
    files: list[str] = field(default_factory=list)
    n_edits: int = 0
    predicted: list[str] = field(default_factory=list)
    gate: str = ""  # "" if the candidate passed every gate, else why it was dropped
    score: float | None = None
    cost: float | None = None
    d_score: float | None = None
    d_cost: float | None = None
    admissible: bool = False
    decision: str = ""
    selected: bool = False
    fixed: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)


class RunHistory:
    def __init__(self, path: Path) -> None:
        self.path = path

    def records(self) -> list[CandidateRecord]:
        if not self.path.exists():
            return []
        return [CandidateRecord(**json.loads(x)) for x in self.path.read_text().splitlines() if x.strip()]

    def write_round(self, t: int, recs: list[CandidateRecord]) -> None:
        """Replace round t's records (idempotent, so a re-run round does not duplicate)."""
        kept = [r for r in self.records() if r.round != t]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(asdict(r)) + "\n" for r in [*kept, *recs]))
        tmp.replace(self.path)

    def measured(self) -> list[tuple[int, list[str], float]]:
        """(round, files touched, d_score) of every evaluated candidate (for the prune set)."""
        return [(r.round, r.files, r.d_score) for r in self.records() if r.d_score is not None]

    def accepted_files(self) -> set[str]:
        return {f for r in self.records() if r.selected for f in r.files}

    def render(self, max_rounds: int = 8) -> str:
        """Compact text of earlier rounds for the proposer and selector."""
        recs = self.records()
        if not recs:
            return "(no earlier rounds in this run)"
        rounds = sorted({r.round for r in recs})[-max_rounds:]
        lines = []
        for t in rounds:
            for r in (x for x in recs if x.round == t):
                status = "SELECTED" if r.selected else (f"DROPPED ({r.gate})" if r.gate else "not selected")
                metric = ""
                if r.d_score is not None:
                    metric = f" dS={r.d_score * 100:+.1f}pts dC={(r.d_cost or 0) * 100:+.0f}%"
                cite = f" [{r.cite}]" if r.cite else ""
                lines.append(
                    f"- r{t} {r.sample} {status}{metric}; root cause{cite}: {r.root_cause[:140]}; "
                    f"edit: {r.what[:200]}; files: {', '.join(r.files)}"
                )
                if r.decision and not r.selected and not r.gate:
                    lines.append(f"    decision: {r.decision[:200]}")
        return "\n".join(lines)

    def to_json(self) -> list[dict[str, Any]]:
        return [asdict(r) for r in self.records()]
