"""SIM unit and property tests: the update/merge invariants of App. D.1 and the D.3 format."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lamarck.sim import prompts
from lamarck.sim.memory import SIM, Attempt, files_field, normalize_root_cause, truncate


def att(
    rnd: int = 0,
    sample: str = "s1",
    predicted: tuple[str, ...] = ("a", "b"),
    fixed: tuple[str, ...] = ("a",),
    domain: str = "spider/qwen",
) -> Attempt:
    return Attempt(
        rnd,
        sample,
        True,
        False,
        "edit",
        "p.md",
        list(predicted),
        list(fixed),
        sorted(set(predicted) - set(fixed)),
        3,
        10,
        domain,
    )


def test_paper_record_format_round_trips() -> None:
    sim = SIM()
    sim.update(att(6), cite=None, name="premature completion without execution verification", opened_round=6)
    d = sim.to_json()
    assert set(d) == {"next_id", "carried_from", "aliases", "problems"}
    assert set(d["problems"][0]) == {"id", "name", "opened_round", "attempts"}
    assert set(d["problems"][0]["attempts"][0]) == {
        "round",
        "sample",
        "accepted",
        "backfilled",
        "what",
        "files",
        "predicted",
        "fixed",
        "missed",
        "passed",
        "total",
        "domain",
    }
    assert SIM.from_json(json.loads(json.dumps(d))).to_json() == d


def test_update_cites_creates_and_matches_identical_text() -> None:
    sim = SIM()
    p1 = sim.update(att(0), cite=None, name="Schema not shown", opened_round=0)
    p2 = sim.update(att(1), cite=p1, name="a different wording", opened_round=1)
    p3 = sim.update(
        att(2), cite=None, name="  schema NOT shown ", opened_round=2
    )  # identical text after normalization
    p4 = sim.update(att(3), cite="P99", name="joins wrong", opened_round=3)  # unknown cite -> new entry
    assert p1 == p2 == p3 == "P1" and p4 == "P2"
    assert len(sim.get("P1").attempts) == 3  # type: ignore[union-attr]
    assert sim.next_id == 3


def test_merge_keeps_every_attempt_and_records_alias() -> None:
    sim = SIM.from_json(
        {
            "next_id": 7,
            "carried_from": None,
            "aliases": {},
            "problems": [
                {"id": "P5", "name": "x", "opened_round": 2, "attempts": []},
                {"id": "P6", "name": "X ", "opened_round": 1, "attempts": []},
            ],
        }
    )
    sim.problems[0].attempts.append(att(2))
    sim.problems[1].attempts.append(att(1))
    assert sim.merge_identical() == [("P6", "P5")]
    assert sim.aliases == {"P6": "P5"} and sim.resolve("P6") == "P5"
    assert sim.n_attempts == 2 and sim.problems[0].opened_round == 1
    # citing the merged id still reaches the surviving entry
    assert sim.update(att(3), cite="P6", name="", opened_round=3) == "P5"
    sim.check_invariants()


def test_conditions() -> None:
    sim = SIM()
    sim.update(att(0), cite=None, name="r1", opened_round=0)
    carried = sim.carried("src-1", "spider/qwen", "tgt-1")
    assert carried.carried_from == {"arm": "src-1", "domain": "spider/qwen", "into": "tgt-1"}
    assert sim.carried_from is None  # carrying copies
    po = carried.problems_only()
    assert [p.name for p in po.problems] == ["r1"] and po.n_attempts == 0
    assert "attempts" not in prompts.render(po, "problems") and "P1: r1" in prompts.render(po, "problems")
    assert prompts.render(carried, "none") == ""
    full = prompts.render(carried, "full")
    assert "SIM JSON" in full and '"predicted"' in full and "Check the domain, benchmark" in full


def test_attempt_validation() -> None:
    with pytest.raises(ValueError):
        Attempt(0, "s1", True, False, "w", "f", ["a"], ["a"], ["a"], 1, 2, "d")  # fixed and missed overlap
    with pytest.raises(ValueError):
        Attempt(0, "s1", True, False, "w", "f", ["a"], ["b"], [], 1, 2, "d")  # fixed not predicted
    with pytest.raises(ValueError):
        Attempt(0, "s1", True, False, "w", "f", [], [], [], 3, 2, "d")


def test_helpers() -> None:
    assert files_field(["c.py", "a.py", "b.md", "d.json"]) == "a.py, b.md, c.py +1 more"
    assert truncate("x" * 400).endswith("...") and len(truncate("x" * 400)) == 280
    assert normalize_root_cause("  A  b\n") == "a b"


def test_pooled_memory_merges_identical_root_causes() -> None:
    a, b = SIM(), SIM()
    a.update(att(0, domain="r1"), cite=None, name="schema missing", opened_round=0)
    b.update(att(0, domain="r2"), cite=None, name="Schema missing", opened_round=0)
    b.update(att(1, domain="r2"), cite=None, name="bad joins", opened_round=1)
    pool = SIM.pooled([a, b], ["r1", "r2"])
    pool.check_invariants()
    assert len(pool.problems) == 2 and pool.n_attempts == 3
    assert pool.resolve("r2:P1") == pool.resolve("r1:P1")


# ----------------------------------------------------------------- properties
NAMES = st.sampled_from(["schema", "Schema", "joins", "JOINS ", "dates", "nulls", "limit"])


@st.composite
def ops(draw: st.DrawFn) -> list[tuple[str | None, str]]:
    n = draw(st.integers(1, 25))
    out = []
    for _ in range(n):
        cite = draw(st.one_of(st.none(), st.sampled_from([f"P{i}" for i in range(1, 9)])))
        out.append((cite, draw(NAMES)))
    return out


@settings(max_examples=200, deadline=None)
@given(ops())
def test_update_invariants(sequence: list[tuple[str | None, str]]) -> None:
    sim = SIM()
    for i, (cite, name) in enumerate(sequence):
        before = sim.n_attempts
        pid = sim.update(att(i), cite=cite, name=name, opened_round=i)
        assert sim.n_attempts == before + 1  # exactly one attempt appended, nothing dropped
        assert sim.get(pid) is not None
        if cite is not None and sim.resolve(cite) is not None and sim.resolve(cite) != pid:
            pytest.fail("a valid citation must be honoured")
        sim.check_invariants()
    # serialisation is lossless
    assert SIM.from_json(sim.to_json()).to_json() == sim.to_json()


@settings(max_examples=200, deadline=None)
@given(st.lists(NAMES, min_size=1, max_size=15), st.randoms(use_true_random=False))
def test_merge_is_deterministic_idempotent_and_lossless(names: list[str], rnd: object) -> None:
    raw = {
        "next_id": len(names) + 1,
        "carried_from": None,
        "aliases": {},
        "problems": [
            {
                "id": f"P{i + 1}",
                "name": n,
                "opened_round": i,
                "attempts": [json.loads(json.dumps(att(i).__dict__))],
            }
            for i, n in enumerate(names)
        ],
    }
    a, b = SIM.from_json(json.loads(json.dumps(raw))), SIM.from_json(json.loads(json.dumps(raw)))
    a.merge_identical()
    b.merge_identical()
    assert a.to_json() == b.to_json()  # deterministic
    snapshot = json.dumps(a.to_json())
    assert a.merge_identical() == [] and json.dumps(a.to_json()) == snapshot  # idempotent
    assert a.n_attempts == len(names)  # lossless
    assert len(a.problems) == len({normalize_root_cause(n) for n in names})
    a.check_invariants()
