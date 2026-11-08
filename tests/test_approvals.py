from datetime import timedelta

import pytest
from sqlalchemy import update

from order_mcp import approvals, services
from order_mcp.db import orders, pending_actions, utcnow
from order_mcp.services import InvalidState


def req(engine, tool="cancel_order", params=None, key=None):
    return approvals.request_action(engine, tool=tool, params=params or {"order_id": 1},
                                    reason="customer asked to cancel", requested_by="agent:test",
                                    ttl_minutes=30, idempotency_key=key)


def order_status(engine, oid):
    with engine.connect() as c:
        return services.get_order(c, oid)["status"]


def test_request_does_not_change_anything(engine):
    action, created = req(engine)
    assert created and action["status"] == "pending"
    assert order_status(engine, 1) == "placed"


def test_duplicate_request_returns_same_action(engine):
    a, c1 = req(engine)
    b, c2 = req(engine)
    assert c1 and not c2 and a["id"] == b["id"]


def test_explicit_idempotency_key(engine):
    a, _ = req(engine, key="retry-key-123")
    b, created = req(engine, params={"order_id": 5}, key="retry-key-123")
    assert not created and b["id"] == a["id"]


def test_approve_executes(engine):
    a, _ = req(engine)
    done = approvals.approve(engine, a["id"], "human:admin")
    assert done["status"] == "executed" and done["decided_by"] == "human:admin"
    assert order_status(engine, 1) == "cancelled"


def test_cannot_approve_twice(engine):
    a, _ = req(engine)
    approvals.approve(engine, a["id"], "human:admin")
    with pytest.raises(InvalidState):
        approvals.approve(engine, a["id"], "human:admin")


def test_reject_then_approve_is_blocked(engine):
    a, _ = req(engine)
    assert approvals.reject(engine, a["id"], "human:admin", "not verified")["status"] == "rejected"
    with pytest.raises(InvalidState):
        approvals.approve(engine, a["id"], "human:admin")
    assert order_status(engine, 1) == "placed"


def test_expired_action_is_not_executed(engine):
    a, _ = req(engine)
    with engine.begin() as c:
        c.execute(update(pending_actions).where(pending_actions.c.id == a["id"])
                  .values(expires_at=utcnow() - timedelta(minutes=1)))
    assert approvals.approve(engine, a["id"], "human:admin")["status"] == "expired"
    assert order_status(engine, 1) == "placed"


def test_state_change_between_request_and_approval_fails_safely(engine):
    a, _ = req(engine)
    with engine.begin() as c:  # order ships before a human gets to it
        c.execute(update(orders).where(orders.c.id == 1).values(status="shipped"))
    done = approvals.approve(engine, a["id"], "human:admin")
    assert done["status"] == "failed" and "no longer cancellable" in done["result"]["error"]
    assert order_status(engine, 1) == "shipped"


def test_expire_stale(engine):
    a, _ = req(engine)
    with engine.begin() as c:
        c.execute(update(pending_actions).values(expires_at=utcnow() - timedelta(seconds=1)))
    assert approvals.expire_stale(engine) == 1
    assert approvals.get_action(engine, a["id"])["status"] == "expired"
