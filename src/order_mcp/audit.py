"""Append-only audit trail."""
from __future__ import annotations

from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.engine import Connection, Engine

from .db import audit_log, utcnow


def record(
    conn: Connection,
    *,
    actor: str,
    event: str,
    outcome: str,
    tool: str | None = None,
    params: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        insert(audit_log).values(
            ts=utcnow(),
            actor=actor,
            event=event,
            tool=tool,
            params=params,
            outcome=outcome,
            details=details,
        )
    )


def recent(engine: Engine, limit: int = 100) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(select(audit_log).order_by(audit_log.c.id.desc()).limit(limit))
        out = []
        for r in rows:
            d = dict(r._mapping)
            d["ts"] = d["ts"].isoformat()
            out.append(d)
        return out
