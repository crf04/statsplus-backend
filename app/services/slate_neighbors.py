"""The nearest Slate Dates on either side of one Slate Date that list games.

A Slate lists the games of ``schedule_season(date)`` bucketed by US Eastern day
(``slate_time``), minus All-Star events.  A neighbour is therefore an event
that the Slate of its own ET date would list: it must belong to the season
that date reads and must not be an All-Star event.  Each search walks the
(season, scheduled_at) index outward from the Slate Date, a page at a time, and
stops at the first listed event.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
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

_Fetch = Callable[[str, "tuple[datetime, str] | None"], "list[dict[str, Any]]"]


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
    containing = current_nba_season(slate_date)

    def seasons_down(first: str, *, inclusive: bool):
        season = catalog.get_adjacent_season(first, below=True, inclusive=inclusive)
        while season is not None:
            yield season
            season = catalog.get_adjacent_season(season, below=True)

    def seasons_up(first: str, *, inclusive: bool):
        season = catalog.get_adjacent_season(first, below=False, inclusive=inclusive)
        while season is not None:
            yield season
            season = catalog.get_adjacent_season(season, below=False)

    if season_is_pinned(settings):
        earlier, later = (
            iter([settings.nba.current_season]),
            iter([settings.nba.current_season]),
        )
    else:
        # A listed event of date d is in current_nba_season(d) or the season
        # before it, and d lies on the requested side of the Slate Date.
        earlier = seasons_down(containing, inclusive=True)
        later = seasons_up(previous_nba_season(containing), inclusive=True)

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

    def search(seasons: Iterable[str], fetch: _Fetch) -> str | None:
        for season in seasons:
            cursor: tuple[datetime, str] | None = None
            while True:
                page = fetch(season, cursor)
                for event in page:
                    day = listed(event)
                    if day is not None:
                        return day.isoformat()
                if len(page) < _PAGE:
                    break
                last = page[-1]
                cursor = (parse_utc_iso(str(last["scheduled_at"])), last["nba_game_id"])
        return None

    def before(season: str, cursor: tuple[datetime, str] | None):
        at, game_id = (start, None) if cursor is None else cursor
        return catalog.get_events_before(season, at, _PAGE, before_id=game_id)

    def after(season: str, cursor: tuple[datetime, str] | None):
        at, game_id = (end, None) if cursor is None else cursor
        return catalog.get_events_after(season, at, _PAGE, after_id=game_id)

    return search(earlier, before), search(later, after)
