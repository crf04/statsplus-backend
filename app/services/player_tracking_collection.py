"""Collect rebound chances and potential assists for served regular-season games.

Rebound chances come from ``boxscoreplayertrackv3`` (one call per game),
potential assists from the Passing day feed of ``leaguedashptstats`` (one call
per game day, matched to games by player and date).  Every run reconciles: it
collects each completed regular-season game of the season with no stored
tracking rows, so a game whose tracking is not published yet (``pending``) is
picked up by a later run (crf04/statsplus#349).

A game is stored only once both feeds have published for it: the box score has
a player with minutes, and the day feed lists at least one of those players.
A player missing from the day feed stores ``potential_ast = NULL``, never 0.
One game's failure never blocks another.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from app.domain.nba_events import (
    REGULAR_SEASON_TYPE,
    is_completed_non_postponed_event,
    player_game_log_season_type,
)
from app.domain.slate_time import slate_date_for_instant
from app.domain.utc import parse_utc_iso
from app.errors import sanitize_public_value
from app.utils.telemetry import ProviderResponseError

logger = logging.getLogger(__name__)

_REGULAR_SEASON_GAME_ID = re.compile(r"002\d{7}\Z")


class PlayerTrackingCollectionService:
    """Fetch, match and store the tracking values of games that have none."""

    def __init__(
        self,
        provider: Any,
        repository: Any,
        events: Callable[[str], Iterable[Mapping[str, Any]]],
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.repository = repository
        self.events = events
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep

    def _eligible(self, season: str) -> dict[str, date]:
        """Completed regular-season games, oldest first, with their Eastern game date."""

        events = sorted(self.events(season), key=lambda event: str(event["scheduled_at"]))
        return {
            str(event["nba_game_id"]): slate_date_for_instant(parse_utc_iso(str(event["scheduled_at"])))
            for event in events
            if is_completed_non_postponed_event(event)
            and player_game_log_season_type(event) == REGULAR_SEASON_TYPE
        }

    def collect(
        self,
        season: str,
        *,
        max_games: int | None = None,
        throttle_seconds: float = 0.0,
        game_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        eligible = self._eligible(season)
        if game_ids is None:
            stored = self.repository.stored_game_ids(season)
            pending = [game_id for game_id in eligible if game_id not in stored]
        else:
            pending = list(dict.fromkeys(str(game_id) for game_id in game_ids))
            invalid = [g for g in pending if _REGULAR_SEASON_GAME_ID.match(g) is None or g not in eligible]
            if invalid:
                raise ValueError(f"not completed regular-season games of {season}: {', '.join(invalid)}")
        batch = pending if max_games is None else pending[:max_games]
        stored_ids: list[str] = []
        unchanged: list[str] = []
        not_published: list[str] = []
        failed: list[dict[str, str]] = []
        day_feeds: dict[date, dict[int, int | None]] = {}
        for index, game_id in enumerate(batch):
            if index and throttle_seconds:
                self._sleep(throttle_seconds)
            game_date = eligible[game_id]
            try:
                box = self.provider.fetch_game_rebound_chances(game_id)
                played = [player_id for player_id, line in box.items() if line["minutes"]]
                if not played:
                    not_published.append(game_id)
                    continue
                if game_date not in day_feeds:
                    day_feeds[game_date] = self.provider.fetch_day_potential_assists(season, game_date)
            except (OSError, ProviderResponseError, ValueError) as error:
                logger.warning("tracking fetch failed for %s: %s", game_id, error)
                failed.append({"game_id": game_id, "error": sanitize_public_value(str(error))})
                continue
            day = day_feeds[game_date]
            if not any(player_id in day for player_id in played):
                not_published.append(game_id)
                continue
            rows = [
                {
                    "player_id": player_id,
                    "reb_chances": line["total"],
                    "oreb_chances": line["offensive"],
                    "dreb_chances": line["defensive"],
                    "potential_ast": day.get(player_id),
                }
                for player_id, line in box.items()
            ]
            changed = self.repository.replace_game(season, game_id, game_date, rows, self._clock())
            (stored_ids if changed else unchanged).append(game_id)
        return {
            "season": season,
            "candidates": len(pending),
            "attempted": len(batch),
            "stored": len(stored_ids),
            "stored_game_ids": stored_ids,
            "unchanged": len(unchanged),
            "not_published": not_published,
            "failed": failed,
            "remaining": len(pending) - len(batch),
        }


__all__ = ["PlayerTrackingCollectionService"]
