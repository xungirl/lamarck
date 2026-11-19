"""SIM retrieval extension: ranking, top-k, token budget, ids preserved."""

from __future__ import annotations

from lamarck.sim import prompts
from lamarck.sim.memory import SIM, Attempt
from lamarck.sim.retrieval import bm25_scores, rank, retrieve, tokenize


def att(what: str) -> Attempt:
    return Attempt(0, "s1", True, False, what, "solve.py", ["a"], [], ["a"], 1, 2, "spider/qwen")


def mem() -> SIM:
    s = SIM()
    s.update(
        att("verify join keys against foreign keys before answering"),
        cite=None,
        name="wrong join columns",
        opened_round=0,
    )
    s.update(
        att("normalize date strings with strftime"), cite=None, name="date format mismatch", opened_round=0
    )
    s.update(
        att("match string literals case-insensitively after probing values"),
        cite=None,
        name="literal value guessed",
        opened_round=1,
    )
    return s


def test_bm25_prefers_matching_document() -> None:
    docs = [tokenize("join keys foreign"), tokenize("dates strftime")]
    a, b = bm25_scores(docs, tokenize("which join keys"))
    assert a > b == 0.0


def test_rank_and_top_k() -> None:
    s = mem()
    top = rank(s, "the query used the wrong join columns between tables")
    assert top[0][0] == "P1"
    r = retrieve(s, "the date format did not match", top_k=1, token_budget=10_000)
    assert [p.id for p in r.problems] == ["P2"]  # original ids are kept for citations


def test_token_budget_is_respected() -> None:
    s = mem()
    small = retrieve(s, "join date literal", top_k=3, token_budget=1)
    assert len(small.problems) == 1  # always at least the best entry
    big = retrieve(s, "join date literal", top_k=3, token_budget=100_000)
    assert len(big.problems) == 3
    assert prompts.approx_tokens(prompts.render(big, "full")) > prompts.approx_tokens(
        prompts.render(small, "full")
    )
