"""Refresh and read the application-owned canonical NBA event catalog.

The service has one deliberately narrow write path: retrieve and validate a
whole explicit season, then upsert provider-owned facts by NBA game ID in one
transaction.  It never replaces the table and never guesses that one game ID
is a replacement for another.  Per-season refresh state is stored separately
so a failed attempt cannot erase a prior successful catalog or conflate event
freshness with another catalog's health.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.errors import ProviderUnavailableError
from app.models.event_catalog import EventCatalogEntry, EventCatalogRefresh
from app.providers.nba_stats import NBAStatsProvider
from app.services.nba_stats_adapter import (
    normalize_whole_season_schedule,
    validate_canonical_season,
)
from app.utils.db import is_demo_database_url


DEFAULT_EVENT_CATALOG_MAX_AGE = timedelta(hours=72)
DEFAULT_FAILURE_SUMMARY = "The event catalog refresh could not complete."


@dataclass(frozen=True, slots=True)
class EventCatalogRefreshResult:
    """Stable result returned after one successful catalog publication."""

    season: str
    event_count: int
    refreshed_at: str

    @property
    def row_count(self) -> int:
        """Compatibility spelling for callers that call events rows."""

        return self.event_count


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Normalize an injected or database timestamp to aware UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


class EventCatalogService:
    """Own canonical event refreshes and persisted freshness reads."""

    def __init__(
        self,
        engine: Engine,
        provider: NBAStatsProvider | None = None,
        settings: RuntimeSettings | None = None,
        *,
        nba_stats_provider: NBAStatsProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        max_age: timedelta | float | None = None,
        max_age_hours: float | None = None,
    ) -> None:
        self.engine = engine
        self.provider = provider if provider is not None else nba_stats_provider
        if self.provider is None:
            raise ValueError("an NBA whole-season schedule provider is required")
        self.settings = settings or get_runtime_settings()
        self._clock = clock or _utc_now

        if is_demo_database_url(str(engine.url)):
            raise ValueError(
                "The canonical event catalog requires a writable application database."
            )

        configured_hours = getattr(
            getattr(self.settings, "catalog", None), "event_max_age_hours", 72.0
        )
        if max_age_hours is not None:
            self.max_age = timedelta(hours=float(max_age_hours))
        elif isinstance(max_age, timedelta):
            self.max_age = max_age
        elif max_age is not None:
            self.max_age = timedelta(hours=float(max_age))
        else:
            self.max_age = timedelta(hours=float(configured_hours))
        if self.max_age.total_seconds() <= 0:
            raise ValueError("event catalog max age must be greater than zero")

    def refresh(
        self,
        season: str,
        *,
        now: datetime | None = None,
    ) -> EventCatalogRefreshResult:
        """Fetch, validate, and atomically publish one explicit season.

        Provider and normalization failures are recorded in the independent
        status table and re-raised as ``ProviderUnavailableError``.  Existing
        event rows and the previous ``last_success_at`` remain untouched.
        """

        canonical_season = validate_canonical_season(season)
        refreshed_at = _as_utc(now or self._clock())
        try:
            raw_frame = self._fetch_schedule(canonical_season)
            frame = normalize_whole_season_schedule(
                self._as_frame(raw_frame), season=canonical_season
            )
            event_count = self._publish(
                canonical_season,
                frame,
                refreshed_at,
            )
            return EventCatalogRefreshResult(
                season=canonical_season,
                event_count=event_count,
                refreshed_at=refreshed_at.isoformat(),
            )
        except ProviderUnavailableError:
            self._record_failure(canonical_season, refreshed_at)
            raise
        except Exception as error:
            self._record_failure(canonical_season, refreshed_at)
            raise ProviderUnavailableError(
                "The event catalog provider is unavailable.", detail=error
            ) from error

    def get_events(self, season: str) -> list[dict[str, Any]]:
        """Read canonical events for one explicit season in schedule order."""

        canonical_season = validate_canonical_season(season)
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(EventCatalogEntry.__table__)
                .where(EventCatalogEntry.__table__.c.season == canonical_season)
                .order_by(
                    EventCatalogEntry.__table__.c.scheduled_at,
                    EventCatalogEntry.__table__.c.nba_game_id,
                )
            ).mappings()
            return [self._serialize_event(row) for row in rows]

    # Persisted-read aliases make the freshness seam explicit without adding
    # a route or coupling this work to a future event-mapping API.
    read_events = get_events
    list_events = get_events
    get_catalog = get_events
    read_catalog = get_events

    def get_freshness(
        self,
        season: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return independent attempt/success/failure and max-age state."""

        canonical_season = validate_canonical_season(season)
        observed_at = _as_utc(now or self._clock())
        with self.engine.connect() as connection:
            row = connection.execute(
                select(EventCatalogRefresh.__table__).where(
                    EventCatalogRefresh.__table__.c.season == canonical_season
                )
            ).mappings().one_or_none()

        last_success = row["last_success_at"] if row is not None else None
        fresh = bool(
            last_success is not None
            and observed_at - _as_utc(last_success) <= self.max_age
        )
        return {
            "season": canonical_season,
            "fresh": fresh,
            "max_age_hours": self.max_age.total_seconds() / 3600,
            "last_attempt_at": _iso(row["last_attempt_at"]) if row else None,
            "last_success_at": _iso(last_success),
            "last_refresh_at": _iso(last_success),
            "last_successful_refresh": _iso(last_success),
            "last_failure_at": _iso(row["last_failure_at"]) if row else None,
            "failure_summary": row["failure_summary"] if row else None,
            "last_failure_summary": row["failure_summary"] if row else None,
            "last_error": row["failure_summary"] if row else None,
            "event_count": int(row["event_count"] or 0) if row else 0,
        }

    read_freshness = get_freshness
    get_catalog_freshness = get_freshness
    get_status = get_freshness
    read_status = get_freshness
    freshness = get_freshness
    status = get_freshness

    refresh_catalog = refresh
    refresh_events = refresh

    def is_fresh(self, season: str, *, now: datetime | None = None) -> bool:
        """Return whether the last successful catalog is within max age."""

        return bool(self.get_freshness(season, now=now)["fresh"])

    def _fetch_schedule(self, season: str) -> Any:
        """Call the canonical whole-season provider method."""

        for name in (
            "fetch_whole_season_schedule",
            "get_whole_season_schedule",
            "fetch_schedule",
            "get_schedule",
            "fetch_season_schedule",
            "get_season_schedule",
        ):
            method = getattr(self.provider, name, None)
            if callable(method):
                return method(season=season)
        raise ProviderUnavailableError(
            "The whole-season NBA schedule provider is unavailable."
        )

    @staticmethod
    def _as_frame(value: Any) -> pd.DataFrame:
        if isinstance(value, pd.DataFrame):
            return value
        if isinstance(value, Mapping):
            return pd.DataFrame([value])
        if isinstance(value, (list, tuple)):
            return pd.DataFrame(value)
        raise ProviderUnavailableError("The NBA schedule provider returned invalid data.")

    def _publish(
        self,
        season: str,
        frame: pd.DataFrame,
        refreshed_at: datetime,
    ) -> int:
        """Upsert provider facts and success state in one transaction."""

        table = EventCatalogEntry.__table__
        refresh_table = EventCatalogRefresh.__table__
        provider_columns = {
            "season",
            "home_team_id",
            "home_team_name",
            "home_team_tricode",
            "away_team_id",
            "away_team_name",
            "away_team_tricode",
            "scheduled_at",
            "status_text",
            "status_code",
            "postponed_status",
            "postponement_evidence",
            "classification",
        }
        with self.engine.begin() as connection:
            for record in frame.to_dict(orient="records"):
                game_id = str(record["nba_game_id"])
                values = {
                    column: record[column]
                    for column in provider_columns
                }
                values["last_seen_at"] = refreshed_at
                existing = connection.execute(
                    select(
                        table.c.nba_game_id,
                        table.c.mapping_needed,
                        table.c.audit_status,
                        table.c.classification,
                    ).where(
                        table.c.nba_game_id == game_id
                    )
                ).mappings().one_or_none()
                if existing is None:
                    connection.execute(
                        insert(table).values(
                            nba_game_id=game_id,
                            first_seen_at=refreshed_at,
                            **values,
                        )
                    )
                else:
                    # Once an operator has marked an event for mapping or
                    # review, keep that audit classification until an
                    # explicit mapping workflow changes it.  Provider-owned
                    # schedule/status/team facts still update in place.
                    if (
                        existing["mapping_needed"]
                        or existing["audit_status"] in {
                            "needs_mapping",
                            "mapping_needed",
                            "needs_review",
                        }
                        or str(existing["classification"]).lower()
                        in {"mapping_needed", "needs_mapping", "needs_review"}
                    ):
                        values.pop("classification", None)
                    connection.execute(
                        update(table)
                        .where(table.c.nba_game_id == game_id)
                        .values(**values)
                    )

            event_count = connection.execute(
                select(func.count())
                .select_from(table)
                .where(table.c.season == season)
            ).scalar_one()
            refresh_exists = connection.execute(
                select(refresh_table.c.season).where(
                    refresh_table.c.season == season
                )
            ).scalar_one_or_none()
            refresh_values = {
                "last_attempt_at": refreshed_at,
                "last_success_at": refreshed_at,
                "event_count": int(event_count),
            }
            if refresh_exists is None:
                connection.execute(
                    insert(refresh_table).values(season=season, **refresh_values)
                )
            else:
                connection.execute(
                    update(refresh_table)
                    .where(refresh_table.c.season == season)
                    .values(**refresh_values)
                )
        return int(event_count)

    def _record_failure(self, season: str, failed_at: datetime) -> None:
        """Persist failure state without touching successful event rows."""

        table = EventCatalogRefresh.__table__
        try:
            with self.engine.begin() as connection:
                exists = connection.execute(
                    select(table.c.season).where(table.c.season == season)
                ).scalar_one_or_none()
                values = {
                    "last_attempt_at": failed_at,
                    "last_failure_at": failed_at,
                    "failure_summary": DEFAULT_FAILURE_SUMMARY,
                }
                if exists is None:
                    connection.execute(
                        insert(table).values(season=season, event_count=0, **values)
                    )
                else:
                    connection.execute(
                        update(table).where(table.c.season == season).values(**values)
                    )
        except Exception:
            # The original provider failure is the useful operator signal;
            # do not mask it if the status table itself is unavailable.
            return

    @staticmethod
    def _serialize_event(row: Mapping[str, Any]) -> dict[str, Any]:
        evidence: Any = row["postponement_evidence"]
        if evidence:
            try:
                evidence = json.loads(evidence)
            except (TypeError, ValueError):
                pass
        return {
            "nba_game_id": row["nba_game_id"],
            "game_id": row["nba_game_id"],
            "season": row["season"],
            "scheduled_at": _iso(row["scheduled_at"]),
            "scheduled_time_utc": _iso(row["scheduled_at"]),
            "status_text": row["status_text"],
            "game_status_text": row["status_text"],
            "status_code": row["status_code"],
            "game_status": row["status_code"],
            "postponed_status": row["postponed_status"],
            "postponement_status": row["postponed_status"],
            "postponement_evidence": evidence,
            "is_postponed": bool(row["postponed_status"] or evidence),
            "classification": row["classification"],
            "event_classification": row["classification"],
            "event_type": row["classification"],
            "home_team_id": row["home_team_id"],
            "home_team_name": row["home_team_name"],
            "home_team_tricode": row["home_team_tricode"],
            "away_team_id": row["away_team_id"],
            "away_team_name": row["away_team_name"],
            "away_team_tricode": row["away_team_tricode"],
            "home_team": {
                "id": row["home_team_id"],
                "name": row["home_team_name"],
                "tricode": row["home_team_tricode"],
            },
            "away_team": {
                "id": row["away_team_id"],
                "name": row["away_team_name"],
                "tricode": row["away_team_tricode"],
            },
            "mapping_needed": bool(row["mapping_needed"]),
            "audit_status": row["audit_status"],
            "audit_note": row["audit_note"],
            "first_seen_at": _iso(row["first_seen_at"]),
            "last_seen_at": _iso(row["last_seen_at"]),
        }


__all__ = [
    "DEFAULT_EVENT_CATALOG_MAX_AGE",
    "EventCatalogRefreshResult",
    "EventCatalogService",
]
