"""The toy domain's transformations (shared by the domain and the fake policy)."""

from __future__ import annotations

from collections.abc import Callable

OPS: dict[str, Callable[[str], str]] = {
    "upper": lambda s: s.upper(),
    "reverse": lambda s: s[::-1],
    "double": lambda s: s + s,
    "title": lambda s: s.title(),
    "sort": lambda s: "".join(sorted(s)),
}
