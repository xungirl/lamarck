"""Sandbox backends: where an evaluation job's untrusted harness code runs.

  LocalBackend   a subprocess on the host. No isolation; used by the offline tests and
                 CI (fake models only), so the full pipeline runs without Docker.
  DockerBackend  one fresh container per job (lamarck.sandbox.docker_backend): read-only
                 harness and data mounts, no route anywhere except the gateway, CPU /
                 memory / pid / time limits, a warm pool of pre-started containers.

Both run the same in-sandbox entry point, lamarck.sandbox.runner.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class SandboxJob:
    job_id: str
    harness_dir: Path  # exported harness (read-only inside the sandbox)
    job_dir: Path  # job.json + tasks.jsonl (read-only inside the sandbox)
    out_dir: Path  # the only writable directory
    data_mounts: dict[str, Path] = field(default_factory=dict)  # name -> host dir, at /data/<name>
    token: str = ""  # policy grant for this run
    timeout_s: float = 1800.0
    cpus: float = 1.0
    memory_mb: int = 1024


@dataclass
class JobResult:
    ok: bool
    wall_s: float
    exit_code: int
    log_tail: str = ""
    timed_out: bool = False


class Backend(Protocol):
    name: str

    def run(self, job: SandboxJob) -> JobResult: ...

    def close(self) -> None: ...


class LocalBackend:
    """Runs the job in a host subprocess. For tests/CI with fake models only."""

    name = "local"

    def __init__(self, gateway_url: str) -> None:
        self.gateway_url = gateway_url

    def run(self, job: SandboxJob) -> JobResult:
        job.out_dir.mkdir(parents=True, exist_ok=True)
        data_view = job.out_dir.parent / f"{job.out_dir.name}.data"
        data_view.mkdir(parents=True, exist_ok=True)
        for name, host in job.data_mounts.items():
            link = data_view / name
            if not link.exists():
                link.symlink_to(host.resolve())
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(REPO_ROOT),
            "LAMARCK_HARNESS": str(job.harness_dir),
            "LAMARCK_JOB": str(job.job_dir),
            "LAMARCK_DATA": str(data_view),
            "LAMARCK_OUT": str(job.out_dir),
            "LAMARCK_GATEWAY": self.gateway_url,
            "LAMARCK_TOKEN": job.token,
        }
        t0 = time.time()
        try:
            r = subprocess.run(
                [sys.executable, "-m", "lamarck.sandbox.runner"],
                env=env,
                cwd=job.out_dir,
                capture_output=True,
                text=True,
                timeout=job.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return JobResult(False, time.time() - t0, -1, "timeout", timed_out=True)
        return JobResult(r.returncode == 0, time.time() - t0, r.returncode, (r.stdout + r.stderr)[-2000:])

    def close(self) -> None:
        return None
