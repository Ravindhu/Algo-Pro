"""Order-rate throttle: token bucket. Keeps orders under exchange per-second limits.

Flow stage: Order-rate throttle
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/throttle.py
"""
from __future__ import annotations

import time
from typing import Callable


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float) -> None:
        self.rate = rate_per_sec
        self.capacity = capacity
        self.tokens = capacity
        self.last = 0.0

    def allow(self, now: float) -> bool:
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


def acquire(bucket: TokenBucket, clock: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep, timeout: float = 2.0) -> bool:
    """Wait for a token for up to `timeout` seconds. False means the order must not be sent."""
    waited = 0.0
    while True:
        if bucket.allow(clock()):
            return True
        if waited >= timeout:
            return False
        sleep(0.05)
        waited += 0.05
