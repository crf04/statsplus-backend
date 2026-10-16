"""Collect completed regular-season games' rotations from NBA LiveData.

For every completed, non-postponed regular-season game in the Event Catalog
that has no stored rotation, fetch the LiveData box score and play-by-play,
derive the rotation and store it whole (crf04/statsplus#223).  One game never
blocks another:

* ``stored``: derived, validated and written (a rerun rewrites identical rows);
* ``rejected``: the documents were read but do not yield a trustworthy
  rotation, so nothing was stored; reported with the reason and remembered.
  After :data:`MAX_REJECTIONS` rejections a game is ``given_up`` (listed in
  every summary, no longer a candidate) until ``--game-id`` forces a retry;
* ``failed``: the documents could not be fetched (not final yet, network);
  nothing was stored and the game stays a candidate, queued behind games
  not yet tried, without using up its rejection budget.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

import requests

from app.domain.nba_events import (
    REGULAR_SEASON_TYPE,
    is_completed_non_postponed_event,
    player_game_log_season_type,
)
from app.models.rotation import MAX_REJECTIONS
from app.errors import sanitize_public_value
from app.services.rotation_derivation import RotationRejected, derive_rotation
from app.utils.telemetry import ProviderResponseError

logger = logging.getLogger(__name__)

_REGULAR_SEASON_GAME_ID = re.compile(r"002\d{7}\Z")


class RotationCollectionService:
    """Fetch, derive, validate and store the rotations of missing games."""

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
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep

    def candidates(self, season: str) -> tuple[list[str], list[dict[str, Any]]]:
        """Games to try, and the games given up on.

        Candidates are completed regular-season games with no stored rotation
        and fewer than :data:`MAX_REJECTIONS` rejections.  Never-tried games
        come first, oldest first; games tried before follow, least recently
        tried first, so a game that keeps failing or being rejected can never
        keep a bounded run from newer games.
        """

        stored = self.repository.stored_game_ids(season)
        attempts = self.repository.attempts(season)
        eligible = [
            str(event["nba_game_id"])
            for event in sorted(
                self.events(season), key=lambda event: str(event["scheduled_at"])
            )
            if is_completed_non_postponed_event(event)
            and player_game_log_season_type(event) == REGULAR_SEASON_TYPE
            and str(event["nba_game_id"]) not in stored
        ]
        given_up = [
            {
                "game_id": game_id,
                "rejections": attempts[game_id]["rejections"],
                "reason": attempts[game_id]["last_reason"],
            }
            for game_id in eligible
            if game_id in attempts and attempts[game_id]["rejections"] >= MAX_REJECTIONS
        ]
        dropped = {item["game_id"] for item in given_up}
        pending = [game_id for game_id in eligible if game_id not in dropped]
        # sorted() is stable, so never-tried games keep their oldest-first order.
        pending.sort(
            key=lambda game_id: (
                str(attempts[game_id]["last_attempt_at"]) if game_id in attempts else ""
            )
        )
        return pending, given_up

    def collect(
        self,
        season: str,
        *,
        max_games: int | None = None,
        throttle_seconds: float = 0.0,
        game_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Collect up to ``max_games`` games; ``game_ids`` names them explicitly.

        Explicit ids are collected even when already stored (they are
        replaced) or given up on (a forced retry), but only regular-season
        ids (``002…``) are accepted.
        """

        given_up: list[dict[str, Any]] = []
        if game_ids is None:
            pending, given_up = self.candidates(season)
        else:
            pending = list(dict.fromkeys(str(game_id) for game_id in game_ids))
            invalid = [g for g in pending if _REGULAR_SEASON_GAME_ID.match(g) is None]
            if invalid:
                raise ValueError(f"not regular-season game ids: {', '.join(invalid)}")
        batch = pending if max_games is None else pending[:max_games]
        stored: list[str] = []
        rejected: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        for index, game_id in enumerate(batch):
            if index and throttle_seconds:
                self._sleep(throttle_seconds)
            try:
                box = self.provider.fetch_game_stats(game_id, season)
                play_by_play = self.provider.fetch_play_by_play(game_id)
            except (requests.RequestException, ProviderResponseError, ValueError) as error:
                logger.warning("rotation fetch failed for %s: %s", game_id, error)
                reason = sanitize_public_value(str(error))
                failed.append({"game_id": game_id, "error": reason})
                self.repository.record_attempt(season, game_id, "failed", reason, self._clock())
                continue
            try:
                rotation = derive_rotation(box, play_by_play)
            except (RotationRejected, KeyError, TypeError, ValueError) as error:
                logger.warning("rotation rejected for %s: %s", game_id, error)
                rejected.append({"game_id": game_id, "reason": str(error)})
                self.repository.record_attempt(
                    season, game_id, "rejected", str(error), self._clock()
                )
                continue
            self.repository.replace_game(season, rotation, self._clock())
            stored.append(game_id)
        return {
            "season": season,
            "candidates": len(pending),
            "attempted": len(batch),
            "stored": len(stored),
            "stored_game_ids": stored,
            "rejected": rejected,
            "failed": failed,
            "given_up": given_up,
            "remaining": len(pending) - len(batch),
        }


__all__ = ["RotationCollectionService"]
