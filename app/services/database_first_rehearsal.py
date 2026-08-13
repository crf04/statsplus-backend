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

from app.models.collection_control import PublicationPointer


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
    ) -> None:
        self.engine = engine
        self.environment = environment
        self.clock = clock or (lambda: datetime.now(UTC))
        self._session = sessionmaker(bind=engine, expire_on_commit=False)

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
                details = dict(collect(cutoff))
                if "status" not in details and not details:
                    raise ValueError("collection/composition callback must return status")
                status = str(details.pop("status", "passed"))
                if status not in {"passed", "failed", "skipped"}:
                    raise ValueError("rehearsal callback returned an invalid status")
                streams = tuple(sorted(str(value) for value in details.pop("streams", ())))
                records.append(
                    RehearsalRecord(
                        sequence=sequence,
                        cutoff=cutoff.isoformat(),
                        status=status,
                        streams=streams,
                        details=details,
                    )
                )
                if status == "failed":
                    raise ValueError(f"historical rehearsal failed at {cutoff.isoformat()}")
            result = synergy_check(dates[-1])
            synergy_status = "passed" if result is True else str(result)
            if synergy_status != "passed":
                raise ValueError("completed-season Synergy validation failed")
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
