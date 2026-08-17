"""America/New_York slate-day timestamp boundaries."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.domain.utc import assume_utc


EASTERN = ZoneInfo("America/New_York")


def slate_day_bounds_utc(slate_date: date) -> tuple[datetime, datetime]:
    """Return the inclusive start and exclusive end of one Eastern slate day."""

    start = datetime.combine(slate_date, time.min, EASTERN).astimezone(timezone.utc)
    next_date = slate_date + timedelta(days=1)
    end = datetime.combine(next_date, time.min, EASTERN).astimezone(timezone.utc)
    return start, end


def slate_day_end_utc(slate_date: date) -> datetime:
    """Return the exclusive UTC instant at the end of one Eastern slate day."""

    return slate_day_bounds_utc(slate_date)[1]


def publication_cutoff_is_after_slate_day(
    cutoff: datetime,
    slate_date: date,
) -> bool:
    """Whether a publication falls at or beyond the slate day's end boundary."""

    return assume_utc(cutoff) >= slate_day_end_utc(slate_date)


__all__ = [
    "publication_cutoff_is_after_slate_day",
    "slate_day_bounds_utc",
    "slate_day_end_utc",
]
