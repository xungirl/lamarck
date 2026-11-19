"""How the proposer and selector see SIM under each memory condition.

  none        nothing (Cold Start, Harness Transfer)
  problems    root causes only: id + name (Problem-Only Memory)
  full        the complete record, every entry and attempt (Full SIM; the paper's
              proposer gets it "without retrieval or truncation")
  retrieved   research extension: only the top-k entries most relevant to the
              current failures, under a token budget (lamarck.sim.retrieval)

The guidance block paraphrases the eight instructions of the paper's App. D.2.
"""

from __future__ import annotations

import json

from .memory import SIM

GUIDANCE = """How to use the SELF-IMPROVEMENT MEMORY (SIM) below:
1. Treat entries as prior experience, with flexibility to choose a different edit.
2. Check the domain, benchmark and policy model behind each entry (the `domain` field of an attempt).
3. Start with an existing diagnosis when it explains the current failure, and cite its id.
4. Reuse the rationale, but reassess the edit after a benchmark or model change.
5. Revise, replace or override an earlier harness modification when new evidence warrants it.
6. Revisit a tested direction only when new evidence justifies it.
7. Create a new problem entry (cite null + a new root-cause name) when no diagnosis explains the failure.
8. Give task-specific evidence more weight in similar settings and less when benchmark and model both changed.
Fields: predicted = tasks the edit was expected to fix (recorded before evaluation); fixed/missed = what
actually happened; passed/total = evolve-set result of that attempt; accepted = the selector kept it."""

PROBLEMS_GUIDANCE = """KNOWN HARNESS PROBLEMS (root causes diagnosed earlier; no edits or outcomes are recorded).
Cite an id when your diagnosis matches one, otherwise name a new root cause."""


def approx_tokens(text: str) -> int:
    """Cheap, model-agnostic token estimate (~4 characters per token)."""
    return (len(text) + 3) // 4


def render(sim: SIM, mode: str) -> str:
    """The SIM block for a prompt under `mode` ('' when the condition has no memory)."""
    if mode == "none":
        return ""
    if mode == "problems":
        if not sim.problems:
            return PROBLEMS_GUIDANCE + "\n(none yet)"
        rows = [f"- {p.id}: {p.name}" for p in sim.problems]
        return PROBLEMS_GUIDANCE + "\n" + "\n".join(rows)
    if mode in ("full", "retrieved"):
        body = (
            json.dumps(sim.to_json(), indent=1, ensure_ascii=False)
            if sim.problems
            else "(empty: no entries yet)"
        )
        return GUIDANCE + "\n\nSIM JSON:\n" + body
    raise ValueError(f"unknown memory mode {mode!r}")
