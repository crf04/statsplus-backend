"""Stored daily lineups: the collection step and the rotation team view's read (crf04/statsplus#492).

``collect_team_lineups`` reads today's (US Eastern) feed document and replaces
the stored row of every team it lists.  ``scripts/collect_odds.py`` runs it on
every five-minute odds beat; it never raises, so it cannot fail that script.
``TeamLineupRepository.read`` finds the team's game in the Event Catalog and
serves the stored lineup for it; no request fetches the feed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import and_, delete, insert, or_, select
from sqlalchemy.engine import Engine

from app.domain.nba_events import is_postponed_event
from app.domain.utc import assume_utc
from app.models.event_catalog import EventCatalogEntry
from app.models.team_lineup import TeamLineup
from app.providers import nba_daily_lineups
from app.providers.nba_daily_lineups import EASTERN, parse_daily_lineups
from app.services.event_catalog_repository import EventCatalogRepository

logger = logging.getLogger(__name__)


def collect_team_lineups(
    engine: Engine,
    *,
    fetch: Callable[[date], Any] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Store today's feed document; report the outcome instead of raising."""

    now = assume_utc((clock or (lambda: datetime.now(UTC)))())
    day = now.astimezone(EASTERN).date()
    try:
        payload = (fetch or nba_daily_lineups.fetch_daily_lineups)(day)
        if payload is None:
            return {"state": "not_published", "date": day.isoformat()}
        lineups = parse_daily_lineups(payload)
        with engine.begin() as connection:
            for lineup in lineups:
                key = and_(
                    TeamLineup.game_id == lineup.game_id,
                    TeamLineup.team_id == lineup.team_id,
                )
                connection.execute(delete(TeamLineup.__table__).where(key))
            if lineups:
                connection.execute(
                    insert(TeamLineup.__table__),
                    [
                        {
                            "game_id": lineup.game_id,
                            "team_id": lineup.team_id,
                            "tricode": lineup.tricode,
                            "game_date": day,
                            # SQLite keeps an aware value's wall time and drops its offset.
                            "feed_timestamp": (
                                None
                                if lineup.feed_timestamp is None
                                else lineup.feed_timestamp.astimezone(UTC)
                            ),
                            "starters": json.dumps([s.as_json() for s in lineup.starters]),
                            "inactive": json.dumps([p.as_json() for p in lineup.inactive]),
                            "collected_at": now,
                        }
                        for lineup in lineups
                    ],
                )
    except Exception as error:
        # Whatever goes wrong, the odds beat that hosts this step carries on.
        logger.warning("daily lineups collection failed for %s: %r", day, error)
        return {"state": "failed", "date": day.isoformat(), "error": type(error).__name__}
    return {"state": "stored", "date": day.isoformat(), "teams": len(lineups)}


class TeamLineupRepository:
    """Reads the stored lineup of a team's game."""

    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] | None = None) -> None:
        self.engine = engine
        self._clock = clock or (lambda: datetime.now(UTC))

    def read(self, team: str, *, day: date | None = None) -> dict[str, Any]:
        """The ``lineup`` block for ``team``'s game on ``day``, or today's or its next game."""

        start = day or assume_utc(self._clock()).astimezone(EASTERN).date()
        events = EventCatalogEntry.__table__
        lineups = TeamLineup.__table__
        window = events.c.scheduled_at >= _eastern_midnight(start)
        if day is not None:
            window = and_(window, events.c.scheduled_at < _eastern_midnight(day + timedelta(days=1)))
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(events)
                .where(
                    window,
                    or_(events.c.home_team_tricode == team, events.c.away_team_tricode == team),
                )
                .order_by(events.c.scheduled_at, events.c.nba_game_id)
            ).mappings()
            event = next(
                (row for row in rows if not is_postponed_event(EventCatalogRepository._serialize(row))),
                None,
            )
            if event is None:
                return _block("no_game")
            home = event["home_team_tricode"] == team
            team_id = event["home_team_id"] if home else event["away_team_id"]
            game_date = assume_utc(event["scheduled_at"]).astimezone(EASTERN).date()
            stored = (
                connection.execute(
                    select(lineups).where(
                        lineups.c.game_id == event["nba_game_id"],
                        lineups.c.team_id == team_id,
                        lineups.c.game_date == game_date,
                    )
                )
                .mappings()
                .first()
            )
        game = {
            "game_id": event["nba_game_id"],
            "game_date": game_date.isoformat(),
            "matchup": (
                f"{team} vs. {event['away_team_tricode']}"
                if home
                else f"{team} @ {event['home_team_tricode']}"
            ),
        }
        if stored is None:
            return _block("not_posted", **game)
        starters = json.loads(stored["starters"])
        feed_timestamp = stored["feed_timestamp"]
        return _block(
            "posted" if starters else "not_posted",
            **game,
            feed_timestamp=None
            if feed_timestamp is None
            else assume_utc(feed_timestamp).astimezone(EASTERN).isoformat(),
            collected_at=assume_utc(stored["collected_at"]).isoformat(),
            starters=starters,
            inactive=json.loads(stored["inactive"]),
        )


def _eastern_midnight(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=EASTERN).astimezone(UTC)


def _block(status: str, **values: Any) -> dict[str, Any]:
    empty: Mapping[str, Any] = {
        "game_id": None,
        "game_date": None,
        "matchup": None,
        "feed_timestamp": None,
        "collected_at": None,
        "starters": [],
        "inactive": [],
    }
    return {"status": status, **empty, **values}
