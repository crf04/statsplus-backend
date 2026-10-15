"""The nearest Slate Dates on either side of one Slate Date that list games.

A Slate lists the games of ``schedule_season(date)`` bucketed by US Eastern day
(``slate_time``), minus All-Star events.  A neighbour is therefore an event
that the Slate of its own ET date would list: it must belong to the season
that date reads and must not be an All-Star event.  Each search walks the
(season, scheduled_at) index outward from the Slate Date, a page at a time, and
stops at the first listed event.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from app.config.settings import current_nba_season
from app.domain.nba_events import (
    is_all_star_kind,
    resolve_stored_event_classification,
)
from app.domain.slate_time import slate_date_for_instant, slate_day_bounds_utc
from app.domain.utc import parse_utc_iso
from app.services.research_season import (
    previous_nba_season,
    schedule_season,
    season_is_pinned,
)

_PAGE = 50

_Fetch = Callable[[str, "datetime | None", int], "list[dict[str, Any]]"]


class _CountMemo:
    """``count_events`` answered once per season for one neighbour search."""

    def __init__(self, catalog: Any) -> None:
        self._catalog = catalog
        self._counts: dict[str, int] = {}

    def count_events(self, season: str) -> int:
        if season not in self._counts:
            self._counts[season] = self._catalog.count_events(season)
        return self._counts[season]


def neighbouring_game_dates(
    settings: Any, catalog: Any, slate_date: date
) -> tuple[str | None, str | None]:
    """``(previous, next)`` Slate Dates, ISO strings or ``None``, around a date."""

    counts = _CountMemo(catalog)
    start, end = slate_day_bounds_utc(slate_date)
    if season_is_pinned(settings):
        earlier = later = [settings.nba.current_season]
    else:
        stored = sorted(catalog.get_seasons())
        containing = current_nba_season(slate_date)
        # A listed event of date d is in current_nba_season(d) or the season
        # before it, and d lies on the requested side of the Slate Date.
        earlier = [s for s in reversed(stored) if s <= containing]
        floor = previous_nba_season(containing)
        later = [s for s in stored if s >= floor]

    def listed(event: dict[str, Any]) -> date | None:
        classification = resolve_stored_event_classification(
            str(event.get("nba_game_id", "")),
            str(event.get("classification") or ""),
        )
        if is_all_star_kind(classification.kind):
            return None
        day = slate_date_for_instant(parse_utc_iso(str(event["scheduled_at"])))
        if schedule_season(settings, counts, day) != event["season"]:
            return None
        return day

    def search(seasons: list[str], fetch: _Fetch) -> str | None:
        for season in seasons:
            seen: set[str] = set()
            bound: datetime | None = None
            while True:
                limit = _PAGE + len(seen)
                page = fetch(season, bound, limit)
                fresh = [e for e in page if e["nba_game_id"] not in seen]
                for event in fresh:
                    seen.add(event["nba_game_id"])
                    day = listed(event)
                    if day is not None:
                        return day.isoformat()
                if not fresh or len(page) < limit:
                    break
                bound = parse_utc_iso(str(page[-1]["scheduled_at"]))
        return None

    def before(season: str, bound: datetime | None, limit: int):
        return catalog.get_events_before(
            season, start if bound is None else bound, limit,
            inclusive=bound is not None,
        )

    def after(season: str, bound: datetime | None, limit: int):
        return catalog.get_events_after(
            season, end if bound is None else bound, limit
        )

    return search(earlier, before), search(later, after)
