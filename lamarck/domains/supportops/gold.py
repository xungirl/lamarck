"""Gold-trajectory replay: proves every task is solvable and that the scorers agree with its reference.

A scripted provider emits the task's gold tool calls one per step, then the gold answer; the run goes
through the real MCP server, the real agent loop (with the H0 harness) and the real scorers.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .env.agent.harness_dir import load_harness
from .env.agent.loop import Agent
from .env.agent.session import open_support_tools
from .env.agent.types import LLMResponse, Message, ToolCall, ToolSpec, Usage
from .scorers import Score, score_task, snapshot
from .seed import create_seeded_db
from .tasks import Task


class GoldProvider:
    name = "gold"
    model = "gold-replay"

    def __init__(self, task: Task) -> None:
        self.steps = [
            LLMResponse(None, [ToolCall(f"gold_{i}", g.tool, json.dumps(g.args))], Usage(1, 1), "tool_calls")
            for i, g in enumerate(task.gold)
        ] + [LLMResponse(task.gold_answer, [], Usage(1, 1))]

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> LLMResponse:
        return self.steps.pop(0) if self.steps else LLMResponse("(script exhausted)", [], Usage(1, 1))


async def _replay(task: Task, template: Path, harness_dir: Path) -> list[Score]:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "run.db"
        shutil.copyfile(template, db)
        before = snapshot(db)
        cfg = load_harness(harness_dir).agent_config()
        async with open_support_tools(db, allow_writes=not task.read_only) as tools:
            result = await Agent(GoldProvider(task), tools, cfg).run(task.prompt)
        return score_task(task, result, db, before)


def replay_all(tasks: list[Task], harness_dir: Path, work: Path) -> dict[str, list[Score]]:
    templates = {s: create_seeded_db(work / f"{s}.db", store=s) for s in sorted({t.store for t in tasks})}
    return {t.id: asyncio.run(_replay(t, templates[t.store], harness_dir)) for t in tasks}
