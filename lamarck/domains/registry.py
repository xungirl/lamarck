"""Name -> Domain instance."""

from __future__ import annotations

from .base import Domain


def get_domain(name: str) -> Domain:
    if name == "toy":
        from .toy.domain import ToyDomain

        return ToyDomain()
    if name == "text2sql":
        from .text2sql.domain import Text2SQLDomain

        return Text2SQLDomain()
    if name == "supportops":
        from .supportops.domain import SupportOpsDomain

        return SupportOpsDomain()
    raise KeyError(f"unknown domain {name!r}")
