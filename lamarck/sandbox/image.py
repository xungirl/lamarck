"""Build the sandbox / gateway image from an explicit allowlist of source files.

The build context is assembled file by file, so nothing that is not listed can
end up inside a sandbox: no task files, no gold answers, no held-out store data,
no results. tests/test_sandbox_image.py checks the allowlist.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
IMAGE = "lamarck-runtime"

# Paths relative to the repository root (directories are copied recursively, *.py/*.md/*.json only).
ALLOWLIST = [
    "lamarck/__init__.py",
    "lamarck/py.typed",
    "lamarck/types.py",
    "lamarck/gateway",
    "lamarck/sandbox/__init__.py",
    "lamarck/sandbox/runner.py",
    "lamarck/sandbox/runtime_util.py",
    "lamarck/sandbox/probes.py",
    "lamarck/domains/__init__.py",
    "lamarck/domains/toy/__init__.py",
    "lamarck/domains/toy/runtime.py",
    "lamarck/domains/toy/ops.py",  # imported by the fake responders
    "lamarck/domains/text2sql/__init__.py",
    "lamarck/domains/text2sql/runtime.py",
    "lamarck/domains/text2sql/sqlexec.py",
    "lamarck/domains/supportops/__init__.py",
    "lamarck/domains/supportops/runtime.py",
    "lamarck/domains/supportops/env",  # MCP server + agent loop; store data and tasks are NOT in env/
]
FORBIDDEN_NAMES = ("tasks", "gold", "stores", "split", "prereg", "heldout")

DOCKERFILE = """FROM python:3.13-slim
RUN pip install --no-cache-dir "httpx>=0.28" "mcp>=2.3,<3" "pydantic>=2.10" "anyio>=4.6"
COPY lamarck /app/lamarck
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /tmp
"""


def context_files() -> list[Path]:
    out: list[Path] = []
    for rel in ALLOWLIST:
        p = REPO / rel
        if p.is_dir():
            out += sorted(
                q
                for q in p.rglob("*")
                if q.is_file()
                and q.suffix in {".py", ".md", ".json", ".typed"}
                and "__pycache__" not in q.parts
            )
        elif p.exists():
            out.append(p)
    return out


def assemble(ctx: Path) -> str:
    """Write the build context to `ctx`; return a content hash used as the image tag."""
    if ctx.exists():
        shutil.rmtree(ctx)
    ctx.mkdir(parents=True)
    h = hashlib.sha256(DOCKERFILE.encode())
    for f in context_files():
        rel = f.relative_to(REPO)
        dest = ctx / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
        h.update(str(rel).encode())
        h.update(f.read_bytes())
    (ctx / "Dockerfile").write_text(DOCKERFILE)
    return h.hexdigest()[:12]


def build(ctx: Path) -> str:
    """Build (or reuse) the image; returns 'lamarck-runtime:<hash>'. Safe to call from several
    processes at once: each assembles its own context directory."""
    own = ctx.with_name(f"{ctx.name}-{os.getpid()}")
    try:
        tag = f"{IMAGE}:{assemble(own)}"
        exists = subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0
        if not exists:
            subprocess.run(["docker", "build", "-q", "-t", tag, str(own)], check=True, capture_output=True)
    finally:
        shutil.rmtree(own, ignore_errors=True)
    return tag
