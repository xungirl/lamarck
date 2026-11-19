"""The selector S: screens proposals before execution, then chooses among evaluated ones.

    H_{t+1} = S(H_t, F_t, C_t, F_t^C; M_t)          (paper Eq. 3)

Two LLM calls per round at most:

  screen  one call over all candidates that passed the deterministic critic. Rejects
          leakage, degenerate or no-op edits, grader gaming, undeclared bundling and
          unbounded loops (criteria adapted from RRSI's critic prompt). Rejected
          candidates receive no evaluation.
  select  one call over the candidates the RRSI rules admit (noise floor, cost rule,
          guards). It may pick one of them or none, weighing performance, complexity,
          domain specificity and regression risk. It cannot pick an inadmissible one.

Both calls read SIM under the run's memory condition. If a call fails or returns
something unusable, the deterministic fallback is used (screen: accept; select:
the admissible candidate with the highest score, as in RRSI's argmax).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .proposer import extract_json

SCREEN_SYSTEM = """You are a strict reviewer of harness (agent scaffold) changes in an automated optimization
loop. Harnesses are evolved on the very tasks they are measured on, so anti-overfitting review matters.
{brief}

REJECT a candidate if ANY of:
1. LEAKAGE / TASK-SPECIALIZATION: hard-codes evolve-set task ids, entity / database / table names,
   expected outputs, magic constants that only fit one task, or branches on task-identifying features.
   Litmus test: would the change still make sense on an unfamiliar task of the same kind?
2. DEGENERATE: effectively a no-op while claiming a mechanism, or deletes a working safety mechanism
   (error handling, output limits, termination guards) without replacement.
3. GRADER GAMING: tries to detect or influence the scorer instead of improving the task work.
4. UNDECLARED BUNDLING: independent changes not covered by the declared description.
5. UNBOUNDED WORK: loops or retries without an explicit exit.
{memory_note}
Otherwise ACCEPT. Review intent and content, not style; deterministic checks handle syntax.
Return strict JSON: {{"verdicts": [{{"sample": "s1", "verdict": "accept" | "reject", "reason": "..."}}]}}"""

SELECT_SYSTEM = """You are the selector of an automated harness-optimization loop. The candidates below were
evaluated on the evolve set and all passed the admissibility rules (noise floor, cost rule, domain guards).
Choose the candidate to keep as the next incumbent, or "none" to keep the current harness. Weigh measured
performance, complexity, domain specificity (generality beyond these tasks) and regression risk.
{memory_note}
Return strict JSON: {{"choice": "<sample id or none>", "reason": "<one or two sentences>"}}"""


@dataclass
class ScreenVerdict:
    sample: str
    accept: bool
    reason: str


def screen_messages(brief: str, candidates: list[dict[str, Any]], memory_block: str) -> list[dict[str, Any]]:
    note = (
        "You also see the SELF-IMPROVEMENT MEMORY: you may reject a candidate that repeats a direction the memory "
        "shows already failed, unless it states new evidence."
        if memory_block
        else ""
    )
    body = json.dumps(candidates, indent=1, ensure_ascii=False)
    user = f"### CANDIDATES\n{body}"
    if memory_block:
        user += "\n\n### SELF-IMPROVEMENT MEMORY\n" + memory_block
    return [
        {"role": "system", "content": SCREEN_SYSTEM.format(brief=brief, memory_note=note)},
        {"role": "user", "content": user},
    ]


def parse_screen(text: str, samples: list[str]) -> dict[str, ScreenVerdict]:
    obj = extract_json(text)
    out: dict[str, ScreenVerdict] = {}
    for v in obj.get("verdicts") or []:
        if isinstance(v, dict) and v.get("sample") in samples:
            s = str(v["sample"])
            out[s] = ScreenVerdict(
                s, str(v.get("verdict", "accept")).lower() != "reject", str(v.get("reason", ""))[:400]
            )
    for s in samples:  # a candidate the reviewer forgot is accepted, not silently dropped
        out.setdefault(s, ScreenVerdict(s, True, "no verdict returned; accepted by default"))
    return out


def select_messages(
    incumbent: dict[str, Any], candidates: list[dict[str, Any]], history: str, memory_block: str
) -> list[dict[str, Any]]:
    note = (
        "Use the SELF-IMPROVEMENT MEMORY: prefer edits whose rationale and recorded outcomes support them, and be "
        "wary of directions that repeatedly missed their predictions."
        if memory_block
        else ""
    )
    user = (
        f"### CURRENT INCUMBENT\n{json.dumps(incumbent, ensure_ascii=False)}\n\n"
        f"### ADMISSIBLE CANDIDATES\n{json.dumps(candidates, indent=1, ensure_ascii=False)}\n\n"
        f"### RUN HISTORY\n{history}"
    )
    if memory_block:
        user += "\n\n### SELF-IMPROVEMENT MEMORY\n" + memory_block
    return [
        {"role": "system", "content": SELECT_SYSTEM.format(memory_note=note)},
        {"role": "user", "content": user},
    ]


def parse_select(text: str, admissible: list[str]) -> tuple[str | None, str]:
    obj = extract_json(text)
    choice = str(obj.get("choice", "")).strip()
    reason = str(obj.get("reason", ""))[:400]
    if choice.lower() == "none":
        return None, reason
    if choice not in admissible:
        raise ValueError(f"selector chose {choice!r}, not admissible")
    return choice, reason
