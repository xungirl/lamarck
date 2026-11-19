"""A deterministic toy domain for tests and CI (no network, no real model).

Each task asks the policy to transform an item according to its kind. The fake
policy (lamarck.gateway.fakes) applies a transformation only if the harness's
rules.md contains a line `RULE <kind>: <op>`; otherwise it echoes the item.
So the only way to raise the score is to evolve the harness, which makes the
whole RSI/CRSI pipeline testable end to end, offline and reproducibly.

  source  evolve / heldout       kinds: color, animal, number
  target  t_evolve / t_heldout   kinds: color, number, city, fruit   (a "dataset shift")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...types import TaskOutcome
from ..base import Domain, Task
from .ops import OPS

TRUE_OP = {"color": "upper", "animal": "reverse", "number": "double", "city": "title", "fruit": "sort"}

ITEMS = {
    "color": ["red", "blue", "green", "amber", "teal", "cyan", "plum", "ivory"],
    "animal": ["cat", "dog", "owl", "yak", "emu", "fox", "elk", "bee"],
    "number": ["one", "two", "six", "ten", "nine", "four", "five", "zero"],
    "city": ["paris", "lima", "oslo", "rome", "cairo", "delhi", "quito", "accra"],
    "fruit": ["pear", "fig", "kiwi", "lime", "plum", "date", "sloe", "yuzu"],
}
SPLITS = {
    "evolve": (["color", "animal", "number"], slice(0, 4)),
    "heldout": (["color", "animal", "number"], slice(4, 8)),
    "t_evolve": (["color", "number", "city", "fruit"], slice(0, 4)),
    "t_heldout": (["color", "number", "city", "fruit"], slice(4, 8)),
}


class ToyDomain(Domain):
    name = "toy"
    seed_harness = Path(__file__).parent / "seed_harness"
    runtime = "lamarck.domains.toy.runtime"
    editable = ("rules.md", "config.json")
    brief = (
        "Toy domain: each task gives a kind and an item; the policy must output the item transformed by the "
        "kind's operation. The harness is a rules file read as the system prompt."
    )
    contract = "Files: rules.md (system prompt; lines 'RULE <kind>: <op>' are obeyed), config.json."

    def splits(self) -> list[str]:
        return list(SPLITS)

    def tasks(self, split: str) -> list[Task]:
        kinds, sl = SPLITS[split]
        out = []
        for kind in kinds:
            for item in ITEMS[kind][sl]:
                tid = f"{split}-{kind}-{item}"
                out.append(
                    Task(tid, {"id": tid, "kind": kind, "item": item}, {"answer": OPS[TRUE_OP[kind]](item)})
                )
        return out

    def score(
        self, split: str, tasks: list[Task], raw: dict[str, dict[str, Any]], out_dir: Path
    ) -> dict[str, TaskOutcome]:
        outcomes = {}
        for t in tasks:
            r = raw.get(t.id, {"error": "missing result"})
            answer = str(r.get("answer", "")).strip()
            ok = answer == t.private["answer"]
            outcomes[t.id] = TaskOutcome(
                task_id=t.id,
                reward=1.0 if ok else 0.0,
                tokens=int(r.get("tokens", 0)),
                calls=int(r.get("calls", 0)),
                error=str(r.get("error", "")),
                trace=f"kind={t.public['kind']} item={t.public['item']} -> answered {answer!r}",
            )
        return outcomes

    def critic_patterns(self) -> list[tuple[str, str]]:
        # answers for individual items are leakage; rules per kind are not
        return [(r"\b(evolve|heldout|t_evolve|t_heldout)-\w+-\w+\b", "task id in harness")]
