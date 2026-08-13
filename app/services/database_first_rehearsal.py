"""Offline Historical Rehearsal Window for database-first activation."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.models.collection_control import PublicationPointer, PublicationVersion


UTC = timezone.utc
REHEARSAL_DATE_COUNT = 7
# The last seven Eastern dates of the completed 2025-26 Regular Season.  The
# runner accepts an explicit date list so future seasons never inherit these
# values accidentally.
DEFAULT_REHEARSAL_DATES = tuple(
    date(2026, 4, day) for day in range(6, 13)
)


@dataclass(frozen=True, slots=True)
class RehearsalRecord:
    sequence: int
    cutoff: str
    status: str
    streams: tuple[str, ...]
    details: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HistoricalRehearsalReport:
    season: str
    environment: str
    status: str
    records: tuple[RehearsalRecord, ...]
    synergy_season_status: str
    production_pointers_unchanged: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "season": self.season,
            "environment": self.environment,
            "status": self.status,
            "records": [asdict(record) for record in self.records],
            "synergy_season_status": self.synergy_season_status,
            "production_pointers_unchanged": self.production_pointers_unchanged,
            "error": self.error,
        }

    def write(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )


class HistoricalRehearsalRunner:
    """Run seven ordered validation callbacks against an isolated database.

    ``collect`` is injected so tests and operators can use recorded fixtures
    or live historical calls.  The runner itself never opens a provider and
    never changes ``PublicationPointer`` rows.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        environment: str = "historical_rehearsal",
        clock: Callable[[], datetime] | None = None,
        isolated_engine: Engine | None = None,
    ) -> None:
        self.engine = engine
        # ``engine`` is the production/control-plane snapshot source.  An
        # explicit isolated engine is required for real rehearsal evidence;
        # keeping the default equal preserves the small in-process test seam.
        self.isolated_engine = isolated_engine or engine
        self.environment = environment
        self.clock = clock or (lambda: datetime.now(UTC))
        self._session = sessionmaker(bind=engine, expire_on_commit=False)
        self._isolated_session = sessionmaker(
            bind=self.isolated_engine, expire_on_commit=False
        )

    def run(
        self,
        season: str,
        *,
        cutoffs: Iterable[date] = DEFAULT_REHEARSAL_DATES,
        collect: Callable[[date], Mapping[str, Any]] | None = None,
        synergy_check: Callable[[date], Any] | None = None,
    ) -> HistoricalRehearsalReport:
        if self.environment.lower() in {"production", "prod"}:
            return self._failed(
                season,
                "historical rehearsal must use an isolated non-production environment",
            )
        dates = tuple(cutoffs)
        if len(dates) != REHEARSAL_DATE_COUNT:
            return self._failed(
                season,
                f"historical rehearsal requires exactly {REHEARSAL_DATE_COUNT} dates",
            )
        if dates != tuple(sorted(set(dates))):
            return self._failed(season, "historical rehearsal dates must be ordered and unique")
        if any(isinstance(cutoff, datetime) or not isinstance(cutoff, date) for cutoff in dates):
            return self._failed(season, "historical rehearsal cutoffs must be calendar dates")
        if collect is None:
            return self._failed(
                season,
                "historical rehearsal requires an isolated collection/composition callback",
            )
        if synergy_check is None:
            return self._failed(
                season,
                "historical rehearsal requires a completed-season Synergy validation callback",
            )
        records: list[RehearsalRecord] = []
        before: tuple[tuple[str, str | None, str | None, int], ...] = ()
        try:
            before = self._pointer_snapshot()
            for sequence, cutoff in enumerate(dates, start=1):
                raw_details = collect(cutoff)
                if not isinstance(raw_details, Mapping):
                    raise ValueError(
                        "collection/composition callback must return a JSON object"
                    )
                details = dict(raw_details)
                status = str(details.get("status", ""))
                if status != "passed":
                    raise ValueError(
                        "historical rehearsal requires every date to pass; "
                        "skipped or failed dates are not evidence"
                    )
                publication_ids = details.get("publication_ids")
                if not isinstance(publication_ids, Mapping) or not publication_ids:
                    raise ValueError(
                        "each rehearsal date requires immutable publication_ids"
                    )
                parity = details.get("parity")
                if not isinstance(parity, Mapping):
                    raise ValueError(
                        "each rehearsal date requires exact/adjudicated parity evidence"
                    )
                if not isinstance(parity.get("equal"), bool) or not isinstance(
                    parity.get("differences"), list
                ):
                    raise ValueError("parity must include boolean equal and differences list")
                if bool(parity["equal"]) != (not parity["differences"]):
                    raise ValueError(
                        "parity equal must agree with the recorded differences"
                    )
                decision = str(parity.get("decision", ""))
                if decision not in {"exact", "approved", "rejected"}:
                    raise ValueError("parity evidence requires an adjudication decision")
                if parity["differences"] and decision == "exact":
                    raise ValueError(
                        "non-empty parity differences require adjudicated approval/rejection"
                    )
                if parity["equal"] and decision != "exact":
                    raise ValueError("exact parity requires the exact decision")
                self._assert_isolated_publications(
                    publication_ids, season=season, cutoff=cutoff
                )
                streams = tuple(sorted(str(key) for key in publication_ids))
                records.append(
                    RehearsalRecord(
                        sequence=sequence,
                        cutoff=cutoff.isoformat(),
                        status=status,
                        streams=streams,
                        details={
                            **details,
                            "publication_ids": {
                                str(key): str(value)
                                for key, value in publication_ids.items()
                            },
                            "parity": dict(parity),
                        },
                    )
                )
            result = synergy_check(dates[-1])
            if not isinstance(result, Mapping) or str(result.get("status")) != "passed":
                raise ValueError("completed-season Synergy validation failed")
            synergy_id = result.get("candidate_publication_id")
            if not isinstance(synergy_id, str) or not synergy_id.strip():
                raise ValueError(
                    "completed-season Synergy validation requires a candidate publication"
                )
            self._assert_isolated_publications(
                {"synergy_play_types": synergy_id},
                season=season,
                cutoff=dates[-1],
            )
            synergy_status = "passed"
            after = self._pointer_snapshot()
            unchanged = before == after
            if not unchanged:
                raise ValueError("historical rehearsal changed a production pointer")
            return HistoricalRehearsalReport(
                season=season,
                environment=self.environment,
                status="passed",
                records=tuple(records),
                synergy_season_status=synergy_status,
                production_pointers_unchanged=True,
            )
        except Exception as error:
            try:
                after_failure = self._pointer_snapshot()
                unchanged = bool(before == after_failure)
            except RuntimeError:
                unchanged = False
            return HistoricalRehearsalReport(
                season=season,
                environment=self.environment,
                status="failed",
                records=tuple(records),
                synergy_season_status="failed",
                production_pointers_unchanged=unchanged,
                error=str(error)[:255],
            )

    def _pointer_snapshot(self) -> tuple[tuple[str, str | None, str | None, int], ...]:
        try:
            with self._session() as session:
                rows = session.scalars(
                    select(PublicationPointer).order_by(PublicationPointer.stream_key)
                ).all()
                return tuple(
                    (
                        row.stream_key,
                        row.active_publication_id,
                        row.previous_publication_id,
                        int(row.fence),
                    )
                    for row in rows
                )
        except Exception as error:
            raise RuntimeError(
                "historical rehearsal requires a migrated isolated control-plane database"
            ) from error

    def _assert_isolated_publications(
        self,
        publication_ids: Mapping[str, Any],
        *,
        season: str,
        cutoff: date,
    ) -> None:
        pairs = tuple(
            (str(key).strip(), str(value).strip())
            for key, value in publication_ids.items()
        )
        ids = tuple(value for _, value in pairs)
        if not ids or any(not value for value in ids):
            raise ValueError("rehearsal publication IDs must be non-empty")
        if len(set(ids)) != len(ids):
            raise ValueError("rehearsal publication IDs must be unique")
        try:
            with self._isolated_session() as session:
                rows = session.scalars(
                    select(PublicationVersion).where(
                        PublicationVersion.publication_id.in_(ids)
                    )
                ).all()
        except Exception as error:
            raise ValueError(
                "historical rehearsal requires a migrated isolated publication database"
            ) from error
        by_id = {row.publication_id: row for row in rows}
        if set(by_id) != set(ids):
            raise ValueError("rehearsal referenced a publication absent from isolated DB")
        for stream_key, publication_id in pairs:
            row = by_id[publication_id]
            if row.stream_key != stream_key:
                raise ValueError("rehearsal publication stream does not match its key")
            if row.season != season or row.cutoff.date() != cutoff:
                raise ValueError("rehearsal publication season/cutoff mismatch")
            if row.status not in {"candidate", "active", "rollback"}:
                raise ValueError("rehearsal publication is not retained evidence")
            self._validate_publication_payload(
                stream_key, row.payload, season=season
            )

    @staticmethod
    def _validate_publication_payload(
        stream_key: str, payload: str, *, season: str
    ) -> None:
        """Reject empty/arbitrary JSON in a rehearsal evidence publication."""

        decoder_streams = {
            "traditional_opponent_season", "traditional_opponent_l15",
            "assist_locations_season", "assist_locations_l15",
            "synergy_play_types_opponent_season",
            "synergy_play_types_opponent_l15",
            "grouped_shot_types_opponent_season",
            "grouped_shot_types_opponent_l15",
            "exact_shot_zones_opponent_season",
            "exact_shot_zones_opponent_l15",
        }
        diet_bases = {
            "synergy_play_types": "play_types",
            "grouped_shot_types": "shot_types",
            "exact_shot_zones": "shot_zones",
            "player_assist_locations": "assist_locations",
        }
        if stream_key not in {
            "player_game_logs", "player_per36", *decoder_streams, *diet_bases
        }:
            return
        try:
            document = json.loads(payload)
            from app.services.database_first_activation import (
                decode_player_diet,
                decode_player_game_logs,
                decode_player_per36,
                decode_team_window,
            )

            if stream_key == "player_game_logs":
                decode_player_game_logs(document, season=season)
            elif stream_key == "player_per36":
                decode_player_per36(document, season=season)
            elif stream_key in diet_bases:
                decode_player_diet(
                    document,
                    base=diet_bases[stream_key],
                    retrieved_at=datetime.now(UTC),
                )
            else:
                decode_team_window(document, stream_key=stream_key)
        except Exception as error:
            raise ValueError(
                f"rehearsal publication payload is invalid for {stream_key}"
            ) from error

    def _failed(self, season: str, error: str) -> HistoricalRehearsalReport:
        return HistoricalRehearsalReport(
            season=season,
            environment=self.environment,
            status="failed",
            records=(),
            synergy_season_status="failed",
            production_pointers_unchanged=True,
            error=error,
        )


HistoricalRehearsal = HistoricalRehearsalRunner

__all__ = [
    "DEFAULT_REHEARSAL_DATES",
    "HistoricalRehearsal",
    "HistoricalRehearsalReport",
    "HistoricalRehearsalRunner",
    "REHEARSAL_DATE_COUNT",
    "RehearsalRecord",
]
