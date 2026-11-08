"""Agent-safety suite: what happens when an agent misbehaves, by accident or because a
prompt told it to. Each case drives the ToolGateway exactly as the MCP server does."""
import pytest
from sqlalchemy import func, select

from order_mcp import approvals, services
from order_mcp.db import audit_log, orders, pending_actions

READ_ONLY = {"orders:read", "inventory:read"}


def count(engine, table, *where):
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(table).where(*where)).scalar()


def snapshot(engine):
    with engine.connect() as c:
        return (c.execute(select(orders.c.id, orders.c.status, orders.c.refunded_cents).order_by(orders.c.id)).all(),
                [p["stock"] for p in services.list_products(c)])


# --- 1. Writes never apply without a human -------------------------------------------

@pytest.mark.parametrize("call", [
    lambda g: g.request_order_cancellation(1, "customer changed their mind"),
    lambda g: g.request_refund(2, 49900, "one cable arrived damaged"),
    lambda g: g.request_stock_adjustment("KB-101", -5, "warehouse count found 5 fewer"),
])
def test_write_requests_change_nothing_until_approved(gateway, engine, call):
    before = snapshot(engine)
    r = call(gateway)
    assert r["ok"] and r["status"] == "pending_approval" and "NOT been applied" in r["message"]
    assert snapshot(engine) == before


def test_full_happy_path_agent_request_human_approve(gateway, engine):
    r = gateway.request_refund(3, 899900, "headphones arrived broken, customer sent photo")
    approvals.approve(engine, r["action_id"], "human:ops")
    s = gateway.get_action_status(r["action_id"])
    assert s["status"] == "executed" and s["result"]["status"] == "refunded"


# --- 2. "Clean up everything" style prompts ------------------------------------------

def test_no_bulk_or_delete_tools_exist():
    from order_mcp import mcp_server
    import asyncio
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert not any(w in n for n in names for w in ("delete", "drop", "bulk", "truncate", "sql"))
    assert {n for n in names if n.startswith("request_")} == {
        "request_order_cancellation", "request_refund", "request_stock_adjustment"}


def test_mass_cancellation_attempt_only_queues_and_is_rate_limited(make_gateway, engine):
    g = make_gateway(write=3)
    results = [g.request_order_cancellation(oid, "agent decided to clean up old orders") for oid in range(1, 9)]
    queued = [r for r in results if r["ok"]]
    limited = [r for r in results if r.get("error_type") == "rate_limited"]
    assert len(queued) + len(limited) + sum(r.get("error_type") == "invalid_request" for r in results) == 8
    assert len(limited) > 0
    assert count(engine, orders, orders.c.status == "cancelled") == 1  # only the pre-existing one


# --- 3. Scope enforcement -------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda g: g.request_order_cancellation(1, "customer changed their mind"),
    lambda g: g.request_refund(2, 100, "goodwill gesture for delay"),
    lambda g: g.request_stock_adjustment("KB-101", 10, "restock shipment arrived"),
])
def test_read_only_agent_cannot_request_writes(make_gateway, engine, call):
    r = call(make_gateway(scopes=READ_ONLY))
    assert not r["ok"] and r["error_type"] == "permission_denied"
    assert count(engine, pending_actions) == 0


def test_read_only_agent_can_still_read(make_gateway):
    g = make_gateway(scopes=READ_ONLY)
    assert g.get_order(1)["ok"] and g.check_stock("KB-101")["ok"]


def test_scope_checked_before_existence_is_revealed(make_gateway):
    r = make_gateway(scopes=READ_ONLY).request_order_cancellation(99999, "probing for valid ids")
    assert r["error_type"] == "permission_denied"  # not "not found"


# --- 4. Bad or malicious arguments ------------------------------------------------------

@pytest.mark.parametrize("call,needle", [
    (lambda g: g.request_refund(4, 10_000_000, "refund way more than paid"), "exceeds"),
    (lambda g: g.request_refund(2, -500, "negative refund to charge customer"), "positive"),
    (lambda g: g.request_refund(1, 100, "refund an order that has not shipped"), "refunds need"),
    (lambda g: g.request_order_cancellation(3, "cancel an order already shipped"), "only"),
    (lambda g: g.request_order_cancellation(6, "cancel an already cancelled order"), "only"),
    (lambda g: g.request_stock_adjustment("KB-101", -10_000, "wipe out all stock"), "<="),
    (lambda g: g.request_stock_adjustment("MN-404", -9, "remove more than exists"), "negative"),
    (lambda g: g.request_stock_adjustment("KB-101", 0, "zero change request"), "non-zero"),
    (lambda g: g.request_stock_adjustment("'; DROP TABLE products;--", 1, "sql injection attempt"), "not found"),
    (lambda g: g.get_order(424242), "not found"),
    (lambda g: g.search_orders(status="deleted"), "unknown status"),
])
def test_invalid_requests_are_rejected_with_clear_errors(gateway, engine, call, needle):
    r = call(gateway)
    assert not r["ok"] and r["error_type"] == "invalid_request" and needle in r["error"]
    assert count(engine, pending_actions) == 0


@pytest.mark.parametrize("reason", ["", "   ", "because"])
def test_changes_require_a_real_reason(gateway, reason):
    r = gateway.request_order_cancellation(1, reason)
    assert not r["ok"] and "reason" in r["error"]


def test_list_size_is_capped(gateway):
    r = gateway.search_orders(limit=10_000)
    assert r["limit_applied"] == 50


def test_internal_errors_are_not_leaked(gateway, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("psycopg.OperationalError: password=hunter2")
    monkeypatch.setattr(services, "get_order", boom)
    r = gateway.get_order(1)
    assert r == {"ok": False, "error": "internal error", "error_type": "internal"}


# --- 5. Retries and duplicates ------------------------------------------------------------

def test_agent_retry_does_not_double_refund(gateway, engine):
    first = gateway.request_refund(2, 49900, "one cable arrived damaged")
    retry = gateway.request_refund(2, 49900, "retrying: one cable arrived damaged")
    assert retry["duplicate"] and retry["action_id"] == first["action_id"]
    approvals.approve(engine, first["action_id"], "human:ops")
    again = gateway.request_refund(2, 49900, "one cable arrived damaged")
    assert again["duplicate"] and again["status"] == "executed"
    with engine.connect() as c:
        assert services.get_order(c, 2)["refunded_cents"] == 49900


# --- 6. Everything is audited ---------------------------------------------------------------

def test_every_call_is_audited_including_denials(make_gateway, engine):
    g = make_gateway(scopes=READ_ONLY)
    g.get_order(1)
    g.request_refund(2, 100, "goodwill gesture for delay")
    g.get_order(999)
    with engine.connect() as c:
        rows = c.execute(select(audit_log.c.tool, audit_log.c.outcome).order_by(audit_log.c.id)).all()
    assert rows == [("get_order", "ok"), ("request_refund", "denied"), ("get_order", "rejected_input")]
