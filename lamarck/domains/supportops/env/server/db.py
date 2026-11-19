"""SQLite schema and connection helpers for the support-ops database.

The database is a plain file. Every tool call opens its own short-lived
connection, which keeps the code thread-safe (tools run in a worker thread)
and lets each eval task work on its own copy of the file.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

# The "current date" of the simulated business. Fixed so that business rules
# such as "refund window is 30 days after delivery" give the same answer on
# every run, no matter when the tests or evals are executed.
AS_OF = date(2026, 11, 1)

SCHEMA = """
CREATE TABLE customers (
    id          TEXT PRIMARY KEY,           -- CUS-0001
    name        TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    tier        TEXT NOT NULL CHECK (tier IN ('standard', 'gold')),
    created_at  TEXT NOT NULL
);

CREATE TABLE orders (
    id            TEXT PRIMARY KEY,         -- ORD-10001
    customer_id   TEXT NOT NULL REFERENCES customers(id),
    status        TEXT NOT NULL CHECK (status IN ('pending', 'shipped', 'delivered', 'cancelled')),
    total_cents   INTEGER NOT NULL CHECK (total_cents >= 0),
    created_at    TEXT NOT NULL,
    delivered_at  TEXT
);

CREATE TABLE order_items (
    order_id          TEXT NOT NULL REFERENCES orders(id),
    sku               TEXT NOT NULL,
    name              TEXT NOT NULL,
    qty               INTEGER NOT NULL CHECK (qty > 0),
    unit_price_cents  INTEGER NOT NULL CHECK (unit_price_cents >= 0)
);

CREATE TABLE tickets (
    id           TEXT PRIMARY KEY,          -- TCK-00001
    customer_id  TEXT NOT NULL REFERENCES customers(id),
    order_id     TEXT REFERENCES orders(id),
    subject      TEXT NOT NULL,
    body         TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('open', 'pending', 'closed')),
    priority     TEXT NOT NULL CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    created_at   TEXT NOT NULL
);

CREATE TABLE refunds (
    id               TEXT PRIMARY KEY,      -- RFD-00001
    order_id         TEXT NOT NULL REFERENCES orders(id),
    amount_cents     INTEGER NOT NULL CHECK (amount_cents > 0),
    reason           TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    created_at       TEXT NOT NULL
);

CREATE TABLE kb_articles (
    id     TEXT PRIMARY KEY,                -- KB-001
    title  TEXT NOT NULL,
    body   TEXT NOT NULL,
    tags   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_tickets_customer ON tickets(customer_id);
CREATE INDEX idx_tickets_created ON tickets(created_at DESC, id DESC);
CREATE INDEX idx_refunds_order ON refunds(order_id);
"""

# Tables the safety scorer watches for unexpected changes.
TABLES = ("customers", "orders", "order_items", "tickets", "refunds", "kb_articles")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)  # autocommit; we BEGIN explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def open_db(path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE takes the write lock up front, so a check-then-insert
    sequence (e.g. "is there room left to refund?" then "insert refund") cannot
    interleave with another writer."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
