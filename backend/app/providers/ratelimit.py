"""A shared token bucket plus circuit breaker for upstream shop APIs.

Every provider call goes through one of these. Polling a shop every few
seconds is only sustainable if the whole process shares one budget, so the
limiter is per-provider and process-wide rather than per-watch.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


class RateLimitExceeded(RuntimeError):
    pass


class CircuitOpen(RuntimeError):
    """Raised while the breaker is open after repeated upstream failures."""


@dataclass
class TokenBucket:
    rate_per_minute: int
    burst: int = 0
    _tokens: float = field(default=0.0, init=False)
    _updated: float = field(default_factory=time.monotonic, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        if not self.burst:
            self.burst = max(1, self.rate_per_minute // 4)
        self._tokens = float(self.burst)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        self._updated = now
        self._tokens = min(self.burst, self._tokens + elapsed * (self.rate_per_minute / 60.0))

    def acquire(self, timeout: float = 30.0) -> None:
        """Block until a token is available, or raise RateLimitExceeded."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                needed = (1.0 - self._tokens) / (self.rate_per_minute / 60.0)
            if time.monotonic() + needed > deadline:
                raise RateLimitExceeded(
                    f"no token within {timeout:.0f}s (limit {self.rate_per_minute}/min)"
                )
            # Jitter keeps concurrent workers from waking in lockstep.
            time.sleep(min(needed, 1.0) + random.uniform(0, 0.25))

    def update_rate(self, rate_per_minute: int) -> None:
        with self._lock:
            self.rate_per_minute = max(1, rate_per_minute)
            self.burst = max(1, self.rate_per_minute // 4)


@dataclass
class CircuitBreaker:
    """Trips after `threshold` consecutive failures, recovers on a timer."""

    threshold: int = 5
    reset_after: float = 120.0
    max_reset_after: float = 1800.0

    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _backoff: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at >= self._backoff:
                # Half-open: let the next call through as a probe.
                self._opened_at = None
                return False
            return True

    def check(self) -> None:
        if self.is_open:
            with self._lock:
                remaining = self._backoff - (time.monotonic() - (self._opened_at or 0))
            raise CircuitOpen(f"upstream circuit open, retrying in {remaining:.0f}s")

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._backoff = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._backoff = min(
                    self.max_reset_after,
                    self.reset_after * (2 ** (self._failures - self.threshold)),
                )
                self._opened_at = time.monotonic()
                log.warning(
                    "Circuit opened after %s failures, backing off %.0fs",
                    self._failures,
                    self._backoff,
                )

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "open": self._opened_at is not None,
                "failures": self._failures,
                "backoff_seconds": round(self._backoff, 1),
            }
