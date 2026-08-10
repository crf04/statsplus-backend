"""Current-season slate reads from the canonical event catalog."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
import re
from typing import Any
from zoneinfo import ZoneInfo

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.domain.freshness import (
    exact_age_seconds,
    exact_seconds,
    exact_timedelta,
    time_window_timedelta,
    within_max_age,
)
from app.domain.nba_events import NBAGameStatus, event_classification, event_kind
from app.errors import InvalidInputError, ProviderUnavailableError


EASTERN = ZoneInfo("America/New_York")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class SlateService:
    """Shape one ET Slate Date from persisted schedule facts."""

    def __init__(
        self,
        event_catalog: Any | None,
        *,
        settings: RuntimeSettings | None = None,
        clock: Callable[[], datetime] | None = None,
        schedule_max_age: timedelta | None = None,
    ) -> None:
        self.event_catalog = event_catalog
        self.settings = settings or get_runtime_settings()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        field = "SLATE_SCHEDULE_MAX_AGE_HOURS"
        if schedule_max_age is None:
            self.schedule_max_age = time_window_timedelta(
                self.settings.catalog.slate_schedule_max_age_hours,
                unit_seconds=3600,
                field=field,
            )
        else:
            self.schedule_max_age = exact_timedelta(
                exact_seconds(schedule_max_age), field=field
            )
        self.schedule_max_age_seconds = exact_seconds(self.schedule_max_age)

    def get_slate(self, requested_date: str | None = None) -> dict[str, Any]:
        slate_date = self._parse_slate_date(requested_date)
        season = self.settings.nba.current_season
        if self.event_catalog is None:
            raise ProviderUnavailableError(
                "The NBA schedule is not available. Please try again later."
            )

        observed_at = _utc(self._clock())
        freshness = self.event_catalog.get_freshness(season, now=observed_at)
        retrieved_at = freshness.get("last_success_at")
        if not retrieved_at:
            raise ProviderUnavailableError(
                "The NBA schedule is not available. Please try again later."
            )

        games = []
        for event in self.event_catalog.get_events(season):
            if not self._belongs_to_slate(event, slate_date):
                continue
            game_id = str(event.get("nba_game_id", ""))
            classification = event_classification(
                game_id, str(event.get("classification") or "")
            )
            kind = event_kind(game_id, classification)
            if self._is_all_star(kind):
                continue
            games.append(
                self._game(event, classification=classification, event_kind=kind)
            )
        games.sort(key=lambda game: (game["scheduled_at"], game["game_id"]))

        return {
            "slate_date": slate_date.isoformat(),
            "pool_status": "unavailable",
            "freshness": {
                "schedule": {
                    "status": self._schedule_freshness(
                        retrieved_at, observed_at=observed_at
                    ),
                    "retrieved_at": retrieved_at,
                },
                "pool": {
                    "status": "unavailable",
                    "retrieved_at": None,
                    "providers": {},
                },
            },
            "games": games,
        }

    def _parse_slate_date(self, value: str | None) -> date:
        if value is None:
            parsed = _utc(self._clock()).astimezone(EASTERN).date()
        else:
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d").date()
            except (TypeError, ValueError) as error:
                raise InvalidInputError(
                    "The slate date must use YYYY-MM-DD.", detail=error
                ) from error
            if parsed.isoformat() != value:
                raise InvalidInputError("The slate date must use YYYY-MM-DD.")

        return parsed

    @staticmethod
    def _scheduled_at(event: Mapping[str, Any]) -> datetime:
        value = str(event["scheduled_at"])
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _utc(parsed)

    @classmethod
    def _belongs_to_slate(cls, event: Mapping[str, Any], slate_date: date) -> bool:
        return cls._scheduled_at(event).astimezone(EASTERN).date() == slate_date

    @staticmethod
    def _is_all_star(classification: str) -> bool:
        normalized = " ".join(
            re.sub(r"[^a-z0-9]+", " ", classification.casefold()).split()
        )
        return "all star" in normalized

    def _schedule_freshness(
        self, retrieved_at: str, *, observed_at: datetime
    ) -> str:
        retrieved = _utc(datetime.fromisoformat(retrieved_at.replace("Z", "+00:00")))
        age = exact_age_seconds(
            exact_seconds(observed_at - retrieved), field="schedule age"
        )
        return "fresh" if within_max_age(age, self.schedule_max_age_seconds) else "stale"

    @classmethod
    def _game(
        cls,
        event: Mapping[str, Any],
        *,
        classification: str,
        event_kind: str,
    ) -> dict[str, Any]:
        game_id = str(event["nba_game_id"])
        preseason = event_kind.casefold() == "preseason"
        unusual_classification = (
            None
            if classification.casefold() in {"regular season", "unknown"}
            else classification
        )
        if event.get("is_postponed") or event.get("postponed_status"):
            state = "postponed"
        elif (
            event.get("status_code") == NBAGameStatus.FINAL
            or str(event.get("status_text", "")).casefold().startswith("final")
        ):
            state = "final"
        else:
            state = "scheduled"

        return {
            "game_id": game_id,
            "away_team": cls._team(event["away_team"]),
            "home_team": cls._team(event["home_team"]),
            "scheduled_at": cls._scheduled_at(event).isoformat(),
            "status": {"state": state, "label": str(event["status_text"])},
            "classification": unusual_classification,
            "preseason": preseason,
        }

    @staticmethod
    def _team(team: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "team_id": int(team["id"]),
            "tricode": str(team["tricode"]),
            "name": str(team["name"]),
            "targetable_player_count": 0,
        }


__all__ = ["SlateService"]
