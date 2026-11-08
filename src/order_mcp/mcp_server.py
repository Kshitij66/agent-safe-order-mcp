"""MCP server: exposes the ToolGateway to LLM clients (Claude Desktop, Cursor) over stdio.

Note: with the stdio transport, stdout carries the JSON-RPC protocol. Never print() here;
logs go to stderr.
"""
from __future__ import annotations

import logging
import sys

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .config import Settings
from .db import init_db, make_engine
from .gateway import ToolGateway

logging.basicConfig(stream=sys.stderr, level=logging.INFO)

INSTRUCTIONS = """\
You are connected to a live order and inventory database.
- Read tools return data immediately.
- Any change (cancel, refund, stock adjustment) is only a REQUEST. It is queued for a human
  to approve and is not applied until they do. Always give a specific reason.
- Never retry a change request that returned an action_id; poll get_action_status instead.
- There are no bulk-delete or bulk-update tools by design. Act on one record at a time.
- Amounts are integer cents (e.g. 49900 = Rs 499.00).
"""

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

settings = Settings.from_env()
engine = make_engine(settings.database_url)
init_db(engine)
gateway = ToolGateway(engine, settings, actor="agent:mcp")

mcp = MCPServer("agent-safe-orders", instructions=INSTRUCTIONS, version="0.1.0")


@mcp.tool(annotations=READ)
def get_order(order_id: int) -> dict:
    """Get one order by id, including product, status, total and refunded amount (cents)."""
    return gateway.get_order(order_id)


@mcp.tool(annotations=READ)
def search_orders(customer_email: str | None = None, status: str | None = None, limit: int = 20) -> dict:
    """Search orders by customer email and/or status (placed, shipped, delivered, cancelled, refunded).
    Returns at most 50 rows, newest first."""
    return gateway.search_orders(customer_email, status, limit)


@mcp.tool(annotations=READ)
def list_products(low_stock_threshold: int | None = None) -> dict:
    """List products with stock levels. Pass low_stock_threshold to only see items at or below it."""
    return gateway.list_products(low_stock_threshold)


@mcp.tool(annotations=READ)
def check_stock(sku: str) -> dict:
    """Get current stock for a product SKU."""
    return gateway.check_stock(sku)


@mcp.tool(annotations=READ)
def get_action_status(action_id: int) -> dict:
    """Check whether a queued change was approved, rejected, executed, failed or expired."""
    return gateway.get_action_status(action_id)


@mcp.tool(annotations=WRITE)
def request_order_cancellation(order_id: int, reason: str, idempotency_key: str | None = None) -> dict:
    """REQUEST cancellation of a 'placed' order (restocks its units). Requires human approval."""
    return gateway.request_order_cancellation(order_id, reason, idempotency_key)


@mcp.tool(annotations=WRITE)
def request_refund(order_id: int, amount_cents: int, reason: str, idempotency_key: str | None = None) -> dict:
    """REQUEST a full or partial refund (in cents) on a shipped/delivered order. Requires human approval."""
    return gateway.request_refund(order_id, amount_cents, reason, idempotency_key)


@mcp.tool(annotations=WRITE)
def request_stock_adjustment(sku: str, delta: int, reason: str, idempotency_key: str | None = None) -> dict:
    """REQUEST a stock change for a SKU (positive adds, negative removes). Requires human approval."""
    return gateway.request_stock_adjustment(sku, delta, reason, idempotency_key)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
