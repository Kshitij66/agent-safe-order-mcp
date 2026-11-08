"""Business logic. Pure functions over a DB connection; no MCP or HTTP here.

Each write has two parts:
  validate_*  - cheap checks run when the agent *requests* the action (fast feedback)
  execute_*   - the real write, run only after human approval. It re-checks everything
                inside a single conditional UPDATE, because the order may have changed
                between request and approval (time-of-check vs time-of-use).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import and_, case, select, update
from sqlalchemy.engine import Connection

from .db import orders, products

ORDER_STATUSES = frozenset({"placed", "shipped", "delivered", "cancelled", "refunded"})
CANCELLABLE = ("placed",)
REFUNDABLE = ("shipped", "delivered")


class DomainError(Exception):
    """A request that is well-formed but not allowed by business rules."""


class NotFound(DomainError):
    pass


class InvalidState(DomainError):
    pass


# ---------------------------------------------------------------- reads

def _order_query():
    return select(
        orders.c.id,
        orders.c.customer_email,
        orders.c.quantity,
        orders.c.total_cents,
        orders.c.refunded_cents,
        orders.c.status,
        orders.c.created_at,
        products.c.sku,
        products.c.name.label("product_name"),
    ).join(products, orders.c.product_id == products.c.id)


def _order_dict(row) -> dict[str, Any]:
    d = dict(row._mapping)
    d["created_at"] = d["created_at"].isoformat()
    return d


def get_order(conn: Connection, order_id: int) -> dict[str, Any]:
    row = conn.execute(_order_query().where(orders.c.id == order_id)).first()
    if row is None:
        raise NotFound(f"order {order_id} not found")
    return _order_dict(row)


def search_orders(
    conn: Connection,
    customer_email: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    q = _order_query().order_by(orders.c.id.desc()).limit(limit)
    if customer_email:
        q = q.where(orders.c.customer_email == customer_email.strip().lower())
    if status:
        if status not in ORDER_STATUSES:
            raise DomainError(f"unknown status {status!r}; expected one of {sorted(ORDER_STATUSES)}")
        q = q.where(orders.c.status == status)
    return [_order_dict(r) for r in conn.execute(q)]


def get_product(conn: Connection, sku: str) -> dict[str, Any]:
    row = conn.execute(select(products).where(products.c.sku == sku)).first()
    if row is None:
        raise NotFound(f"product {sku!r} not found")
    return dict(row._mapping)


def list_products(conn: Connection, low_stock_threshold: int | None = None) -> list[dict[str, Any]]:
    q = select(products).order_by(products.c.sku)
    if low_stock_threshold is not None:
        q = q.where(products.c.stock <= low_stock_threshold)
    return [dict(r._mapping) for r in conn.execute(q)]


# ---------------------------------------------------------------- cancel order

def validate_cancel_order(conn: Connection, order_id: int) -> None:
    order = get_order(conn, order_id)
    if order["status"] not in CANCELLABLE:
        raise InvalidState(f"order {order_id} is {order['status']}; only {CANCELLABLE} orders can be cancelled")


def execute_cancel_order(conn: Connection, order_id: int) -> dict[str, Any]:
    row = conn.execute(
        select(orders.c.product_id, orders.c.quantity).where(orders.c.id == order_id)
    ).first()
    if row is None:
        raise NotFound(f"order {order_id} not found")
    # Compare-and-set: only flips the status if it is *still* cancellable.
    res = conn.execute(
        update(orders)
        .where(and_(orders.c.id == order_id, orders.c.status.in_(CANCELLABLE)))
        .values(status="cancelled")
    )
    if res.rowcount != 1:
        raise InvalidState(f"order {order_id} is no longer cancellable")
    # Put the units back on the shelf, in the same transaction.
    conn.execute(
        update(products)
        .where(products.c.id == row.product_id)
        .values(stock=products.c.stock + row.quantity)
    )
    return {"order_id": order_id, "status": "cancelled", "restocked_units": row.quantity}


# ---------------------------------------------------------------- refund

def validate_refund(conn: Connection, order_id: int, amount_cents: int) -> None:
    if amount_cents <= 0:
        raise DomainError("amount_cents must be positive")
    order = get_order(conn, order_id)
    if order["status"] not in REFUNDABLE:
        raise InvalidState(f"order {order_id} is {order['status']}; refunds need {REFUNDABLE}")
    remaining = order["total_cents"] - order["refunded_cents"]
    if amount_cents > remaining:
        raise DomainError(f"refund {amount_cents} exceeds refundable amount {remaining}")


def execute_refund(conn: Connection, order_id: int, amount_cents: int) -> dict[str, Any]:
    new_refunded = orders.c.refunded_cents + amount_cents
    res = conn.execute(
        update(orders)
        .where(
            and_(
                orders.c.id == order_id,
                orders.c.status.in_(REFUNDABLE),
                new_refunded <= orders.c.total_cents,
            )
        )
        .values(
            refunded_cents=new_refunded,
            status=case((new_refunded == orders.c.total_cents, "refunded"), else_=orders.c.status),
        )
    )
    if res.rowcount != 1:
        raise InvalidState(f"refund of {amount_cents} on order {order_id} is no longer valid")
    order = get_order(conn, order_id)
    return {
        "order_id": order_id,
        "refunded_now_cents": amount_cents,
        "refunded_total_cents": order["refunded_cents"],
        "status": order["status"],
    }


# ---------------------------------------------------------------- stock adjustment

def validate_stock_adjustment(conn: Connection, sku: str, delta: int, max_delta: int) -> None:
    if delta == 0:
        raise DomainError("delta must be non-zero")
    if abs(delta) > max_delta:
        raise DomainError(f"|delta| must be <= {max_delta}")
    product = get_product(conn, sku)
    if product["stock"] + delta < 0:
        raise DomainError(f"stock would go negative ({product['stock']} + {delta})")


def execute_stock_adjustment(conn: Connection, sku: str, delta: int) -> dict[str, Any]:
    res = conn.execute(
        update(products)
        .where(and_(products.c.sku == sku, products.c.stock + delta >= 0))
        .values(stock=products.c.stock + delta)
    )
    if res.rowcount != 1:
        get_product(conn, sku)  # raises NotFound if missing
        raise InvalidState(f"adjusting {sku} by {delta} would make stock negative")
    return {"sku": sku, "delta": delta, "stock": get_product(conn, sku)["stock"]}
