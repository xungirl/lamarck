"""Entry point executed INSIDE the sandbox for one evaluation job.

Layout seen by the runner (paths come from environment variables so the same
code runs in Docker and in the local test backend):

  LAMARCK_HARNESS  harness under test, read-only
  LAMARCK_JOB      job.json + tasks.jsonl (public task fields only), read-only
  LAMARCK_DATA     read-only data for this split only (databases, store template)
  LAMARCK_OUT      the only writable directory: results.jsonl (+ domain artifacts)
  LAMARCK_GATEWAY  gateway base URL;   LAMARCK_TOKEN  this run's policy grant

Gold answers and expectations are never in any of these directories.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from ..gateway.client import GatewayClient


def load_job(job_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    job = json.loads((job_dir / "job.json").read_text())
    tasks = [json.loads(line) for line in (job_dir / "tasks.jsonl").read_text().splitlines() if line.strip()]
    return job, tasks


def main() -> int:
    harness = Path(os.environ["LAMARCK_HARNESS"])
    job_dir = Path(os.environ["LAMARCK_JOB"])
    data = Path(os.environ.get("LAMARCK_DATA", "/data"))
    out = Path(os.environ["LAMARCK_OUT"])
    client = GatewayClient(os.environ["LAMARCK_GATEWAY"], os.environ["LAMARCK_TOKEN"])
    job, tasks = load_job(job_dir)
    t0 = time.time()
    try:
        runtime = importlib.import_module(job["runtime"])
        runtime.run_job(job=job, tasks=tasks, harness_dir=harness, data_dir=data, out_dir=out, client=client)
        status: dict[str, Any] = {"ok": True}
    except Exception as exc:  # the harness is untrusted; report, do not crash silently
        status = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-3000:],
        }
    status["wall_s"] = round(time.time() - t0, 2)
    (out / "status.json").write_text(json.dumps(status))
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
