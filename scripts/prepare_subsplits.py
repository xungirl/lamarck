"""Create the budget-revision sub-splits (PREREG deviation 3). Run once, before any experiment
under the revised budget; the output file's sha256 is recorded in PREREG.md.

Each sub-split is a uniform random sample (seed 20261119) of a frozen, pre-registered split,
drawn in a fixed order so adding a sub-split later would not change the earlier ones.
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lamarck.domains import subsplits

SEED = 20261119
PLAN = [  # (domain, parent split, sub-split, size), in drawing order
    ("text2sql", "bird_evolve", "bird_evolve_s30", 30),
    ("text2sql", "bird_heldout", "bird_heldout_s40", 40),
    ("text2sql", "spider_evolve", "spider_evolve_s30", 30),
    ("text2sql", "spider_heldout", "spider_heldout_s40", 40),
    ("supportops", "so_evolve", "so_evolve_s20", 20),
    ("supportops", "so_heldout", "so_heldout_s20", 20),
]


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    frozen = {
        "text2sql": json.loads((repo / "data/splits/text2sql.json").read_text())["splits"],
        "supportops": json.loads((repo / "data/splits/supportops.json").read_text())["splits"],
    }
    rng = random.Random(SEED)
    out: dict[str, dict[str, dict[str, object]]] = {}
    for domain, parent, name, size in PLAN:
        ids = sorted(rng.sample(sorted(frozen[domain][parent]), size))
        out.setdefault(domain, {})[name] = {"parent": parent, "ids": ids}
    doc = {"seed": SEED, "note": "uniform subsamples of frozen splits (PREREG deviation 3)", "subsplits": out}
    subsplits.FILE.write_text(json.dumps(doc, indent=1) + "\n")
    print(subsplits.FILE, hashlib.sha256(subsplits.FILE.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
