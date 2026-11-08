"""Human-in-the-loop approval workflow.

Agent asks  ->  request_action() stores a 'pending' row      (nothing changes yet)
Human says  ->  approve()  executes it in one transaction     -> 'executed' or 'failed'
                reject()   closes it                          -> 'rejected'
Nobody acts ->  it expires after ACTION_TTL_MINUTES           -> 'expired'
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import and_, insert, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from . import audit, services
from .db import pending_actions, utcnow
from .services import DomainError, InvalidState, NotFound

# tool name -> function that performs the real write
EXECUTORS: dict[str, Callable[..., dict[str, Any]]] = {
    "cancel_order": services.execute_cancel_order,
    "refund_order": services.execute_refund,
    "adjust_stock": services.execute_stock_adjustment,
}

FINAL_STATUSES = frozenset({"executed", "failed", "rejected", "expired"})


def derive_idempotency_key(tool: str, params: dict[str, Any]) -> str:
    """Same tool + same params -> same key, so an agent retrying a request can't queue it twice."""
    canonical = json.dumps({"tool": tool, "params": params}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _action_dict(row) -> dict[str, Any]:
    d = dict(row._mapping)
    for k in ("requested_at", "expires_at", "decided_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


def _fetch(conn: Connection, action_id: int):
    row = conn.execute(select(pending_actions).where(pending_actions.c.id == action_id)).first()
    if row is None:
        raise NotFound(f"action {action_id} not found")
    return row


def request_action(
    engine: Engine,
    *,
    tool: str,
    params: dict[str, Any],
    reason: str,
    requested_by: str,
    ttl_minutes: int,
    idempotency_key: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Queue a write for approval. Returns (action, created). created=False means a duplicate."""
    if tool not in EXECUTORS:
        raise DomainError(f"unknown action {tool!r}")
    key = idempotency_key or derive_idempotency_key(tool, params)
    now = utcnow()
    try:
        with engine.begin() as conn:
            existing = conn.execute(
                select(pending_actions).where(pending_actions.c.idempotency_key == key)
            ).first()
            if existing is not None:
                return _action_dict(existing), False
            new_id = conn.execute(
                insert(pending_actions).values(
                    tool=tool,
                    params=params,
                    reason=reason,
                    status="pending",
                    idempotency_key=key,
                    requested_by=requested_by,
                    requested_at=now,
                    expires_at=now + timedelta(minutes=ttl_minutes),
                )
            ).inserted_primary_key[0]
            return _action_dict(_fetch(conn, new_id)), True
    except IntegrityError:
        # Two identical requests raced; the UNIQUE constraint let exactly one insert win.
        with engine.connect() as conn:
            row = conn.execute(
                select(pending_actions).where(pending_actions.c.idempotency_key == key)
            ).first()
            return _action_dict(row), False


def approve(engine: Engine, action_id: int, approver: str, note: str | None = None) -> dict[str, Any]:
    """Execute an approved action. The claim + write + status update are one transaction."""
    with engine.begin() as conn:
        now = utcnow()
        # Atomic claim: only one approver can move a row out of 'pending'.
        # On Postgres the row lock makes a concurrent approve wait, then match 0 rows.
        claimed = conn.execute(
            update(pending_actions)
            .where(
                and_(
                    pending_actions.c.id == action_id,
                    pending_actions.c.status == "pending",
                    pending_actions.c.expires_at > now,
                )
            )
            .values(status="approved", decided_by=approver, decided_at=now, decision_note=note)
        )
        if claimed.rowcount != 1:
            row = _fetch(conn, action_id)
            if row.status == "pending":  # it was pending but past its expiry
                conn.execute(
                    update(pending_actions)
                    .where(pending_actions.c.id == action_id)
                    .values(status="expired")
                )
                audit.record(conn, actor=approver, event="approve", tool=row.tool,
                             params=row.params, outcome="expired", details={"action_id": action_id})
                return _action_dict(_fetch(conn, action_id))
            raise InvalidState(f"action {action_id} is already {row.status}")

        row = _fetch(conn, action_id)
        try:
            result = EXECUTORS[row.tool](conn, **row.params)
            status = "executed"
        except DomainError as e:
            # State changed since the request (e.g. order shipped). Nothing was written.
            result = {"error": str(e)}
            status = "failed"
        conn.execute(
            update(pending_actions)
            .where(pending_actions.c.id == action_id)
            .values(status=status, result=result)
        )
        audit.record(conn, actor=approver, event="approve", tool=row.tool, params=row.params,
                     outcome=status, details={"action_id": action_id, "result": result})
        return _action_dict(_fetch(conn, action_id))


def reject(engine: Engine, action_id: int, approver: str, note: str | None = None) -> dict[str, Any]:
    with engine.begin() as conn:
        res = conn.execute(
            update(pending_actions)
            .where(and_(pending_actions.c.id == action_id, pending_actions.c.status == "pending"))
            .values(status="rejected", decided_by=approver, decided_at=utcnow(), decision_note=note)
        )
        row = _fetch(conn, action_id)
        if res.rowcount != 1:
            raise InvalidState(f"action {action_id} is already {row.status}")
        audit.record(conn, actor=approver, event="reject", tool=row.tool, params=row.params,
                     outcome="rejected", details={"action_id": action_id, "note": note})
        return _action_dict(row)


def expire_stale(engine: Engine) -> int:
    with engine.begin() as conn:
        res = conn.execute(
            update(pending_actions)
            .where(and_(pending_actions.c.status == "pending", pending_actions.c.expires_at <= utcnow()))
            .values(status="expired")
        )
        return res.rowcount


def get_action(engine: Engine, action_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        return _action_dict(_fetch(conn, action_id))


def list_actions(engine: Engine, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    q = select(pending_actions).order_by(pending_actions.c.id.desc()).limit(limit)
    if status:
        q = q.where(pending_actions.c.status == status)
    with engine.connect() as conn:
        return [_action_dict(r) for r in conn.execute(q)]
