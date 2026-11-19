"""Harness hooks for the support-ops agent. H0: every hook is a pass-through.

The agent loop calls these at fixed points (see the constitution for the contract):

    user_message(prompt) -> str
    tool_description(name, description) -> str
    before_tool_call(name, args, ctx) -> str | None     # a string blocks the call and is shown to the model
    format_tool_result(name, args, text, is_error, ctx) -> str
    after_final(answer, ctx) -> str | None              # a string sends the model back once more
    CLIENT_TOOLS: list of {"name", "description", "parameters", "fn"}  # local, pure-Python tools

ctx = {"user_prompt": str, "step": int, "max_steps": int,
       "calls": [{"name": str, "args": dict, "is_error": bool, "result": str}, ...]}
"""

from __future__ import annotations

from typing import Any

CLIENT_TOOLS: list[dict[str, Any]] = []


def user_message(prompt: str) -> str:
    return prompt


def tool_description(name: str, description: str) -> str:
    return description


def before_tool_call(name: str, args: dict[str, Any], ctx: dict[str, Any]) -> str | None:
    return None


def format_tool_result(
    name: str, args: dict[str, Any], text: str, is_error: bool, ctx: dict[str, Any]
) -> str:
    return text


def after_final(answer: str, ctx: dict[str, Any]) -> str | None:
    return None
