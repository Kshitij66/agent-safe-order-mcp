"""Database schema (SQLAlchemy Core). Works on SQLite (dev/tests) and PostgreSQL (docker)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

# Money is stored as integer cents/paise: floats can't represent 0.1 exactly.
products = Table(
    "products",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sku", String(32), unique=True, nullable=False),
    Column("name", String(200), nullable=False),
    Column("price_cents", Integer, nullable=False),
    Column("stock", Integer, nullable=False),
    # Last line of defence: the database itself refuses negative stock.
    CheckConstraint("stock >= 0", name="ck_products_stock_non_negative"),
    CheckConstraint("price_cents >= 0", name="ck_products_price_non_negative"),
)

orders = Table(
    "orders",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("customer_email", String(254), nullable=False, index=True),
    Column("product_id", Integer, ForeignKey("products.id"), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("total_cents", Integer, nullable=False),
    Column("refunded_cents", Integer, nullable=False, default=0),
    Column("status", String(20), nullable=False, index=True),
    Column("created_at", DateTime, nullable=False),
    CheckConstraint("quantity > 0", name="ck_orders_quantity_positive"),
    CheckConstraint(
        "refunded_cents >= 0 AND refunded_cents <= total_cents",
        name="ck_orders_refund_within_total",
    ),
)

# Every write an agent asks for becomes a row here. Nothing executes until a human approves it.
pending_actions = Table(
    "pending_actions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tool", String(50), nullable=False),
    Column("params", JSON, nullable=False),
    Column("reason", Text, nullable=False),
    # pending -> executed | failed | rejected | expired
    Column("status", String(20), nullable=False, index=True),
    Column("idempotency_key", String(64), unique=True, nullable=False),
    Column("requested_by", String(100), nullable=False),
    Column("requested_at", DateTime, nullable=False),
    Column("expires_at", DateTime, nullable=False),
    Column("decided_by", String(100)),
    Column("decided_at", DateTime),
    Column("decision_note", Text),
    Column("result", JSON),
)

# Append-only record of every tool call and every human decision.
audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("actor", String(100), nullable=False),
    Column("event", String(50), nullable=False),
    Column("tool", String(50)),
    Column("params", JSON),
    Column("outcome", String(30), nullable=False),
    Column("details", JSON),
)


def utcnow() -> datetime:
    """Naive UTC timestamp (all stored times are UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


def init_db(engine: Engine) -> None:
    metadata.create_all(engine)
