"""Create the pre-registered support-ops splits (run once, before any experiment).

so_evolve = all 40 tasks of the evolve store; so_heldout = all 40 tasks of the heldout store;
the 30 ood tasks are split uniformly at random (seeded) into ood_evolve (15) and ood_heldout (15).
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lamarck.domains.supportops.domain import SPLIT_FILE, TASK_FILES
from lamarck.domains.supportops.tasks import load_tasks

SEED = 20261118


def main() -> None:
    sets = {name: load_tasks(path) for name, path in TASK_FILES.items()}
    ood = sorted(t.id for t in sets["ood"])
    rng = random.Random(SEED)
    rng.shuffle(ood)
    splits = {
        "so_evolve": sorted(t.id for t in sets["evolve"]),
        "so_heldout": sorted(t.id for t in sets["heldout"]),
        "ood_evolve": sorted(ood[:15]),
        "ood_heldout": sorted(ood[15:]),
    }
    cats = {t.id: t.category for s in sets.values() for t in s}
    doc = {
        "seed": SEED,
        "source": "agent-eval-mcp rrsi-wip@3b186b3 evals/sets/*.yaml (copied to lamarck/domains/supportops/)",
        "task_file_sha256": {n: hashlib.sha256(p.read_bytes()).hexdigest() for n, p in TASK_FILES.items()},
        "splits": splits,
        "categories": {n: dict(Counter(cats[i] for i in ids)) for n, ids in splits.items()},
    }
    SPLIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SPLIT_FILE.write_text(json.dumps(doc, indent=1) + "\n")
    print(SPLIT_FILE, hashlib.sha256(SPLIT_FILE.read_bytes()).hexdigest())
    print(
        {k: len(v) for k, v in splits.items()},
        doc["categories"]["ood_evolve"],
        doc["categories"]["ood_heldout"],
    )


if __name__ == "__main__":
    main()
