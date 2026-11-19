"""Docker sandbox backend (Docker via colima; gVisor `runsc` runtime when available).

Isolation per evaluation job:

  * a FRESH container per job, never reused: nothing a harness leaves behind (files in
    /tmp, background processes) can reach the next job. A warm pool of pre-started
    containers per mount profile hides the start-up latency;
  * gVisor (`--runtime runsc`): the container's syscalls go to a user-space kernel, not
    the VM's kernel (falls back to runc, recorded in the job result, if runsc is missing);
  * read-only root filesystem, read-only harness/job/data mounts, one writable /out,
    tmpfs /tmp; non-root user (nobody), all capabilities dropped, no-new-privileges;
  * CPU, memory, pid limits; a wall-clock timeout enforced from the host;
  * network: only the `lamarck-sbx` network, created with --internal (no route out of
    the Docker host). The gateway container is the only other member of that network, so
    the sandbox can reach the gateway and nothing else;
  * data mounts per split: an evolve-set job mounts only the evolve databases / store,
    never held-out data, and never any gold answers (those are not files the sandbox
    could mount: they stay in host memory in Domain.score).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .backends import JobResult, SandboxJob

NETWORK = "lamarck-sbx"
GATEWAY_NAME = "lamarck-gateway"
GATEWAY_PORT = 18080  # published on the host's 127.0.0.1 only


def docker(*args: str, check: bool = True, timeout: float | None = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=timeout)


def runsc_available() -> bool:
    r = docker("info", "--format", "{{json .Runtimes}}", check=False)
    return r.returncode == 0 and "runsc" in r.stdout


def ensure_network() -> None:
    if docker("network", "inspect", NETWORK, check=False).returncode != 0:
        docker("network", "create", "--internal", NETWORK)


def gateway_ip() -> str:
    """The gateway's address on the sandbox network. gVisor's netstack does not reach Docker's
    embedded DNS (127.0.0.11), so sandboxes get the name through --add-host (/etc/hosts)."""
    fmt = '{{(index .NetworkSettings.Networks "' + NETWORK + '").IPAddress}}'
    return docker("inspect", "-f", fmt, GATEWAY_NAME).stdout.strip()


def start_gateway(
    image: str,
    state_dir: Path,
    keyfile: Path | None,
    fake: bool = True,
    spend_file: Path | None = None,
    spend_cap: float = 9.50,
) -> str:
    """(Re)start the gateway container; returns the host URL. The key file is mounted read-only
    into the gateway only; sandboxes never see it. With a key, the RMB spend ledger's directory is
    mounted read-write at /budget (the gateway refuses to start upstream without one)."""
    if keyfile is not None and spend_file is None:
        raise ValueError("an upstream gateway needs a spend ledger file")
    ensure_network()
    docker("rm", "-f", GATEWAY_NAME, check=False)
    state_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "run", "-d", "--name", GATEWAY_NAME, "--restart", "unless-stopped",
        "--network", "bridge", "-p", f"127.0.0.1:{GATEWAY_PORT}:8080",
        "-v", f"{state_dir.resolve()}:/state",
        "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
    ]  # fmt: skip
    cmd = ["python", "-m", "lamarck.gateway.server", "--state", "/state", "--port", "8080"]
    if keyfile is not None:
        args += ["-v", f"{keyfile.resolve()}:/secrets/provider.env:ro"]
        cmd += ["--keyfile", "/secrets/provider.env"]
    if spend_file is not None:
        spend_file.parent.mkdir(parents=True, exist_ok=True)
        args += ["-v", f"{spend_file.parent.resolve()}:/budget"]
        cmd += ["--spend-file", f"/budget/{spend_file.name}", "--spend-cap", str(spend_cap)]
    if fake:
        cmd.append("--fake")
    docker(*args, image, *cmd)
    docker("network", "connect", NETWORK, GATEWAY_NAME)
    url = f"http://127.0.0.1:{GATEWAY_PORT}"
    for _ in range(100):
        try:
            import urllib.request

            urllib.request.urlopen(url + "/healthz", timeout=1)
            return url
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("gateway container did not become healthy")


@dataclass
class _Warm:
    name: str
    slot: Path


@dataclass
class DockerBackend:
    image: str
    slots_root: Path
    gateway_url_in_sandbox: str = f"http://{GATEWAY_NAME}:8080"
    use_runsc: bool = True
    warm: int = 2  # pre-started containers kept per mount profile
    cpus: float = 1.0
    memory_mb: int = 1024
    pids: int = 256
    name: str = "docker"
    _pools: dict[tuple[tuple[str, str], ...], list[_Warm]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        ensure_network()
        self.runtime = "runsc" if (self.use_runsc and runsc_available()) else "runc"
        self.gateway_host = gateway_ip()
        self.slots_root.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------- containers
    def _create(self, mounts: dict[str, Path]) -> _Warm:
        name = f"lamarck-job-{uuid.uuid4().hex[:10]}"
        slot = self.slots_root / name
        for sub in ("harness", "job", "out"):
            (slot / sub).mkdir(parents=True, exist_ok=True)
        os.chmod(slot / "out", 0o777)
        args = [
            "run", "-d", "--name", name, "--runtime", self.runtime,
            "--network", NETWORK, "--read-only", "--tmpfs", "/tmp:size=256m,mode=1777",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", str(self.pids), "--memory", f"{self.memory_mb}m", "--memory-swap", f"{self.memory_mb}m",
            "--cpus", str(self.cpus), "--user", "65534:65534",
            "--add-host", f"{GATEWAY_NAME}:{self.gateway_host}",
            "-v", f"{slot / 'harness'}:/harness:ro", "-v", f"{slot / 'job'}:/job:ro", "-v", f"{slot / 'out'}:/out:rw",
        ]  # fmt: skip
        for mname, host in sorted(mounts.items()):
            args += ["-v", f"{host.resolve()}:/data/{mname}:ro"]
        docker(*args, self.image, "sleep", "infinity")
        return _Warm(name, slot)

    def _take(self, mounts: dict[str, Path]) -> _Warm:
        key = tuple(sorted((k, str(v)) for k, v in mounts.items()))
        with self._lock:
            pool = self._pools.setdefault(key, [])
            w = pool.pop() if pool else None
        if w is None:
            w = self._create(mounts)
        threading.Thread(target=self._refill, args=(key, mounts), daemon=True).start()
        return w

    def _refill(self, key: tuple[tuple[str, str], ...], mounts: dict[str, Path]) -> None:
        with self._lock:
            need = self.warm - len(self._pools.get(key, []))
        for _ in range(max(0, need)):
            w = self._create(mounts)
            with self._lock:
                self._pools.setdefault(key, []).append(w)

    def _destroy(self, w: _Warm) -> None:
        docker("rm", "-f", w.name, check=False)
        shutil.rmtree(w.slot, ignore_errors=True)

    # ------------------------------------------------------------------ run
    def run(self, job: SandboxJob) -> JobResult:
        t0 = time.time()
        w = self._take(job.data_mounts)
        try:
            shutil.copytree(job.harness_dir, w.slot / "harness", dirs_exist_ok=True)
            shutil.copytree(job.job_dir, w.slot / "job", dirs_exist_ok=True)
            env = [
                "-e", "LAMARCK_HARNESS=/harness", "-e", "LAMARCK_JOB=/job", "-e", "LAMARCK_DATA=/data",
                "-e", "LAMARCK_OUT=/out", "-e", f"LAMARCK_GATEWAY={self.gateway_url_in_sandbox}",
                "-e", f"LAMARCK_TOKEN={job.token}",
            ]  # fmt: skip
            try:
                r = docker(
                    "exec",
                    *env,
                    w.name,
                    "python",
                    "-m",
                    "lamarck.sandbox.runner",
                    check=False,
                    timeout=job.timeout_s,
                )
                timed_out = False
            except subprocess.TimeoutExpired:
                r, timed_out = None, True
            job.out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(w.slot / "out", job.out_dir, dirs_exist_ok=True)
            if r is None:
                return JobResult(False, time.time() - t0, -1, f"timeout after {job.timeout_s}s", timed_out)
            return JobResult(r.returncode == 0, time.time() - t0, r.returncode, (r.stdout + r.stderr)[-2000:])
        finally:
            self._destroy(w)

    def exec_probe(
        self, mounts: dict[str, Path], argv: list[str], timeout: float = 60
    ) -> subprocess.CompletedProcess[str]:
        """Run an arbitrary command in a fresh sandbox (used by the escape probes)."""
        w = self._take(mounts)
        try:
            return docker("exec", w.name, *argv, check=False, timeout=timeout)
        finally:
            self._destroy(w)

    def close(self) -> None:
        with self._lock:
            pools, self._pools = self._pools, {}
        for pool in pools.values():
            for w in pool:
                self._destroy(w)
