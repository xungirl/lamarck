"""Harness hooks: the narrow, bounded interface through which an evolvable harness can shape the loop.

The agent loop (loop.py) is part of the fixed environment. A harness directory (see harness_dir.py)
may supply plain functions that the loop calls at fixed points:

    user_message(prompt) -> str                         wrap the human's request
    tool_description(name, description) -> str          rewrite a tool's description (schema is fixed)
    before_tool_call(name, args, ctx) -> str | None     a non-None string blocks the call; the text is
                                                        returned to the model as the tool result
    format_tool_result(name, args, text, is_error, ctx) -> str
    after_final(answer, ctx) -> str | None              a non-None string is sent back as a user
                                                        message once more (at most max_final_checks)
    CLIENT_TOOLS                                        pure-Python tools run locally, never on the server

The loop owns every bound: it calls hooks synchronously, catches every exception (falls back to the
default behaviour and records a `hook_error` trace event), never lets a hook rename or add server tools,
and keeps max_steps and timeouts. `ctx` is a fresh dict per call, so hooks cannot mutate loop state.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ClientTool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[[dict[str, Any]], str]


class Hooks(Protocol):
    client_tools: Sequence[ClientTool]

    def user_message(self, prompt: str) -> str: ...

    def tool_description(self, name: str, description: str) -> str: ...

    def before_tool_call(self, name: str, args: dict[str, Any], ctx: dict[str, Any]) -> str | None: ...

    def format_tool_result(
        self, name: str, args: dict[str, Any], text: str, is_error: bool, ctx: dict[str, Any]
    ) -> str: ...

    def after_final(self, answer: str, ctx: dict[str, Any]) -> str | None: ...


class DefaultHooks:
    """Pass-through hooks: exactly the behaviour of the loop without a harness."""

    client_tools: Sequence[ClientTool] = ()

    def user_message(self, prompt: str) -> str:
        return prompt

    def tool_description(self, name: str, description: str) -> str:
        return description

    def before_tool_call(self, name: str, args: dict[str, Any], ctx: dict[str, Any]) -> str | None:
        return None

    def format_tool_result(
        self, name: str, args: dict[str, Any], text: str, is_error: bool, ctx: dict[str, Any]
    ) -> str:
        return text

    def after_final(self, answer: str, ctx: dict[str, Any]) -> str | None:
        return None
