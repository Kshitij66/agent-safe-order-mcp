"""ToolGateway: the single choke point every agent tool call passes through.

    agent -> MCP tool -> ToolGateway._call()
                           1. scope check        (is the agent allowed this at all?)
                           2. rate limit         (is it calling too fast?)
                           3. input validation   (are the arguments sane?)
                           4. run                (read directly, or queue a pending action)
                           5. audit              (always, whatever the outcome)

Kept free of MCP imports so it can be unit-tested directly.
"""
from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.engine import Engine

from . import approvals, audit, services
from .config import Settings
from .guard import Guard, PermissionDenied, RateLimited
from .services import DomainError

MAX_LIST_LIMIT = 50
MIN_REASON_LEN = 10

APPROVAL_MESSAGE = (
    "This change is queued and has NOT been applied. A human must approve it in the approval "
    "dashboard. Do not retry; call get_action_status with this action_id to check progress."
)


class ToolGateway:
    def __init__(self, engine: Engine, settings: Settings, guard: Guard | None = None, actor: str = "agent"):
        self.engine = engine
        self.settings = settings
        self.actor = actor
        self.guard = guard or Guard(settings.scopes, settings.read_rate_per_minute, settings.write_rate_per_minute)

    # ------------------------------------------------------------ core pipeline

    def _call(self, tool: str, scope: str, params: dict[str, Any], fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            self.guard.check(scope)
            payload = fn()
            response, outcome = {"ok": True, **payload}, payload.get("_outcome", "ok")
            response.pop("_outcome", None)
        except PermissionDenied as e:
            response, outcome = {"ok": False, "error": str(e), "error_type": "permission_denied"}, "denied"
        except RateLimited as e:
            response, outcome = {"ok": False, "error": str(e), "error_type": "rate_limited"}, "rate_limited"
        except DomainError as e:
            response, outcome = {"ok": False, "error": str(e), "error_type": "invalid_request"}, "rejected_input"
        except Exception:  # never leak internals (stack traces, SQL) to the model
            response, outcome = {"ok": False, "error": "internal error", "error_type": "internal"}, "error"
        with self.engine.begin() as conn:
            audit.record(conn, actor=self.actor, event="tool_call", tool=tool, params=params, outcome=outcome,
                         details={"action_id": response.get("action_id")} if "action_id" in response else None)
        return response

    def _queue(self, tool: str, params: dict[str, Any], reason: str, idempotency_key: str | None) -> dict[str, Any]:
        reason = (reason or "").strip()
        if len(reason) < MIN_REASON_LEN:
            raise DomainError(f"a reason of at least {MIN_REASON_LEN} characters is required for any change")
        if idempotency_key is not None and not (8 <= len(idempotency_key) <= 64):
            raise DomainError("idempotency_key must be 8-64 characters")
        action, created = approvals.request_action(
            self.engine, tool=tool, params=params, reason=reason, requested_by=self.actor,
            ttl_minutes=self.settings.action_ttl_minutes, idempotency_key=idempotency_key,
        )
        return {
            "status": "pending_approval" if action["status"] == "pending" else action["status"],
            "action_id": action["id"],
            "duplicate": not created,
            "message": APPROVAL_MESSAGE if action["status"] == "pending" else f"This request was already {action['status']}.",
            "_outcome": "queued" if created else "duplicate",
        }

    # ------------------------------------------------------------ read tools

    def get_order(self, order_id: int) -> dict[str, Any]:
        def run():
            with self.engine.connect() as conn:
                return {"order": services.get_order(conn, int(order_id))}
        return self._call("get_order", "orders:read", {"order_id": order_id}, run)

    def search_orders(self, customer_email: str | None = None, status: str | None = None, limit: int = 20) -> dict[str, Any]:
        params = {"customer_email": customer_email, "status": status, "limit": limit}

        def run():
            if customer_email is not None and len(customer_email) > 254:
                raise DomainError("customer_email too long")
            capped = max(1, min(int(limit), MAX_LIST_LIMIT))
            with self.engine.connect() as conn:
                rows = services.search_orders(conn, customer_email, status, capped)
            return {"orders": rows, "count": len(rows), "limit_applied": capped}
        return self._call("search_orders", "orders:read", params, run)

    def list_products(self, low_stock_threshold: int | None = None) -> dict[str, Any]:
        def run():
            with self.engine.connect() as conn:
                rows = services.list_products(conn, low_stock_threshold)
            return {"products": rows, "count": len(rows)}
        return self._call("list_products", "inventory:read", {"low_stock_threshold": low_stock_threshold}, run)

    def check_stock(self, sku: str) -> dict[str, Any]:
        def run():
            with self.engine.connect() as conn:
                p = services.get_product(conn, sku)
            return {"sku": p["sku"], "name": p["name"], "stock": p["stock"]}
        return self._call("check_stock", "inventory:read", {"sku": sku}, run)

    def get_action_status(self, action_id: int) -> dict[str, Any]:
        def run():
            a = approvals.get_action(self.engine, int(action_id))
            return {"action_id": a["id"], "tool": a["tool"], "status": a["status"],
                    "result": a["result"], "decision_note": a["decision_note"]}
        # Agents may only check actions; reading status is harmless, so it needs only a read scope.
        return self._call("get_action_status", "orders:read", {"action_id": action_id}, run)

    # ------------------------------------------------------------ write tools (approval-gated)

    def request_order_cancellation(self, order_id: int, reason: str, idempotency_key: str | None = None) -> dict[str, Any]:
        params = {"order_id": int(order_id)}

        def run():
            with self.engine.connect() as conn:
                services.validate_cancel_order(conn, params["order_id"])
            return self._queue("cancel_order", params, reason, idempotency_key)
        return self._call("request_order_cancellation", "orders:write", {**params, "reason": reason}, run)

    def request_refund(self, order_id: int, amount_cents: int, reason: str, idempotency_key: str | None = None) -> dict[str, Any]:
        params = {"order_id": int(order_id), "amount_cents": int(amount_cents)}

        def run():
            with self.engine.connect() as conn:
                services.validate_refund(conn, params["order_id"], params["amount_cents"])
            return self._queue("refund_order", params, reason, idempotency_key)
        return self._call("request_refund", "orders:write", {**params, "reason": reason}, run)

    def request_stock_adjustment(self, sku: str, delta: int, reason: str, idempotency_key: str | None = None) -> dict[str, Any]:
        params = {"sku": sku, "delta": int(delta)}

        def run():
            with self.engine.connect() as conn:
                services.validate_stock_adjustment(conn, sku, params["delta"], self.settings.max_stock_delta)
            return self._queue("adjust_stock", params, reason, idempotency_key)
        return self._call("request_stock_adjustment", "inventory:write", {**params, "reason": reason}, run)
