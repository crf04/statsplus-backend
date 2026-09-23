"""Shadow sampling: measure how often a confident NLP parse disagrees with the LLM.

A query the deterministic parser is confident about never reaches the LLM, so
its mistakes are invisible. With ``LLM_SHADOW_SAMPLE_RATE`` above zero, that
fraction of confident queries is also parsed by the LLM in the background and
the two Filter Sets are compared field by field. The response is always the
NLP result and never waits for the LLM; the comparison only produces one
structured ``nl_shadow`` log line, which ``scripts/summarize_nl_shadow.py``
turns into an agreement rate.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

logger = logging.getLogger(__name__)

LOG_PREFIX = "nl_shadow"

# The fields that decide which games a Filter Set returns. Confidence, intent
# and time_period describe the parse rather than the filters, so a difference
# there is not a disagreement about the answer.
COMPARED_FIELDS = (
    "player_name",
    "team_name",
    "game_count",
    "location",
    "players_on",
    "players_off",
    "opponent_filters",
    "minutes_filter",
    "self_filters",
    "date_filter",
    "season",
)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text.casefold() or None


def _number(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _self_filter(entry: Any) -> tuple:
    """One self filter as (stat, operator, value, value2), from either parser."""
    read = entry.get if isinstance(entry, dict) else lambda key: getattr(entry, key, None)
    stat = read("stat_column") or read("stat")
    return (
        str(stat).upper(),
        str(read("operator")),
        _number(read("value")),
        _number(read("value2")),
    )


def comparable_view(result: dict) -> dict:
    """Normalize one ``/api/nl-query`` result for comparison.

    The NLP path returns dataclasses and tuples while the LLM path returns
    dicts and lists; both describe the same Filter Set, so only differences
    that change which games match should survive normalization.
    """
    location = _text(result.get("location"))
    minutes = result.get("minutes_filter")
    opponents = zip(result.get("teams_against") or [], result.get("rank_filter") or [])
    return {
        "player_name": _text(result.get("player_name")),
        "team_name": _text(result.get("team_name")),
        "game_count": result.get("game_count"),
        # "both" is the absence of a location filter.
        "location": None if location == "both" else location,
        "players_on": sorted(_text(name) for name in result.get("players_on") or []),
        "players_off": sorted(_text(name) for name in result.get("players_off") or []),
        "opponent_filters": sorted((str(team), str(rank)) for team, rank in opponents),
        "minutes_filter": [int(bound) for bound in minutes] if minutes else None,
        "self_filters": sorted(_self_filter(entry) for entry in result.get("self_filters") or []),
        "date_filter": _text(result.get("date_filter")),
        "season": _text(result.get("season")),
    }


def differences(nlp_view: dict, llm_view: dict) -> dict:
    """The compared fields whose normalized values differ, with both values."""
    return {
        field: {"nlp": nlp_view[field], "llm": llm_view[field]}
        for field in COMPARED_FIELDS
        if nlp_view[field] != llm_view[field]
    }


class ShadowSampler:
    """Run a sampled fraction of shadow comparisons off the request thread.

    At most ``max_in_flight`` comparisons run or wait at once. A sample that
    arrives while that many are outstanding is dropped rather than queued, so a
    slow LLM can never build an unbounded backlog behind live traffic.
    """

    def __init__(
        self,
        rate: float,
        *,
        max_in_flight: int = 4,
        executor=None,
        rng: Callable[[], float] = random.random,
    ):
        self.rate = rate
        self._rng = rng
        self._slots = threading.BoundedSemaphore(max_in_flight)
        self._executor = executor or ThreadPoolExecutor(
            max_workers=min(2, max_in_flight), thread_name_prefix="nl-shadow"
        )

    def maybe_submit(self, comparison: Callable[[], None]) -> bool:
        """Schedule ``comparison`` for this query if it is sampled and a slot is free."""
        if self.rate <= 0 or self._rng() >= self.rate:
            return False
        if not self._slots.acquire(blocking=False):
            logger.info("%s dropped: too many comparisons in flight", LOG_PREFIX)
            return False

        def run():
            try:
                comparison()
            except Exception:  # pragma: no cover - comparisons log their own errors
                logger.exception("%s comparison crashed", LOG_PREFIX)
            finally:
                self._slots.release()

        try:
            self._executor.submit(run)
        except Exception:
            self._slots.release()
            logger.exception("%s could not be scheduled", LOG_PREFIX)
            return False
        return True


def log_comparison(query: str, nlp_confidence: float, started: float, *,
                   diff: dict | None = None, error: Exception | None = None,
                   clock: Callable[[], float] = time.monotonic) -> dict:
    """Emit the one structured line a shadow comparison produces."""
    if error is not None:
        outcome = "error"
    else:
        outcome = "disagree" if diff else "agree"
    record = {
        "outcome": outcome,
        "query": query,
        "nlp_confidence": round(float(nlp_confidence or 0.0), 3),
        "latency_ms": round((clock() - started) * 1000),
    }
    if diff:
        record["differences"] = diff
    if error is not None:
        record["error"] = f"{type(error).__name__}: {error}"
    logger.info("%s %s", LOG_PREFIX, json.dumps(record, sort_keys=True, default=str))
    return record
