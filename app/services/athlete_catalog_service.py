"""Refresh and read the application-owned canonical athlete catalog.

The service is intentionally pull-based: an operator or deployment command
chooses one or more explicit seasons, the provider result is normalized in
memory, and each season is published in one database transaction.  A failed
provider call or publication records failure metadata in a separate
transaction while leaving the last successful rows untouched.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import pandas as pd
from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.domain.freshness import (
    exact_scaled_seconds,
    exact_seconds,
    time_window_timedelta,
)
from app.domain.market_content import NumericDomainError
from app.errors import InvalidConfigurationError
from app.models.athlete_catalog import AthleteCatalog, AthleteCatalogFreshness
from app.providers.nba_stats import ROSTER_COLUMNS, validate_canonical_season
from app.providers.nba_stats import NBAStatsAdapter
from app.utils.telemetry import ProviderResponseError
from app.utils.db import is_demo_database_url

logger = logging.getLogger(__name__)

CATALOG_TABLE_NAME = AthleteCatalog.__tablename__
FRESHNESS_TABLE_NAME = AthleteCatalogFreshness.__tablename__
DEFAULT_FRESHNESS_DAYS = 7
CATALOG_FAILURE_SUMMARY = "The athlete catalog refresh could not complete."


@dataclass(frozen=True, slots=True)
class AthleteCatalogSeasonResult:
    season: str
    status: str
    row_count: int
    published_at: str | None = None
    failure_summary: str | None = None


@dataclass(frozen=True, slots=True)
class AthleteCatalogBatchResult(Mapping[str, AthleteCatalogSeasonResult]):
    results: tuple[AthleteCatalogSeasonResult, ...]

    def __getitem__(self, season: str) -> AthleteCatalogSeasonResult:
        for result in self.results:
            if result.season == season:
                return result
        raise KeyError(season)

    def __iter__(self) -> Iterator[str]:
        return (result.season for result in self.results)

    def __len__(self) -> int:
        return len(self.results)

    @property
    def has_failures(self) -> bool:
        return any(result.status == "failed" for result in self.results)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized is not None else None


class AthleteCatalogService:
    """Own canonical athlete catalog publication and freshness reads."""

    def __init__(
        self,
        db_engine: Engine,
        settings: RuntimeSettings | None = None,
        *,
        nba_stats_provider: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        freshness_days: int | None = None,
        write_fence: Any | None = None,
    ) -> None:
        self.engine = db_engine
        self.settings = settings or get_runtime_settings()
        engine_url = str(getattr(db_engine, "url", ""))
        if is_demo_database_url(engine_url):
            raise InvalidConfigurationError(
                "The bundled demo database is read-only and cannot store an athlete catalog."
            )
        self.nba_stats = nba_stats_provider or NBAStatsAdapter(settings=self.settings)
        self._clock = clock or _utc_now
        self._write_fence = write_fence
        configured_days = (
            freshness_days
            if freshness_days is not None
            else self.settings.catalog.athlete_freshness_days
        )
        # A direct override enters the same time-window authority the
        # configuration boundary uses, in the same unit, so a service built by
        # hand can never hold a TTL an operator could not configure.
        try:
            self._max_age = time_window_timedelta(
                configured_days,
                unit_seconds=86400,
                field="ATHLETE_CATALOG_FRESHNESS_DAYS",
            )
            days = exact_scaled_seconds(
                configured_days,
                unit_seconds=1,
                field="ATHLETE_CATALOG_FRESHNESS_DAYS",
            )
            if days != days.to_integral_value():
                raise NumericDomainError(
                    "ATHLETE_CATALOG_FRESHNESS_DAYS must be a whole number of days"
                )
        except ValueError as error:
            raise InvalidConfigurationError(str(error)) from error
        self.freshness_days = int(days)

    def refresh(
        self,
        seasons: Iterable[str] | None = None,
    ) -> AthleteCatalogBatchResult:
        """Refresh every explicitly requested season, atomically per season."""

        if seasons is None:
            raise ValueError("one or more explicit canonical seasons are required")
        self._assert_legacy_write_allowed()
        canonical_seasons = self._validate_seasons(seasons)
        results: list[AthleteCatalogSeasonResult] = []
        for season in canonical_seasons:
            try:
                provider_frame = self.nba_stats.get_player_roster(season=season)
                frame = provider_frame
                if frame.empty:
                    raise ProviderResponseError(
                        "NBA Stats returned an empty roster for the requested season."
                    )
                published_at = _as_utc(self._clock()) or _utc_now()
                self._publish_season(season, frame, published_at)
                results.append(AthleteCatalogSeasonResult(
                    season, "succeeded", len(frame.index), published_at.isoformat()
                ))
            except Exception:
                self._record_failure(season)
                results.append(AthleteCatalogSeasonResult(
                    season, "failed", 0, failure_summary=CATALOG_FAILURE_SUMMARY
                ))
        return AthleteCatalogBatchResult(tuple(results))

    def refresh_season(self, season: str) -> AthleteCatalogSeasonResult:
        """Refresh one explicit season and return its publication state."""

        return self.refresh([season])[validate_canonical_season(season)]

    def _assert_legacy_write_allowed(self) -> None:
        checker = getattr(self._write_fence, "assert_writable", None)
        if callable(checker):
            checker("athlete_catalog")

    def get_catalog(
        self,
        season: str,
        *,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Read persisted canonical athletes for one explicit season."""

        season = validate_canonical_season(season)
        with self.engine.connect() as connection:
            statement = (
                select(AthleteCatalog.__table__)
                .where(AthleteCatalog.season == season)
                .order_by(AthleteCatalog.player_id)
            )
            if active_only:
                statement = statement.where(AthleteCatalog.is_active_for_season.is_(True))
            rows = connection.execute(statement).mappings().all()
        return [
            {
                column: row[column]
                for column in ROSTER_COLUMNS
            }
            for row in rows
        ]

    def get_freshness(
        self,
        season: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Read persisted refresh state and evaluate the configured TTL."""

        season = validate_canonical_season(season)
        observed_at = _as_utc(now or self._clock()) or _utc_now()
        with self.engine.connect() as connection:
            row = connection.execute(
                select(AthleteCatalogFreshness.__table__).where(
                    AthleteCatalogFreshness.season == season
                )
            ).mappings().first()
        last_success = _as_utc(row["last_success_at"]) if row else None
        # One duration decides freshness and is reported: the TTL is stated as
        # the exact seconds of the very timedelta it was compared against, so a
        # reader can never see an age past a ceiling this catalog called fresh.
        max_age = self._max_age
        is_fresh = bool(last_success is not None and observed_at <= last_success + max_age)
        return {
            "season": season,
            "is_fresh": is_fresh,
            "freshness_days": self.freshness_days,
            "max_age_seconds": exact_seconds(max_age),
            "last_success_at": _iso(last_success),
            "last_failure_at": _iso(_as_utc(row["last_failure_at"]) if row else None),
            "last_failure_summary": row["last_failure_summary"] if row else None,
            "row_count": (row["last_success_row_count"] if row else None),
        }

    def is_fresh(self, season: str, *, now: datetime | None = None) -> bool:
        """Return whether the last successful publication is within its TTL."""

        return bool(self.get_freshness(season, now=now)["is_fresh"])

    @staticmethod
    def _validate_seasons(seasons: Iterable[str]) -> tuple[str, ...]:
        if isinstance(seasons, str):
            raise ValueError("one or more explicit canonical seasons are required")
        try:
            requested = tuple(seasons)
        except TypeError as error:
            raise ValueError(
                "one or more explicit canonical seasons are required"
            ) from error
        if not requested:
            raise ValueError("one or more explicit canonical seasons are required")
        canonical = tuple(validate_canonical_season(season) for season in requested)
        if len(set(canonical)) != len(canonical):
            raise ValueError("seasons must not be repeated")
        return canonical

    def _publish_season(
        self,
        season: str,
        frame: pd.DataFrame,
        published_at: datetime,
    ) -> None:
        rows = []
        for record in frame.to_dict(orient="records"):
            rows.append(
                {
                    **{column: record.get(column) for column in ROSTER_COLUMNS},
                    "published_at": published_at,
                }
            )
        catalog_table = AthleteCatalog.__table__
        freshness_table = AthleteCatalogFreshness.__table__
        with self.engine.begin() as connection:
            # Delete and insert occur in this same transaction.  Any failure
            # rolls back the delete, preserving the prior successful season.
            connection.execute(
                delete(catalog_table).where(catalog_table.c.season == season)
            )
            if rows:
                connection.execute(insert(catalog_table), rows)
            self._upsert_success(
                connection,
                freshness_table,
                season=season,
                published_at=published_at,
                row_count=len(rows),
            )

    def _upsert_success(
        self,
        connection,
        freshness_table,
        *,
        season: str,
        published_at: datetime,
        row_count: int,
    ) -> None:
        values = {
            "last_success_at": published_at,
            "last_success_row_count": row_count,
            "updated_at": published_at,
        }
        result = connection.execute(
            update(freshness_table)
            .where(freshness_table.c.season == season)
            .values(**values)
        )
        if result.rowcount == 0:
            connection.execute(
                insert(freshness_table).values(season=season, **values)
            )

    def _record_failure(self, season: str) -> None:
        """Record failure independently from the publication transaction."""

        failed_at = _as_utc(self._clock()) or _utc_now()
        try:
            freshness_table = AthleteCatalogFreshness.__table__
            values = {
                "last_failure_at": failed_at,
                "last_failure_summary": CATALOG_FAILURE_SUMMARY,
                "updated_at": failed_at,
            }
            with self.engine.begin() as connection:
                result = connection.execute(
                    update(freshness_table)
                    .where(freshness_table.c.season == season)
                    .values(**values)
                )
                if result.rowcount == 0:
                    connection.execute(
                        insert(freshness_table).values(season=season, **values)
                    )
        except Exception:
            logger.exception("Could not record athlete catalog failure for %s", season)
        logger.warning("Athlete catalog refresh failed for season %s", season)


__all__ = [
    "AthleteCatalogService",
    "AthleteCatalogBatchResult",
    "AthleteCatalogSeasonResult",
    "CATALOG_FAILURE_SUMMARY",
    "CATALOG_TABLE_NAME",
    "DEFAULT_FRESHNESS_DAYS",
    "FRESHNESS_TABLE_NAME",
]
