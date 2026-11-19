"""Create the pre-registered text-to-SQL splits (run once, before any experiment).

Spider dev: the 20 databases are shuffled with the seed; 10 go to the evolve pool and 10 to
the held-out pool (database-disjoint). 60 evolve and 120 held-out questions are sampled
uniformly from their pools. BIRD mini-dev (SQLite): 60 target-evolve and 120 target-held-out
questions sampled uniformly (disjoint questions; databases are shared, as in a uniform
random split). Gold queries that fail or exceed the timeout on their own database are
excluded before sampling, by a fixed rule.
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lamarck.domains.text2sql import data, scoring

SEED = 20261118
SIZES = {"spider_evolve": 60, "spider_heldout": 120, "bird_evolve": 60, "bird_heldout": 120}


def main() -> None:
    tasks = data.tasks()
    excluded = []
    valid = {}
    for tid, t in sorted(tasks.items()):
        g = scoring.run_gold(data.db_file(t), t["gold_sql"])
        if not g.ok or g.truncated:
            excluded.append({"id": tid, "error": g.error or "truncated"})
        else:
            valid[tid] = t
    rng = random.Random(SEED)
    spider_dbs = sorted({t["db_id"] for t in valid.values() if t["dataset"] == "spider"})
    rng.shuffle(spider_dbs)
    evo_dbs, held_dbs = sorted(spider_dbs[:10]), sorted(spider_dbs[10:])
    pool = lambda ds, dbs=None: sorted(  # noqa: E731
        tid for tid, t in valid.items() if t["dataset"] == ds and (dbs is None or t["db_id"] in dbs)
    )
    out = {
        "spider_evolve": sorted(rng.sample(pool("spider", evo_dbs), SIZES["spider_evolve"])),
        "spider_heldout": sorted(rng.sample(pool("spider", held_dbs), SIZES["spider_heldout"])),
    }
    bird = pool("bird")
    rng.shuffle(bird)
    out["bird_evolve"] = sorted(bird[: SIZES["bird_evolve"]])
    out["bird_heldout"] = sorted(bird[SIZES["bird_evolve"] : SIZES["bird_evolve"] + SIZES["bird_heldout"]])
    stats = {name: dict(Counter(tasks[i]["db_id"] for i in ids)) for name, ids in out.items()}
    stats["bird_difficulty"] = {
        n: dict(Counter(tasks[i]["difficulty"] for i in out[n])) for n in ("bird_evolve", "bird_heldout")
    }
    doc = {
        "seed": SEED,
        "spider_evolve_dbs": evo_dbs,
        "spider_heldout_dbs": held_dbs,
        "excluded_gold_failures": excluded,
        "splits": out,
        "stats": stats,
    }
    path = data.split_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1) + "\n")
    print(path, hashlib.sha256(path.read_bytes()).hexdigest())
    print(json.dumps({k: len(v) for k, v in out.items()}), "excluded:", len(excluded))


if __name__ == "__main__":
    main()
