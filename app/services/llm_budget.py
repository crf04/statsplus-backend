"""A per-user daily cap on paid OpenAI fallback calls (crf04/statsplus#157).

The cap is a counter per uid per UTC day in the limiter's store (Redis in
production), so every worker draws on one budget.  Keys expire two days out,
so a store with ``allkeys-lru`` never has to keep them.  When the store fails
the call is allowed and a warning is logged, like the request limits; the
OpenAI project's spend ceiling is the hard backstop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from limits.storage import Storage

logger = logging.getLogger(__name__)

DAILY_LLM_FALLBACK_LIMIT = 50
_KEY_TTL_SECONDS = 2 * 24 * 60 * 60


class LLMDailyBudget:
    """Counts OpenAI fallback calls per user per UTC day."""

    def __init__(
        self,
        storage: Storage,
        *,
        limit: int = DAILY_LLM_FALLBACK_LIMIT,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._storage = storage
        self._limit = limit
        self._now = now

    def try_consume(self, uid: str) -> bool:
        """Spend one call for ``uid``; False once today's budget is gone."""

        key = f"llm_fallback/{uid}/{self._now().astimezone(timezone.utc).strftime('%Y-%m-%d')}"
        try:
            used = self._storage.incr(key, _KEY_TTL_SECONDS)
        except Exception as error:
            logger.warning(
                "LLM fallback budget store unavailable; allowing the call: %s", error
            )
            return True
        return used <= self._limit
