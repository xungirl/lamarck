from __future__ import annotations

import pytest

from lamarck.domains.toy.domain import ToyDomain
from lamarck.engine import critic
from lamarck.engine.proposer import ProposalError, extract_json, parse_proposal


def test_extract_json_tolerates_fences_and_braces_in_strings() -> None:
    text = 'Here:\n```json\n{"a": "x { y }", "b": {"c": 1}}\n```'
    assert extract_json(text) == {"a": "x { y }", "b": {"c": 1}}


def test_parse_filters_predictions_to_failing_tasks() -> None:
    text = (
        '{"root_cause": {"cite": "P2", "name": "x"}, "what": "w", "n_independent_edits": 1,'
        ' "edits": [{"file": "rules.md", "content": "RULE a: upper"}], "predicted": ["t1", "t9"]}'
    )
    p = parse_proposal(text, "s1", ToyDomain(), failing={"t1", "t2"})
    assert p.predicted == ["t1"] and p.dropped_predictions == ["t9"] and p.cite == "P2"


@pytest.mark.parametrize("path", ["../escape.md", "/etc/passwd", "solve.py", ".hidden.md"])
def test_parse_rejects_non_editable_files(path: str) -> None:
    text = f'{{"root_cause": {{"name": "x"}}, "edits": [{{"file": "{path}", "content": "x"}}]}}'
    with pytest.raises(ProposalError):
        parse_proposal(text, "s1", ToyDomain(), failing=set())


def test_critic_checks() -> None:
    diff = "+++ b/harness/solve.py\n+import os\n+x = 1\n"
    reasons = critic.screen(diff, {"solve.py": "import os\nx = 1\n"}, 1, 1, [])
    assert any("allowlist" in r for r in reasons)
    reasons = critic.screen("+RULE color: upper\n", {"rules.md": "RULE color: upper"}, 2, 1, [])
    assert any("budget" in r for r in reasons)
    reasons = critic.screen("+if q == 'sp0042':\n", {"a.py": "x = (\n"}, 1, 3, [(r"sp\d{4}", "task id")])
    assert any("task id" in r for r in reasons) and any("compile" in r for r in reasons)
    assert critic.screen("+be careful with joins\n", {"p.md": "be careful"}, 1, 1, []) == []


def test_critic_ignores_removed_lines() -> None:
    diff = "-old line mentioning sp0042\n+new generic line\n"
    assert critic.screen(diff, {"p.md": "new generic line"}, 1, 1, [(r"sp\d{4}", "task id")]) == []
