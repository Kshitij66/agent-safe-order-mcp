"""Race conditions. These need a real multi-writer database, so they run only on Postgres
(set TEST_DATABASE_URL). SQLite serialises all writers, so the race can't happen there."""
import threading

import pytest

from order_mcp import approvals, services
from order_mcp.services import InvalidState

pytestmark = pytest.mark.skipif(
    not __import__("os").getenv("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="needs Postgres",
)


def run_concurrently(fn, n):
    barrier = threading.Barrier(n)
    results = []

    def worker(i):
        barrier.wait()  # release all threads at the same instant
        try:
            results.append(("ok", fn(i)))
        except InvalidState as e:
            results.append(("conflict", str(e)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_double_click_approve_executes_exactly_once(engine):
    action, _ = approvals.request_action(engine, tool="refund_order", params={"order_id": 2, "amount_cents": 49900},
                                         reason="damaged cable", requested_by="agent", ttl_minutes=30)
    results = run_concurrently(lambda i: approvals.approve(engine, action["id"], f"human:{i}"), 10)
    assert sum(r[0] == "ok" for r in results) == 1
    assert sum(r[0] == "conflict" for r in results) == 9
    with engine.connect() as c:
        assert services.get_order(c, 2)["refunded_cents"] == 49900  # not 10x


def test_parallel_refunds_never_exceed_order_total(engine):
    # Order 7 total = 699800. Ten different approved refunds of 100000 each = 1,000,000 requested.
    ids = []
    for i in range(10):
        a, _ = approvals.request_action(engine, tool="refund_order", params={"order_id": 7, "amount_cents": 100000},
                                        reason="partial refund", requested_by="agent", ttl_minutes=30,
                                        idempotency_key=f"refund-7-part-{i}")
        ids.append(a["id"])
    results = run_concurrently(lambda i: approvals.approve(engine, ids[i], "human:ops"), 10)
    statuses = [r[1]["status"] for r in results]
    assert statuses.count("executed") == 6 and statuses.count("failed") == 4
    with engine.connect() as c:
        assert services.get_order(c, 7)["refunded_cents"] == 600000


def test_parallel_identical_requests_queue_one_action(engine):
    results = run_concurrently(lambda i: approvals.request_action(
        engine, tool="cancel_order", params={"order_id": 1}, reason="cancel please",
        requested_by="agent", ttl_minutes=30), 10)
    ids = {r[1][0]["id"] for r in results}
    created = sum(r[1][1] for r in results)
    assert len(ids) == 1 and created == 1
