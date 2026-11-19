"""Token-bucket rate limiting (idea from the author's rate-gate project, re-implemented).

A bucket holds up to `burst` tokens and refills continuously at `rate` tokens per
second. `acquire()` blocks until a token is available or the timeout passes. The
clock and sleep functions are injectable so tests run without real waiting.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    def __init__(
        self,
        rate: float,
        burst: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0 or burst <= 0:
            raise ValueError("rate and burst must be positive")
        self.rate = rate
        self.burst = burst
        self._clock = clock
        self._sleep = sleep
        self._tokens = burst
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def try_acquire(self, n: float = 1.0) -> float:
        """Take n tokens if possible and return 0.0, else return the seconds to wait."""
        with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return 0.0
            return (n - self._tokens) / self.rate

    def acquire(self, n: float = 1.0, timeout: float = 120.0) -> bool:
        deadline = self._clock() + timeout
        while True:
            wait = self.try_acquire(n)
            if wait == 0.0:
                return True
            if self._clock() + wait > deadline:
                return False
            self._sleep(wait)
