"""Permission scopes and rate limiting for agent tool calls."""
from __future__ import annotations

import threading
import time
from typing import Callable


class PermissionDenied(Exception):
    pass


class RateLimited(Exception):
    pass


class TokenBucket:
    """Classic token bucket: holds up to `capacity` tokens, refills at `rate_per_sec`.

    Each call spends one token. Bursts up to `capacity` are allowed; sustained
    traffic is capped at the refill rate.
    """

    def __init__(self, capacity: int, rate_per_sec: float, clock: Callable[[], float] = time.monotonic):
        self.capacity = capacity
        self.rate = rate_per_sec
        self.clock = clock
        self.tokens = float(capacity)
        self.updated = clock()
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            now = self.clock()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


class Guard:
    def __init__(
        self,
        scopes: frozenset[str],
        read_per_minute: int,
        write_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.scopes = scopes
        self.buckets = {
            "read": TokenBucket(read_per_minute, read_per_minute / 60, clock),
            "write": TokenBucket(write_per_minute, write_per_minute / 60, clock),
        }

    def check(self, scope: str) -> None:
        if scope not in self.scopes:
            raise PermissionDenied(f"agent lacks scope {scope!r}")
        kind = "write" if scope.endswith(":write") else "read"
        if not self.buckets[kind].try_acquire():
            raise RateLimited(f"{kind} rate limit exceeded; slow down")
