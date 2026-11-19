"""MCP server exposing the support-ops tools.

`build_server()` returns an `MCPServer` bound to one SQLite file. With
`allow_writes=False` the write tools are simply not registered, so a
read-only client cannot even see them (and the service layer refuses writes
as a second check).

(lamarck: the standalone `main()` entry point of the original is omitted; the evaluation runtime
builds the server in-process against a per-task copy of the store database.)
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Annotated, Literal, TypeVar

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .models import (
    CustomerMatches,
    CustomerOrders,
    KBSearchResult,
    OrderDetail,
    OrderStatus,
    Priority,
    RefundReason,
    RefundResult,
    Ticket,
    TicketPage,
    TicketStatus,
)
from .service import MANAGER_APPROVAL_CENTS, REFUND_WINDOW_DAYS, DomainError, SupportService

T = TypeVar("T")
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Shared argument types: pydantic validates these before our code runs and
# they are published to the client as the tool's JSON Schema.
CustomerId = Annotated[str, Field(pattern=r"^CUS-\d{4}$", description="Customer id, e.g. CUS-0001")]
OrderId = Annotated[str, Field(pattern=r"^ORD-\d{5}$", description="Order id, e.g. ORD-10001")]
TicketId = Annotated[str, Field(pattern=r"^TCK-\d{5}$", description="Ticket id, e.g. TCK-00001")]

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)

WRITE_TOOLS = frozenset({"create_ticket", "update_ticket_status", "issue_refund"})


async def _run[T](fn: Callable[[], T]) -> T:
    """Run blocking SQLite work in a worker thread and map domain errors to MCP tool errors."""
    try:
        return await anyio.to_thread.run_sync(fn)
    except DomainError as exc:
        # ToolError -> CallToolResult(is_error=True) whose text the model can read and react to.
        raise ToolError(str(exc)) from exc


def build_server(db_path: str | Path, *, allow_writes: bool = False, log_level: LogLevel = "WARNING") -> MCPServer:
    svc = SupportService(db_path, allow_writes=allow_writes)
    mode = "read/write" if allow_writes else "read-only"
    server = MCPServer(
        "supportops",
        instructions=f"Internal support-operations tools ({mode}). Amounts are integer cents.",
        log_level=log_level,
    )

    @server.tool(annotations=READ_ONLY)
    async def search_kb(
        query: Annotated[str, Field(min_length=2, max_length=200, description="Keywords to search for")],
        limit: Annotated[int, Field(ge=1, le=10)] = 3,
    ) -> KBSearchResult:
        """Search internal knowledge-base articles (policies, shipping, warranty, ...) with BM25 ranking."""
        return await _run(partial(svc.search_kb, query, limit))

    @server.tool(annotations=READ_ONLY)
    async def get_customer(
        customer_id: CustomerId | None = None,
        email: Annotated[str | None, Field(description="Exact email address")] = None,
        name: Annotated[str | None, Field(min_length=2, description="Full or partial name")] = None,
    ) -> CustomerMatches:
        """Look up customers by exactly one of customer_id, email or (partial) name. Name may match several."""
        return await _run(partial(svc.get_customer, customer_id, email, name))

    @server.tool(annotations=READ_ONLY)
    async def list_orders(customer_id: CustomerId, status: OrderStatus | None = None) -> CustomerOrders:
        """List a customer's orders, newest first, optionally filtered by status."""
        return await _run(partial(svc.list_orders, customer_id, status))

    @server.tool(annotations=READ_ONLY)
    async def get_order(order_id: OrderId) -> OrderDetail:
        """Get one order with items, past refunds, remaining refundable amount and days since delivery."""
        return await _run(partial(svc.get_order, order_id))

    @server.tool(annotations=READ_ONLY)
    async def list_tickets(
        customer_id: CustomerId | None = None,
        status: TicketStatus | None = None,
        priority: Priority | None = None,
        order_id: OrderId | None = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        cursor: Annotated[str | None, Field(description="next_cursor from a previous page")] = None,
    ) -> TicketPage:
        """List support tickets, newest first, with optional filters and cursor pagination."""
        return await _run(partial(svc.list_tickets, customer_id, status, priority, order_id, limit, cursor))

    if not allow_writes:
        return server

    @server.tool(annotations=WRITE)
    async def create_ticket(
        customer_id: CustomerId,
        subject: Annotated[str, Field(min_length=3, max_length=120)],
        body: Annotated[str, Field(min_length=3, max_length=2000)],
        priority: Priority = "normal",
        order_id: OrderId | None = None,
    ) -> Ticket:
        """Open a new support ticket for a customer (optionally linked to one of their orders)."""
        return await _run(partial(svc.create_ticket, customer_id, subject, body, priority, order_id))

    @server.tool(annotations=WRITE)
    async def update_ticket_status(ticket_id: TicketId, status: TicketStatus) -> Ticket:
        """Change a ticket's status to open, pending or closed."""
        return await _run(partial(svc.update_ticket_status, ticket_id, status))

    refund_description = None  # None = the docstring below (the core store's policy)
    if (svc.refund_window_days, svc.manager_approval_cents) != (REFUND_WINDOW_DAYS, MANAGER_APPROVAL_CENTS):
        refund_description = (
            f"Issue a refund. Enforces policy (delivered, {svc.refund_window_days}-day window, "
            f"<= ${svc.manager_approval_cents / 100:.0f}, <= remaining balance).\n"
            "Repeating a call with the same idempotency_key returns the original refund instead of paying twice."
        )

    @server.tool(
        description=refund_description,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True),
    )
    async def issue_refund(
        order_id: OrderId,
        amount_cents: Annotated[int, Field(gt=0, description="Refund amount in integer cents")],
        reason: RefundReason,
        idempotency_key: Annotated[
            str, Field(min_length=8, max_length=64, description="Unique key per refund; retries reuse the same key")
        ],
    ) -> RefundResult:
        """Issue a refund. Enforces policy (delivered, 30-day window, <= $500, <= remaining balance).
        Repeating a call with the same idempotency_key returns the original refund instead of paying twice."""
        return await _run(partial(svc.issue_refund, order_id, amount_cents, reason, idempotency_key))

    return server
