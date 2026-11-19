"""Tool executors: how the agent loop reaches tools.

The loop depends only on the `ToolExecutor` protocol. The production
implementation talks to an MCP server through the official client; tests can
wrap it (e.g. to inject a transient failure) without touching the loop.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import anyio
from mcp import Client
from mcp_types import CallToolResult, TextContent

from .types import ToolResult, ToolSpec


class TransientToolError(Exception):
    """Transport-level failure (connection dropped, timeout) that is safe to retry."""


class ToolExecutor(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...


def _to_result(result: CallToolResult) -> ToolResult:
    if result.structured_content is not None and not result.is_error:
        # Compact JSON: fewer tokens than the pretty-printed text block.
        text = json.dumps(result.structured_content, separators=(",", ":"), ensure_ascii=False)
    else:
        text = "\n".join(c.text for c in result.content if isinstance(c, TextContent))
    return ToolResult(is_error=bool(result.is_error), text=text)


class McpToolExecutor:
    """Adapts a connected `mcp.Client` to the `ToolExecutor` protocol."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def list_tools(self) -> list[ToolSpec]:
        specs: list[ToolSpec] = []
        cursor: str | None = None
        while True:
            page = await self._client.list_tools(cursor=cursor)
            for t in page.tools:
                read_only = bool(t.annotations and t.annotations.read_only_hint)
                specs.append(ToolSpec(t.name, t.description or "", dict(t.input_schema), read_only))
            cursor = page.next_cursor
            if cursor is None:
                return specs

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            result = await self._client.call_tool(name, arguments)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, ConnectionError) as exc:
            raise TransientToolError(f"{type(exc).__name__}: {exc}") from exc
        return _to_result(result)
