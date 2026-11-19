"""Business logic for the support-ops tools.

Plain synchronous Python over SQLite, with no MCP imports, so it can be unit
tested directly and reused by any transport. The MCP layer (app.py) only
translates arguments in and errors out.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Literal

from . import db
from .bm25 import BM25Index
from .models import (
    Customer,
    CustomerMatches,
    CustomerOrders,
    KBHit,
    KBSearchResult,
    OrderDetail,
    OrderItem,
    OrderSummary,
    Priority,
    Refund,
    RefundReason,
    RefundResult,
    Ticket,
    TicketPage,
    TicketStatus,
)

REFUND_WINDOW_DAYS = 30
MANAGER_APPROVAL_CENTS = 50_000  # single refunds above $500 need a human manager

ErrorCode = Literal["not_found", "invalid_argument", "rule_violation", "conflict", "permission_denied"]


class DomainError(Exception):
    """An expected failure that should be shown to the caller (and the LLM)."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _read_policy(conn: sqlite3.Connection) -> tuple[int, int]:
    """(refund window days, manager approval cents). The core store has no `store_policy` table and
    uses the module constants; the RRSI stores (stores.py) carry their own policy."""
    has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_policy'").fetchone()
    if has is None:
        return REFUND_WINDOW_DAYS, MANAGER_APPROVAL_CENTS
    values = {r["key"]: int(r["value"]) for r in conn.execute("SELECT key, value FROM store_policy")}
    return (
        values.get("refund_window_days", REFUND_WINDOW_DAYS),
        values.get("manager_approval_cents", MANAGER_APPROVAL_CENTS),
    )


def _encode_cursor(created_at: str, ticket_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([created_at, ticket_id]).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        created_at, ticket_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return str(created_at), str(ticket_id)
    except (ValueError, TypeError) as exc:
        raise DomainError("invalid_argument", "cursor is malformed; pass next_cursor unchanged") from exc


class SupportService:
    def __init__(self, db_path: str | Path, *, allow_writes: bool, today: date = db.AS_OF) -> None:
        self.db_path = Path(db_path)
        self.allow_writes = allow_writes
        self.today = today
        with db.open_db(self.db_path) as conn:
            rows = conn.execute("SELECT id, title, body, tags FROM kb_articles").fetchall()
            self.refund_window_days, self.manager_approval_cents = _read_policy(conn)
        self._kb = {r["id"]: r for r in rows}
        # Title and tags are repeated so that matches there weigh more than body matches.
        self._index = BM25Index({r["id"]: f"{r['title']} {r['title']} {r['tags']} {r['body']}" for r in rows})

    # ---------------------------------------------------------------- reads

    def search_kb(self, query: str, limit: int = 3) -> KBSearchResult:
        hits = [
            KBHit(
                id=h.doc_id, title=self._kb[h.doc_id]["title"], body=self._kb[h.doc_id]["body"], score=round(h.score, 3)
            )
            for h in self._index.search(query, limit)
        ]
        return KBSearchResult(query=query, hits=hits)

    def get_customer(self, customer_id: str | None, email: str | None, name: str | None) -> CustomerMatches:
        given = [v for v in (customer_id, email, name) if v]
        if len(given) != 1:
            raise DomainError("invalid_argument", "pass exactly one of customer_id, email or name")
        with db.open_db(self.db_path) as conn:
            if customer_id:
                rows = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchall()
            elif email:
                rows = conn.execute("SELECT * FROM customers WHERE lower(email) = lower(?)", (email,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM customers WHERE name LIKE ? ORDER BY id LIMIT 10", (f"%{name}%",)
                ).fetchall()
        if not rows:
            raise DomainError("not_found", f"no customer matches {given[0]!r}")
        return CustomerMatches(customers=[Customer(**dict(r)) for r in rows])

    def list_orders(self, customer_id: str, status: str | None = None) -> CustomerOrders:
        with db.open_db(self.db_path) as conn:
            self._require_customer(conn, customer_id)
            sql, params = "SELECT * FROM orders WHERE customer_id = ?", [customer_id]
            if status:
                sql += " AND status = ?"
                params.append(status)
            rows = conn.execute(sql + " ORDER BY created_at DESC, id DESC", params).fetchall()
        return CustomerOrders(
            customer_id=customer_id,
            orders=[OrderSummary(**{k: r[k] for k in OrderSummary.model_fields}) for r in rows],
        )

    def get_order(self, order_id: str) -> OrderDetail:
        with db.open_db(self.db_path) as conn:
            return self._order_detail(conn, order_id)

    def list_tickets(
        self,
        customer_id: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        order_id: str | None = None,
        limit: int = 10,
        cursor: str | None = None,
    ) -> TicketPage:
        """Keyset pagination ordered by (created_at DESC, id DESC): stable even if rows are inserted."""
        where: list[str] = []
        params: list[str | int] = []
        for col, val in (
            ("customer_id", customer_id),
            ("status", status),
            ("priority", priority),
            ("order_id", order_id),
        ):
            if val:
                where.append(f"{col} = ?")
                params.append(val)
        if cursor:
            created_at, ticket_id = _decode_cursor(cursor)
            where.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params += [created_at, created_at, ticket_id]
        sql = "SELECT * FROM tickets"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit + 1)  # fetch one extra row to know whether another page exists
        with db.open_db(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        page = [Ticket(**dict(r)) for r in rows[:limit]]
        next_cursor = _encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
        return TicketPage(tickets=page, next_cursor=next_cursor)

    # --------------------------------------------------------------- writes

    def create_ticket(
        self, customer_id: str, subject: str, body: str, priority: Priority = "normal", order_id: str | None = None
    ) -> Ticket:
        self._require_writes()
        with db.open_db(self.db_path) as conn, db.write_transaction(conn):
            self._require_customer(conn, customer_id)
            if order_id:
                row = conn.execute("SELECT customer_id FROM orders WHERE id = ?", (order_id,)).fetchone()
                if row is None:
                    raise DomainError("not_found", f"order {order_id} does not exist")
                if row["customer_id"] != customer_id:
                    raise DomainError("invalid_argument", f"order {order_id} does not belong to {customer_id}")
            ticket = Ticket(
                id=self._next_id(conn, "tickets", "TCK-", 5),
                customer_id=customer_id,
                order_id=order_id,
                subject=subject,
                body=body,
                status="open",
                priority=priority,
                created_at=self.today.isoformat(),
            )
            conn.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?)", tuple(ticket.model_dump().values()))
        return ticket

    def update_ticket_status(self, ticket_id: str, status: TicketStatus) -> Ticket:
        self._require_writes()
        with db.open_db(self.db_path) as conn, db.write_transaction(conn):
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if row is None:
                raise DomainError("not_found", f"ticket {ticket_id} does not exist")
            conn.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))
        return Ticket(**{**dict(row), "status": status})

    def issue_refund(
        self, order_id: str, amount_cents: int, reason: RefundReason, idempotency_key: str
    ) -> RefundResult:
        self._require_writes()
        with db.open_db(self.db_path) as conn, db.write_transaction(conn):
            # 1. Idempotency: the same key + same request returns the original refund.
            prior = conn.execute("SELECT * FROM refunds WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if prior is not None:
                same = (prior["order_id"], prior["amount_cents"], prior["reason"]) == (order_id, amount_cents, reason)
                if not same:
                    raise DomainError("conflict", "idempotency_key was already used for a different refund")
                detail = self._order_detail(conn, order_id)
                return RefundResult(
                    refund=Refund(**{k: prior[k] for k in Refund.model_fields}),
                    replayed=True,
                    remaining_refundable_cents=detail.remaining_refundable_cents,
                )

            # 2. Business rules.
            order = self._order_detail(conn, order_id)
            if order.status == "cancelled":
                raise DomainError("rule_violation", "cancelled orders are refunded automatically; no refund needed")
            if order.status != "delivered":
                raise DomainError("rule_violation", f"order is {order.status}; only delivered orders can be refunded")
            assert order.days_since_delivery is not None
            if order.days_since_delivery > self.refund_window_days:
                raise DomainError(
                    "rule_violation",
                    f"delivered {order.days_since_delivery} days ago; refund window is {self.refund_window_days} days",
                )
            if amount_cents > self.manager_approval_cents:
                raise DomainError(
                    "rule_violation", f"refunds above ${self.manager_approval_cents / 100:.2f} require manager approval"
                )
            if amount_cents > order.remaining_refundable_cents:
                raise DomainError(
                    "rule_violation",
                    f"amount exceeds remaining refundable balance of {order.remaining_refundable_cents} cents",
                )

            # 3. Write. UNIQUE(idempotency_key) is a second line of defence.
            refund = Refund(
                id=self._next_id(conn, "refunds", "RFD-", 5),
                order_id=order_id,
                amount_cents=amount_cents,
                reason=reason,
                created_at=self.today.isoformat(),
            )
            conn.execute(
                "INSERT INTO refunds VALUES (?,?,?,?,?,?)",
                (refund.id, order_id, amount_cents, reason, idempotency_key, refund.created_at),
            )
        return RefundResult(
            refund=refund, replayed=False, remaining_refundable_cents=order.remaining_refundable_cents - amount_cents
        )

    # -------------------------------------------------------------- helpers

    def _require_writes(self) -> None:
        if not self.allow_writes:
            raise DomainError("permission_denied", "this server is running in read-only mode")

    @staticmethod
    def _require_customer(conn: sqlite3.Connection, customer_id: str) -> None:
        if conn.execute("SELECT 1 FROM customers WHERE id = ?", (customer_id,)).fetchone() is None:
            raise DomainError("not_found", f"customer {customer_id} does not exist")

    @staticmethod
    def _next_id(conn: sqlite3.Connection, table: str, prefix: str, width: int) -> str:
        (max_id,) = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()  # table is a constant, not user input
        n = int(max_id.removeprefix(prefix)) + 1 if max_id else 1
        return f"{prefix}{n:0{width}d}"

    def _order_detail(self, conn: sqlite3.Connection, order_id: str) -> OrderDetail:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise DomainError("not_found", f"order {order_id} does not exist")
        items = conn.execute(
            "SELECT sku, name, qty, unit_price_cents FROM order_items WHERE order_id = ?", (order_id,)
        ).fetchall()
        refunds = conn.execute(
            "SELECT id, order_id, amount_cents, reason, created_at FROM refunds WHERE order_id = ? ORDER BY id",
            (order_id,),
        ).fetchall()
        refunded = sum(r["amount_cents"] for r in refunds)
        delivered_at = row["delivered_at"]
        days = (self.today - date.fromisoformat(delivered_at)).days if delivered_at else None
        return OrderDetail(
            **dict(row),
            items=[OrderItem(**dict(i)) for i in items],
            refunds=[Refund(**dict(r)) for r in refunds],
            refunded_cents=refunded,
            remaining_refundable_cents=row["total_cents"] - refunded,
            days_since_delivery=days,
        )
