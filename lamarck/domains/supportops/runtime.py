"""In-sandbox runtime for support-ops: the MCP tool server + agent loop from agent-eval-mcp.

For each task: copy the read-only store template (/data/store/store.db) to /tmp, start the
support-ops MCP server in-process on that copy (read-only tasks get a server without write
tools), load the harness directory (prompt, tool policy, knobs, hooks) into the bounded agent
loop, and run the request. The final database copy goes to /out/dbs/ so the host can score
state and safety; the expectations themselves never enter the sandbox.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ...gateway.client import GatewayClient, GatewayError
from ...sandbox.runtime_util import CallBudgetExhausted, PolicyEnv, run_tasks, runtime_flags
from .env.agent.harness_dir import load_harness
from .env.agent.loop import Agent
from .env.agent.providers import LLMError, TransientLLMError
from .env.agent.session import open_support_tools
from .env.agent.types import LLMResponse, Message, ToolCall, ToolSpec, Usage


class GatewayProvider:
    """LLMProvider for the agent loop, backed by the per-task PolicyEnv (gateway + call cap)."""

    name = "gateway"

    def __init__(self, policy: PolicyEnv, model: str) -> None:
        self.policy = policy
        self.model = model

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> LLMResponse:
        def call() -> LLMResponse:
            try:
                res = self.policy.chat(list(messages), tools=[t.to_openai() for t in tools] or None)
            except CallBudgetExhausted as exc:
                raise LLMError(str(exc)) from exc
            except GatewayError as exc:
                if exc.budget_exceeded or exc.status in (400, 401, 403, 404):
                    raise LLMError(str(exc)) from exc
                raise TransientLLMError(str(exc)) from exc
            calls = [
                ToolCall(id=str(tc.get("id") or f"call_{i}"), name=tc["function"]["name"],
                         arguments=tc["function"].get("arguments") or "{}")
                for i, tc in enumerate(res.tool_calls)
                if tc.get("function")
            ]  # fmt: skip
            return LLMResponse(res.content or None, calls, Usage(res.prompt_tokens, res.completion_tokens))

        return await asyncio.to_thread(call)


async def _run_one(
    task: dict[str, Any],
    job: dict[str, Any],
    harness_dir: Path,
    data_dir: Path,
    out_dir: Path,
    client: GatewayClient,
) -> dict[str, Any]:
    work = Path("/tmp") if Path("/tmp").is_dir() else out_dir
    db = work / f"lamarck-{task['id']}.db"
    shutil.copyfile(data_dir / "store" / "store.db", db)
    policy = PolicyEnv(client, job["model"], job["max_calls"], job["policy_params"])
    harness = load_harness(harness_dir)
    config = harness.agent_config(llm_timeout_s=120.0, tool_timeout_s=15.0, run_timeout_s=300.0)
    async with open_support_tools(db, allow_writes=not task.get("read_only", False)) as tools:
        result = await Agent(GatewayProvider(policy, job["model"]), tools, config).run(task["prompt"])
    (out_dir / "dbs").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(db, out_dir / "dbs" / f"{task['id']}.db")
    db.unlink(missing_ok=True)
    return {
        "final_answer": result.final_answer,
        "stop_reason": result.stop_reason,
        "steps": result.steps,
        "llm_calls": result.llm_calls,
        "tool_calls": [
            {
                "step": c.step,
                "name": c.name,
                "arguments": c.arguments,
                "is_error": c.is_error,
                "result": c.result[:600],
                "blocked": c.blocked,
                "client": c.client,
            }
            for c in result.tool_calls
        ],
        "hook_errors": result.hook_errors,
        "error": result.error or "",
        "tokens": policy.tokens,
        "calls": policy.calls,
        **runtime_flags(policy),
    }


def run_job(
    job: dict[str, Any],
    tasks: list[dict[str, Any]],
    harness_dir: Path,
    data_dir: Path,
    out_dir: Path,
    client: GatewayClient,
) -> None:
    def solve(task: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(_run_one(task, job, harness_dir, data_dir, out_dir, client))

    run_tasks(tasks, solve, out_dir, job.get("task_parallel", 6))
    (out_dir / "runtime.json").write_text(json.dumps({"tasks": len(tasks)}))
