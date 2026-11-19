"""Sandbox start-up latency: cold container (create + exec) vs warm pool (exec only), runsc vs runc.

Each sample runs `python -c pass` inside a fresh sandbox with the production flags
(read-only root, internal network, limits). Run on an otherwise idle machine.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lamarck.sandbox import docker_backend, image

ROOT = Path(__file__).resolve().parents[1] / "runs" / "bench"


def sample(be: docker_backend.DockerBackend, warm: bool, n: int) -> list[float]:
    out = []
    for _ in range(n):
        if warm:
            w = be._create({})
            t0 = time.perf_counter()
            docker_backend.docker("exec", w.name, "python", "-c", "pass")
            out.append(time.perf_counter() - t0)
            be._destroy(w)
        else:
            t0 = time.perf_counter()
            w = be._create({})
            docker_backend.docker("exec", w.name, "python", "-c", "pass")
            out.append(time.perf_counter() - t0)
            be._destroy(w)
    return out


def main() -> None:
    tag = image.build(ROOT / "ctx")
    res: dict[str, object] = {"image": tag, "n": 15}
    for runtime in ("runsc", "runc"):
        be = docker_backend.DockerBackend(tag, ROOT / "slots", use_runsc=(runtime == "runsc"), warm=0)
        assert be.runtime == runtime, f"{runtime} unavailable"
        for mode in ("cold", "warm"):
            xs = sample(be, mode == "warm", 15)
            res[f"{runtime}_{mode}_s"] = {
                "median": round(statistics.median(xs), 3),
                "p90": round(sorted(xs)[13], 3),
                "min": round(min(xs), 3),
            }
    (Path(__file__).resolve().parents[1] / "results" / "sandbox_bench.json").write_text(
        json.dumps(res, indent=1)
    )
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
