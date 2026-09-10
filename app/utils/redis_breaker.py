"""A shared Redis circuit breaker for every Redis-backed seam.

A Redis outage must degrade to a miss, never a 5xx, so readers that touch
Redis share one small breaker: once a failure is recorded, reads skip Redis
entirely for a cooldown before trying again.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

REDIS_FAILURE_COOLDOWN_SECONDS = 30


class RedisCircuitBreaker:
    """Record Redis failures and skip calls while the circuit is open."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        cooldown: float = REDIS_FAILURE_COOLDOWN_SECONDS,
        message: str = "Redis unavailable (%s); bypassing cache for %ss",
    ) -> None:
        self._clock = clock
        self._cooldown = cooldown
        self._message = message
        self._lock = threading.Lock()
        self._open_until = 0.0

    def is_open(self) -> bool:
        with self._lock:
            return self._clock() < self._open_until

    def record_failure(self, error: Exception) -> None:
        """Open the circuit for one more cooldown, warning only when new."""

        with self._lock:
            now = self._clock()
            already_open = now < self._open_until
            self._open_until = now + self._cooldown
        if already_open:
            return
        logger.warning(self._message, error, self._cooldown)
