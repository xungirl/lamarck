"""Pydantic models returned by the tools (they become the MCP output schemas)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Tier = Literal["standard", "gold"]
OrderStatus = Literal["pending", "shipped", "delivered", "cancelled"]
TicketStatus = Literal["open", "pending", "closed"]
Priority = Literal["low", "normal", "high", "urgent"]
RefundReason = Literal["damaged", "not_as_described", "late_delivery", "changed_mind", "other"]


class KBHit(BaseModel):
    id: str
    title: str
    body: str
    score: float


class KBSearchResult(BaseModel):
    query: str
    hits: list[KBHit]


class Customer(BaseModel):
    id: str
    name: str
    email: str
    tier: Tier
    created_at: str


class CustomerMatches(BaseModel):
    """get_customer can match several people when searching by name."""

    customers: list[Customer]


class OrderSummary(BaseModel):
    id: str
    status: OrderStatus
    total_cents: int
    created_at: str
    delivered_at: str | None


class CustomerOrders(BaseModel):
    customer_id: str
    orders: list[OrderSummary]


class OrderItem(BaseModel):
    sku: str
    name: str
    qty: int
    unit_price_cents: int


class Refund(BaseModel):
    id: str
    order_id: str
    amount_cents: int
    reason: str
    created_at: str


class OrderDetail(OrderSummary):
    customer_id: str
    items: list[OrderItem]
    refunds: list[Refund]
    refunded_cents: int
    remaining_refundable_cents: int
    days_since_delivery: int | None


class Ticket(BaseModel):
    id: str
    customer_id: str
    order_id: str | None
    subject: str
    body: str
    status: TicketStatus
    priority: Priority
    created_at: str


class TicketPage(BaseModel):
    tickets: list[Ticket]
    next_cursor: str | None


class RefundResult(BaseModel):
    refund: Refund
    replayed: bool  # True when the idempotency key matched an earlier identical request
    remaining_refundable_cents: int
