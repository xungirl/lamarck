"""Sandbox escape probes and limits (needs Docker; skipped when no daemon is reachable)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lamarck.config import RunConfig
from lamarck.stack import Stack

REPO = Path(__file__).resolve().parents[1]


def docker_up() -> bool:
    return (
        shutil.which("docker") is not None
        and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


pytestmark = [pytest.mark.docker, pytest.mark.skipif(not docker_up(), reason="no Docker daemon")]


@pytest.fixture(scope="module")
def dstack() -> Stack:
    from lamarck.stack import docker_stack

    root = REPO / "runs" / "test-docker"
    shutil.rmtree(root / "runs", ignore_errors=True)
    shutil.rmtree(root / "cache", ignore_errors=True)
    st = docker_stack(root, keyfile=None, fake=True, warm=1, memory_mb=256)
    yield st
    st.backend.close()


def probe(st: Stack, data: Path) -> dict[str, dict[str, object]]:
    r = st.backend.exec_probe({"evolve_db": data}, ["python", "-m", "lamarck.sandbox.probes"])  # type: ignore[attr-defined]
    return json.loads(r.stdout)


def test_escape_probes_fail_and_gateway_works(dstack: Stack, tmp_path: Path) -> None:
    data = REPO / "runs" / "test-docker" / "probe-data"
    data.mkdir(parents=True, exist_ok=True)
    res = probe(dstack, data)
    allowed = {"gateway_healthz_allowed"}
    informational = {"data_listing", "kernel"}
    for name, r in res.items():
        if name in informational:
            continue
        assert r["succeeded"] == (name in allowed), f"{name}: {r}"
    assert res["data_listing"]["detail"] == "evolve_db"  # only this split's data is mounted
    assert "gvisor" in str(res["kernel"]["detail"]) or dstack.backend.runtime == "runc"  # type: ignore[attr-defined]
    (REPO / "results").mkdir(exist_ok=True)
    (REPO / "results" / "sandbox_probes.json").write_text(
        json.dumps({"runtime": dstack.backend.runtime, "probes": res}, indent=1)  # type: ignore[attr-defined]
    )


def test_memory_limit_kills_allocation(dstack: Stack) -> None:
    r = dstack.backend.exec_probe(
        {}, ["python", "-c", "x = bytearray(1024 * 1024 * 1024); print('allocated')"]
    )  # type: ignore[attr-defined]
    assert "allocated" not in r.stdout


def test_pid_limit_bounds_fork_bombs(dstack: Stack) -> None:
    code = (
        "import os, time\n"
        "n = 0\n"
        "try:\n"
        "    for _ in range(1000):\n"
        "        if os.fork() == 0:\n"
        "            time.sleep(3); os._exit(0)\n"
        "        n += 1\n"
        "except OSError:\n"
        "    pass\n"
        "print('started', n)\n"
    )
    r = dstack.backend.exec_probe({}, ["python", "-c", code])  # type: ignore[attr-defined]
    # Either fork() starts failing below 1000 processes, or (gVisor) the whole sandbox is torn down
    # when its sentry hits the pid cgroup limit. Both bound the damage to this one container.
    assert "started 1000" not in r.stdout
    assert r.stdout.strip() or r.returncode != 0


def test_timeout_is_enforced(dstack: Stack) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        dstack.backend.exec_probe({}, ["python", "-c", "while True: pass"], timeout=5)  # type: ignore[attr-defined]


def test_toy_run_in_docker_sandbox(dstack: Stack) -> None:
    run = dstack.make_run(RunConfig(run_id="toy-docker", domain="toy", rounds=2, k=2))
    s = run.run()
    assert s["evolve_end"] >= s["evolve_start"]
    assert s["heldout_end"] is not None
