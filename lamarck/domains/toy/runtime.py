"""In-sandbox runtime for the toy domain: one policy call per task, rules.md as the system prompt."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...gateway.client import GatewayClient
from ...sandbox.runtime_util import PolicyEnv, read_config, read_text, run_tasks


def run_job(
    job: dict[str, Any],
    tasks: list[dict[str, Any]],
    harness_dir: Path,
    data_dir: Path,
    out_dir: Path,
    client: GatewayClient,
) -> None:
    system = read_text(harness_dir, "rules.md")
    cfg = read_config(harness_dir)

    def solve(task: dict[str, Any]) -> dict[str, Any]:
        env = PolicyEnv(client, job["model"], job["max_calls"], job["policy_params"])
        user = f"kind={task['kind']} item={task['item']}"
        res = env.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=int(cfg.get("max_tokens", 64)),
        )
        return {"answer": res.content.strip(), "tokens": env.tokens, "calls": env.calls}

    run_tasks(tasks, solve, out_dir, job.get("task_parallel", 4))
