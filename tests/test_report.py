"""`make report` on toy runs named like planned runs: tables, paired statistics, RMB, not-run list."""

from __future__ import annotations

import json
from pathlib import Path

from lamarck.config import RunConfig
from lamarck.dashboard import build as dashboard
from lamarck.experiments import report
from lamarck.stack import Stack


def toy(run_id: str, memory: str = "full", target: bool = False) -> RunConfig:
    if target:
        return RunConfig(run_id=run_id, domain="toy", evolve_split="t_evolve", heldout_split="t_heldout",
                         rounds=1, k=2, memory=memory)  # type: ignore[arg-type]  # fmt: skip
    return RunConfig(run_id=run_id, domain="toy", rounds=2, k=2, seed=int(run_id[-1]))


def test_report_from_toy_runs(stack: Stack, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for i in (1, 2):
        src = stack.make_run(toy(f"sql-src-{i}"))
        src.run()
        stack.make_run(toy(f"sql-full-{i}", "full", True), source=src).run()
        stack.make_run(toy(f"sql-transfer-{i}", "none", True), source=src).run()
    spend = tmp_path / "spend.json"
    spend.write_text(json.dumps({"cap_rmb": 9.5, "spent_rmb": 0.25, "calls": 10, "failed": 0,
                                 "by": {"run:sql-src-1": {"rmb": 0.1}, "role:policy": {"calls": 10, "prompt_tokens": 5,
                                        "cached_tokens": 0, "completion_tokens": 1, "rmb": 0.25}}}))  # fmt: skip
    out = tmp_path / "results"
    agg = report.build(stack.root, out, spend)
    assert [p["source"] for p in agg["paired"]["pairs"]] == [1, 2]
    assert agg["paired"]["stats"]["n"] == 2
    assert {r["run"] for r in agg["source"]} == {"sql-src-1", "sql-src-2"}
    assert agg["runs"]["sql-src-1"]["cost"]["rmb"] == 0.1
    assert agg["spend"]["spent_rmb"] == 0.25
    assert any(r["run"] == "sql-full-3" and r["state"] == "not started" for r in agg["not_run"])
    md = (out / "RESULTS.md").read_text()
    assert "Paired study" in md and "RMB 0.2500" in md and "descriptive" in md
    assert (out / "runs" / "sql-full-1" / "final_harness").is_dir()
    # the dashboard embeds the same summary
    monkeypatch.setattr(dashboard, "RESULTS", out)
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "index.html")
    html = dashboard.build().read_text()
    assert '"sql-full-1"' in html and "/*__DATA__*/null" not in html
