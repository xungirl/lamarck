"""Shared fixtures: an offline stack (in-process gateway with fake models + subprocess backend)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lamarck.stack import Stack, local_stack


@pytest.fixture
def stack(tmp_path: Path) -> Stack:
    return local_stack(tmp_path)
