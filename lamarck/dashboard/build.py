"""Build the static experiment dashboard (one HTML file, zero runtime dependencies) from results/.

    uv run python -m lamarck.dashboard.build      -> docs/dashboard/index.html

Data is embedded as JSON; charts are drawn by ~300 lines of vanilla JS in template.html.
Roadmap statuses come from git tags: stage-N exists -> done.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "results"
OUT = REPO / "docs" / "dashboard" / "index.html"

PITCH = (
    "lamarck re-implements Self-Improvement Memory (SIM) on a regularized RSI engine: an LLM proposer edits an "
    "agent's harness, candidates are git branches evaluated in gVisor sandboxes behind a budgeted LLM gateway, and a "
    "memory of root causes, predictions and outcomes is carried when the benchmark or the model changes."
)

STAGES = [
    (
        "Engine core",
        "Candidates as git branches; RRSI regularizers: edit budget, noise floor, cost rule, critic, prune set.",
    ),
    (
        "Self-Improvement Memory",
        "Problem entries keyed by root cause, predictions sealed before evaluation, deterministic merge, App. D.3 JSON.",
    ),
    (
        "LLM gateway",
        "Per-run grants, call caps, token buckets, retries, audit log, and an RMB spend ledger with a hard cap.",
    ),
    (
        "Sandboxed evaluation",
        "Fresh gVisor container per job, internal-only network to the gateway, read-only data, escape probes.",
    ),
    (
        "Domains + pre-registration",
        "Text-to-SQL (Spider → BIRD) and support-ops (MCP tools); splits hashed in PREREG.md.",
    ),
    (
        "Source RSI runs",
        "Budget-revised plan: 7 text-to-SQL (3 primary + 4 added) and 1 support-ops source runs build harnesses and SIM.",
    ),
    (
        "CRSI experiments",
        "Four memory conditions on Spider → BIRD and a support-ops policy shift; cross-model with simulated models.",
    ),
    (
        "Paired study",
        "Full SIM vs Harness Transfer from the same evolved harness: 3 pairs primary, 7 pairs secondary.",
    ),
    (
        "Retrieval ablation",
        "Pooled SIM of all source runs: full context vs top-k retrieved under a token budget.",
    ),
    ("Dashboard", "This page: lineage, curves, SIM explorer, cost, roadmap."),
    ("CI + docs", "pytest, mypy --strict, ruff, flakiness runs, GitHub Actions, reproduce scripts."),
    ("Technical report", "REPORT.md with statistics and threats to validity."),
]


def tags() -> set[str]:
    out = subprocess.run(["git", "tag"], cwd=REPO, capture_output=True, text=True).stdout
    return set(out.split())


def load(path: Path, default: Any) -> Any:
    return json.loads(path.read_text()) if path.exists() else default


def run_payload(d: Path, summary: dict[str, Any], source: str | None) -> dict[str, Any]:
    hist = (
        [json.loads(x) for x in (d / "history.jsonl").read_text().splitlines()]
        if (d / "history.jsonl").exists()
        else []
    )
    s = dict(summary)
    s["source"] = source
    keep = (
        "round",
        "sample",
        "selected",
        "gate",
        "score",
        "d_score",
        "cite",
        "root_cause",
        "what",
        "decision",
    )
    return {
        "summary": s,
        "history": [{k: r.get(k) for k in keep} for r in hist],
        "sim": load(d / "sim.json", {"problems": []}),
    }


def build() -> Path:
    from ..experiments.plan import build_plan

    plan = build_plan()
    summary = load(
        RESULTS / "summary.json", {"paired": {"pairs": []}, "conditions": {}, "ledger": {}, "runs": {}}
    )
    runs = {}
    for name, s in summary.get("runs", {}).items():
        d = RESULTS / "runs" / name
        if d.exists():
            runs[name] = run_payload(d, s, plan[name].source if name in plan else None)
    cost = load(RESULTS / "cost.json", {"note": "", "models": [], "groups": []})
    have = tags()
    roadmap = [
        {"label": f"Stage {i}", "title": t, "text": txt, "status": "done" if f"stage-{i}" in have else "todo"}
        for i, (t, txt) in enumerate(STAGES, start=1)
    ]
    data = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "pitch": PITCH,
        "summary": summary,
        "runs": runs,
        "cost": cost,
        "roadmap": roadmap,
    }
    html = (Path(__file__).parent / "template.html").read_text()
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html.replace("/*__DATA__*/null", blob))
    return OUT


if __name__ == "__main__":
    print(build())
