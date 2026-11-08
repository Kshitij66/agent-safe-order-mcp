import pytest

from order_mcp.guard import Guard, PermissionDenied, RateLimited, TokenBucket
from conftest import FakeClock


def test_token_bucket_burst_then_refill():
    clock = FakeClock()
    b = TokenBucket(capacity=3, rate_per_sec=1.0, clock=clock)
    assert [b.try_acquire() for _ in range(4)] == [True, True, True, False]
    clock.t = 1.0
    assert b.try_acquire() and not b.try_acquire()
    clock.t = 100.0  # refill is capped at capacity
    assert sum(b.try_acquire() for _ in range(10)) == 3


def test_scope_denied():
    g = Guard(frozenset({"orders:read"}), 10, 10, FakeClock())
    g.check("orders:read")
    with pytest.raises(PermissionDenied):
        g.check("orders:write")


def test_reads_and_writes_have_separate_buckets():
    g = Guard(frozenset({"orders:read", "orders:write"}), read_per_minute=100, write_per_minute=2, clock=FakeClock())
    g.check("orders:write"); g.check("orders:write")
    with pytest.raises(RateLimited):
        g.check("orders:write")
    g.check("orders:read")  # reads still allowed
