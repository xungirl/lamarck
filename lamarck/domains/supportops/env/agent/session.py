"""Open an MCP connection to the support-ops server over the chosen transport."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver import MCPServer

from ..server.app import build_server
from .tools import McpToolExecutor

Transport = Literal["inprocess", "stdio", "http"]


@asynccontextmanager
async def open_support_tools(
    db_path: str | Path,
    *,
    allow_writes: bool,
    transport: Transport = "inprocess",
    url: str = "http://127.0.0.1:8000/mcp",
) -> AsyncIterator[McpToolExecutor]:
    """`inprocess` runs the server in this process (fast; used by tests and evals),
    `stdio` spawns it as a subprocess, `http` connects to an already-running server."""
    target: MCPServer | StdioServerParameters | str
    if transport == "inprocess":
        target = build_server(db_path, allow_writes=allow_writes)
    elif transport == "stdio":
        args = ["-m", "agent_eval_mcp.server.app", "--db", str(db_path)]
        if allow_writes:
            args.append("--allow-writes")
        target = StdioServerParameters(command=sys.executable, args=args)
    else:
        target = url
    async with Client(target) as client:
        yield McpToolExecutor(client)
