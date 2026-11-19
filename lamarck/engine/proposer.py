"""The proposer P: C_t = P(H_t, F_t, D_t; M_t)   (paper Eq. 3).

One LLM call per candidate. The proposer reads the incumbent harness (all
files), evolve-set feedback F_t (failing tasks with short traces, passing task
ids), the run history D_t, the prune set, the edit budget b_t and, depending on
the memory condition, the Self-Improvement Memory M_t. It returns one candidate
as strict JSON: a root-cause citation or a new root cause, a description of the
edit, complete new contents of the files it changes, and the ids of currently
failing evolve tasks it predicts the edit will fix. The prediction is sealed to
disk before the candidate is evaluated (lamarck.engine.loop).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..domains.base import Domain, Task
from ..types import EvalResult

DIVERSITY_HINTS = [
    "Target the most frequent failure pattern in the feedback.",
    "Target a different failure pattern than the most frequent one, or a weakness the frequent pattern hides.",
    "Consider a structural change (control flow, verification, context) or removing machinery that does not "
    "pay for itself.",
    "Look at the passing tasks too: make a change that fixes failures without breaking what works.",
    "Propose the smallest change you believe has the largest expected gain.",
]

SYSTEM = """You are a harness engineer inside an automated harness-optimization loop.
A frozen policy model ({policy_model}) solves tasks inside the HARNESS below. You improve the harness
(prompts, configuration, control code around the model), never the model itself.

DOMAIN
{brief}

HARNESS CONTRACT
{contract}

RULES
- Generalize. Never hard-code task ids, database / table / entity names, expected answers, or logic that
  only makes sense for particular evolve-set tasks: a leakage screen rejects such edits before evaluation,
  and the result is measured on held-out tasks you never see.
- Edit budget this round: at most {budget} independent edit(s) in this candidate.
- You may only create or change these files: {editable}. Python files must stay importable and keep the
  contract's function signatures.
- Every edit must have a plausible causal link to the failures you diagnose.

OUTPUT: strict JSON only, no prose before or after:
{{"root_cause": {{"cite": "<id of an existing memory entry, or null>", "name": "<one-line root-cause hypothesis>"}},
 "what": "<what the edit changes and why, 1-3 sentences>",
 "n_independent_edits": <integer>,
 "edits": [{{"file": "<path relative to the harness>", "content": "<the COMPLETE new file content>"}}],
 "predicted": ["<ids of CURRENTLY FAILING evolve tasks you expect this edit to fix>"]}}"""


@dataclass
class Proposal:
    sample: str
    cite: str | None
    root_cause: str
    what: str
    n_edits: int
    edits: dict[str, str | None]  # file -> new content (None deletes)
    predicted: list[str]
    dropped_predictions: list[str] = field(default_factory=list)
    raw: str = ""


class ProposalError(ValueError):
    pass


def render_harness(files: dict[str, str]) -> str:
    return "\n\n".join(f"=== file: {name} ===\n{text}" for name, text in sorted(files.items()))


def render_feedback(domain: Domain, tasks: list[Task], ev: EvalResult, max_chars: int) -> str:
    by_id = {t.id: t for t in tasks}
    head = (
        f"Incumbent evolve-set score: {len(ev.passed)}/{len(ev.outcomes)} = {ev.score * 100:.1f}%; "
        f"mean policy tokens per task: {ev.cost:.0f}"
    )
    lines: list[str] = []
    used = 0
    for tid in ev.failed:
        o = ev.outcomes[tid]
        task_line = domain.describe_task(by_id[tid]) if tid in by_id else ""
        err = f" | error: {o.error[:160]}" if o.error else ""
        line = f"- {tid} | {task_line} | {o.trace}{err}"
        if used + len(line) > max_chars:
            lines.append(f"- ... {len(ev.failed) - len(lines)} more failing tasks omitted for length")
            break
        lines.append(line[:1200])
        used += len(line)
    passing = ", ".join(ev.passed)
    return (
        f"{head}\nFAILING TASKS (id | task | trace):\n"
        + "\n".join(lines)
        + f"\nPASSING TASKS (do not break): {passing}"
    )


def build_messages(
    *,
    domain: Domain,
    policy_model: str,
    files: dict[str, str],
    feedback: str,
    history: str,
    prune: list[str],
    memory_block: str,
    budget: int,
    t: int,
    sample_index: int,
) -> list[dict[str, Any]]:
    system = SYSTEM.format(
        policy_model=policy_model,
        brief=domain.brief,
        contract=domain.contract,
        budget=budget,
        editable=", ".join(domain.editable),
    )
    parts = [
        "### HARNESS FILES (current incumbent)\n" + render_harness(files),
        "### EVOLVE-SET FEEDBACK\n" + feedback,
        "### RUN HISTORY (earlier rounds of this run)\n" + history,
    ]
    if prune:
        parts.append(
            "### PRUNE SET\nRecent edits to these files did not pay off (best recent gain <= 0). "
            "Consider simplifying or removing machinery there: " + ", ".join(prune)
        )
    if memory_block:
        parts.append("### SELF-IMPROVEMENT MEMORY\n" + memory_block)
    hint = DIVERSITY_HINTS[sample_index % len(DIVERSITY_HINTS)]
    parts.append(
        f"### YOUR TASK\nPropose candidate s{sample_index + 1} of round {t}. {hint}\nReturn the JSON object only."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]


def extract_json(text: str) -> dict[str, Any]:
    """The first top-level JSON object in `text` (fences and prose tolerated)."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start = text.find("{")
    if start < 0:
        raise ProposalError("no JSON object in response")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start : i + 1])
                except json.JSONDecodeError as exc:
                    raise ProposalError(f"invalid JSON: {exc}") from exc
                if not isinstance(obj, dict):
                    raise ProposalError("JSON is not an object")
                return obj
    raise ProposalError("unterminated JSON object")


def parse_proposal(text: str, sample: str, domain: Domain, failing: set[str]) -> Proposal:
    obj = extract_json(text)
    rc = obj.get("root_cause") or {}
    if not isinstance(rc, dict):
        rc = {"cite": None, "name": str(rc)}
    cite = rc.get("cite")
    cite = str(cite).strip() if cite not in (None, "", "null", "None") else None
    name = str(rc.get("name") or "").strip()
    edits_raw = obj.get("edits") or []
    if not isinstance(edits_raw, list) or not edits_raw:
        raise ProposalError("no edits")
    edits: dict[str, str | None] = {}
    for e in edits_raw:
        if not isinstance(e, dict) or "file" not in e:
            raise ProposalError("malformed edit entry")
        rel = str(e["file"]).strip().removeprefix("harness/")
        if not domain.is_editable(rel):
            raise ProposalError(f"file not editable: {rel}")
        content = e.get("content")
        if content is not None and not isinstance(content, str):
            raise ProposalError(f"content of {rel} is not a string")
        if content is not None and len(content) > domain.max_file_chars:
            raise ProposalError(f"{rel} exceeds {domain.max_file_chars} characters")
        edits[rel] = content
    predicted_raw = [str(p) for p in (obj.get("predicted") or []) if isinstance(p, (str, int))]
    predicted = sorted({p for p in predicted_raw if p in failing})
    dropped = sorted({p for p in predicted_raw if p not in failing})
    try:
        n_edits = int(obj.get("n_independent_edits") or len(edits))
    except (TypeError, ValueError):
        n_edits = len(edits)
    if not name and cite is None:
        raise ProposalError("missing root cause")
    return Proposal(
        sample=sample,
        cite=cite,
        root_cause=name,
        what=str(obj.get("what") or "").strip(),
        n_edits=max(1, n_edits),
        edits=edits,
        predicted=predicted,
        dropped_predictions=dropped,
        raw=text,
    )
