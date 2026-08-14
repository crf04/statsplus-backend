"""Persistence and freshness state for the canonical event catalog."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine

from app.domain.freshness import exact_seconds
from app.domain.nba_events import is_postponed_event
from app.domain.utc import assume_utc
from app.models.event_catalog import EventCatalogEntry, EventCatalogRefresh

DEFAULT_FAILURE_SUMMARY = "The event catalog refresh could not complete."


def _iso(value: datetime | None) -> str | None:
    return assume_utc(value).isoformat() if value is not None else None


class EventCatalogRepository:
    """Own transactional event upserts and persisted refresh state."""

    def __init__(self, engine: Engine, *, write_fence: Any | None = None) -> None:
        self.engine = engine
        self._write_fence = write_fence

    def publish(self, season: str, frame: pd.DataFrame, refreshed_at: datetime) -> int:
        table = EventCatalogEntry.__table__
        refresh_table = EventCatalogRefresh.__table__
        provider_columns = {
            "season", "home_team_id", "home_team_name", "home_team_tricode",
            "away_team_id", "away_team_name", "away_team_tricode", "scheduled_at",
            "status_text", "status_code", "postponed_status", "postponement_evidence",
            "classification",
        }
        with self.engine.begin() as connection:
            checker = getattr(self._write_fence, "assert_writable", None)
            if callable(checker):
                checker("event_catalog", connection=connection)
            for record in frame.to_dict(orient="records"):
                game_id = str(record["nba_game_id"])
                values = {column: record[column] for column in provider_columns}
                if isinstance(values["postponement_evidence"], (dict, list)):
                    values["postponement_evidence"] = json.dumps(
                        values["postponement_evidence"], sort_keys=True
                    )
                values["last_seen_at"] = refreshed_at
                exists = connection.execute(
                    select(table.c.nba_game_id).where(table.c.nba_game_id == game_id)
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(insert(table).values(
                        nba_game_id=game_id, first_seen_at=refreshed_at, **values
                    ))
                else:
                    connection.execute(
                        update(table).where(table.c.nba_game_id == game_id).values(**values)
                    )
            event_count = connection.execute(
                select(func.count()).select_from(table).where(table.c.season == season)
            ).scalar_one()
            exists = connection.execute(
                select(refresh_table.c.season).where(refresh_table.c.season == season)
            ).scalar_one_or_none()
            values = {"last_attempt_at": refreshed_at, "last_success_at": refreshed_at,
                      "event_count": int(event_count)}
            if exists is None:
                connection.execute(insert(refresh_table).values(season=season, **values))
            else:
                connection.execute(update(refresh_table).where(
                    refresh_table.c.season == season).values(**values))
        return int(event_count)

    def record_failure(self, season: str, failed_at: datetime) -> None:
        table = EventCatalogRefresh.__table__
        try:
            with self.engine.begin() as connection:
                exists = connection.execute(select(table.c.season).where(
                    table.c.season == season)).scalar_one_or_none()
                values = {"last_attempt_at": failed_at, "last_failure_at": failed_at,
                          "failure_summary": DEFAULT_FAILURE_SUMMARY}
                if exists is None:
                    connection.execute(insert(table).values(season=season, event_count=0, **values))
                else:
                    connection.execute(update(table).where(
                        table.c.season == season).values(**values))
        except Exception:
            # Never obscure the original provider or persistence exception.
            return

    def list_events(self, season: str) -> list[dict[str, Any]]:
        table = EventCatalogEntry.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(select(table).where(
                table.c.season == season).order_by(table.c.scheduled_at, table.c.nba_game_id)).mappings()
            return [self._serialize(row) for row in rows]

    def count_events(self, season: str) -> int:
        """Count actual persisted events for one season."""

        table = EventCatalogEntry.__table__
        with self.engine.connect() as connection:
            return int(
                connection.execute(
                    select(func.count()).select_from(table).where(table.c.season == season)
                ).scalar_one()
            )

    def list_events_between(
        self, season: str, starts_at: datetime, ends_at: datetime
    ) -> list[dict[str, Any]]:
        """Read events in one half-open UTC window without decoding a season."""

        table = EventCatalogEntry.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(table)
                .where(
                    table.c.season == season,
                    table.c.scheduled_at >= assume_utc(starts_at),
                    table.c.scheduled_at < assume_utc(ends_at),
                )
                .order_by(table.c.scheduled_at, table.c.nba_game_id)
            ).mappings()
            return [self._serialize(row) for row in rows]

    def freshness(self, season: str, observed_at: datetime, max_age: timedelta) -> dict[str, Any]:
        table = EventCatalogRefresh.__table__
        with self.engine.connect() as connection:
            row = connection.execute(select(table).where(
                table.c.season == season)).mappings().one_or_none()
        success = row["last_success_at"] if row else None
        # The TTL is reported as the exact duration it was gated on, counted
        # from the timedelta's own whole microseconds.  Rewriting it as float
        # hours made a third of an hour gate at 1200 seconds and report
        # 1199.99999999999988, so an age and its own ceiling disagreed at the
        # boundary they were compared at.
        return {"season": season, "fresh": bool(success and assume_utc(observed_at) - assume_utc(success) <= max_age),
                "max_age_seconds": exact_seconds(max_age),
                "last_attempt_at": _iso(row["last_attempt_at"]) if row else None,
                "last_success_at": _iso(success), "last_failure_at": _iso(row["last_failure_at"]) if row else None,
                "failure_summary": row["failure_summary"] if row else None,
                "event_count": int(row["event_count"] or 0) if row else 0}

    @staticmethod
    def _serialize(row: Mapping[str, Any]) -> dict[str, Any]:
        evidence = row["postponement_evidence"]
        if evidence:
            try:
                evidence = json.loads(evidence)
            except (TypeError, ValueError):
                pass
        event = {"nba_game_id": row["nba_game_id"], "season": row["season"],
                "scheduled_at": _iso(row["scheduled_at"]), "status_text": row["status_text"],
                "status_code": row["status_code"], "postponed_status": row["postponed_status"],
                "postponement_evidence": evidence,
                "classification": row["classification"],
                "home_team_id": row["home_team_id"], "home_team_name": row["home_team_name"],
                "home_team_tricode": row["home_team_tricode"], "away_team_id": row["away_team_id"],
                "away_team_name": row["away_team_name"], "away_team_tricode": row["away_team_tricode"],
                "home_team": {"id": row["home_team_id"], "name": row["home_team_name"], "tricode": row["home_team_tricode"]},
                "away_team": {"id": row["away_team_id"], "name": row["away_team_name"], "tricode": row["away_team_tricode"]},
                "first_seen_at": _iso(row["first_seen_at"]), "last_seen_at": _iso(row["last_seen_at"])}
        event["is_postponed"] = is_postponed_event(event)
        return event
