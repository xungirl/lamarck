"""Escape probes, executed INSIDE a sandbox container. Prints one JSON object.

Each probe tries something a malicious or buggy harness might try and records
whether it succeeded. The test (tests/test_sandbox_docker.py) asserts that every
escape fails while the one allowed path (the gateway) works.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.request
from typing import Any


def attempt(fn: Any) -> dict[str, Any]:
    try:
        out = fn()
        return {"succeeded": True, "detail": str(out)[:200]}
    except Exception as exc:
        return {"succeeded": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}


def tcp(host: str, port: int) -> str:
    with socket.create_connection((host, port), timeout=3):
        return "connected"


def write(path: str) -> str:
    with open(path, "w") as f:
        f.write("x")
    return "written"


def main() -> None:
    gateway = os.environ.get("PROBE_GATEWAY", "http://lamarck-gateway:8080")
    results = {
        "egress_ip_1.1.1.1:443": attempt(lambda: tcp("1.1.1.1", 443)),
        "egress_ip_8.8.8.8:53": attempt(lambda: tcp("8.8.8.8", 53)),
        "egress_dashscope_https": attempt(
            lambda: urllib.request.urlopen("https://dashscope.aliyuncs.com", timeout=4).status
        ),
        "dns_external_name": attempt(lambda: socket.gethostbyname("example.com")),
        "metadata_169.254.169.254": attempt(lambda: tcp("169.254.169.254", 80)),
        "host_gateway_published_port": attempt(lambda: tcp("host.docker.internal", 18080)),
        "colima_host_192.168.5.2": attempt(lambda: tcp("192.168.5.2", 22)),
        "write_harness_mount": attempt(lambda: write("/harness/pwned.txt")),
        "write_job_mount": attempt(lambda: write("/job/pwned.txt")),
        "write_data_mount": attempt(lambda: write("/data/pwned.txt")),
        "write_root_fs": attempt(lambda: write("/etc/pwned.txt")),
        "docker_socket_present": attempt(lambda: os.stat("/var/run/docker.sock")),
        "raw_socket_cap_net_raw": attempt(
            lambda: socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        ),
        "chown_as_root": attempt(lambda: os.chown("/tmp", 0, 0)),
        "read_host_home": attempt(lambda: os.listdir("/Users")),
        "gateway_healthz_allowed": attempt(
            lambda: urllib.request.urlopen(gateway + "/healthz", timeout=4).status
        ),
        "gateway_admin_without_secret": attempt(
            lambda: (
                urllib.request.urlopen(urllib.request.Request(gateway + "/admin/ledger"), timeout=4).status
            )
        ),
        "uid": {"succeeded": os.getuid() == 0, "detail": f"uid={os.getuid()}"},
        "data_listing": {
            "succeeded": False,
            "detail": ",".join(sorted(os.listdir("/data"))) if os.path.isdir("/data") else "",
        },
        "kernel": {"succeeded": False, "detail": os.uname().release},
    }
    json.dump(results, sys.stdout)


if __name__ == "__main__":
    main()
