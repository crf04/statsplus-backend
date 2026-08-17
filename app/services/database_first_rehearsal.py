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

from app.domain.publication_integrity import publication_payload_matches_checksum
from app.models.canonical_game_ledger import LedgerParityArtifact
from app.models.collection_control import PublicationPointer, PublicationVersion
from app.services.publication_authority import verify_publication_authority
from app.services.team_matchup_publications import NBA_PUBLICATION_STREAM_KEYS


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
    production_immutability_checked: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "season": self.season,
            "environment": self.environment,
            "status": self.status,
            "records": [asdict(record) for record in self.records],
            "synergy_season_status": self.synergy_season_status,
            "production_pointers_unchanged": self.production_pointers_unchanged,
            "production_immutability_checked": self.production_immutability_checked,
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
        production_engine: Engine | None = None,
    ) -> None:
        self.engine = engine
        self.isolated_engine = isolated_engine or engine
        self.production_engine = production_engine
        self.environment = environment
        self.clock = clock or (lambda: datetime.now(UTC))
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
        environment = self.environment.lower()
        if environment in {"production", "prod"}:
            return self._failed(
                season,
                "historical rehearsal must use an isolated non-production environment",
            )
        production_checked = environment not in {"unit", "test_unit"}
        if production_checked and (
            self.production_engine is None
            or self.production_engine is self.isolated_engine
            or str(self.production_engine.url) == str(self.isolated_engine.url)
        ):
            return self._failed(
                season,
                "operator rehearsal requires an explicit separate production snapshot database",
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
            if production_checked:
                before = self._pointer_snapshot(self.production_engine)
            for sequence, cutoff in enumerate(dates, start=1):
                raw_details = collect(cutoff)
                if not isinstance(raw_details, Mapping):
                    raise ValueError(
                        "collection/composition callback must return a JSON object"
                    )
                details = dict(raw_details)
                forbidden_assertions = {
                    "status", "parity", "equal", "differences", "decision"
                }
                if forbidden_assertions & set(details):
                    raise ValueError(
                        "collection callback must return raw facts; parity and status "
                        "assertions are derived by the rehearsal"
                    )
                publication_ids = details.get("publication_ids")
                if not isinstance(publication_ids, Mapping) or not publication_ids:
                    raise ValueError(
                        "each rehearsal date requires immutable publication_ids"
                    )
                expected_facts = self._expected_facts(details)
                if expected_facts is None:
                    raise ValueError(
                        "each rehearsal date requires governed raw expected/legacy facts"
                    )
                publications = self._load_isolated_publications(
                    publication_ids, season=season, cutoff=cutoff
                )
                streams = tuple(sorted(str(key) for key in publication_ids))
                parity = self._derive_parity(
                    publications, expected_facts, season=season
                )
                records.append(
                    RehearsalRecord(
                        sequence=sequence,
                        cutoff=cutoff.isoformat(),
                        status="passed",
                        streams=streams,
                        details={
                            **details,
                            "publication_ids": {
                                str(key): str(value)
                                for key, value in publication_ids.items()
                            },
                            "parity": parity,
                        },
                    )
                )
            result = synergy_check(dates[-1])
            if not isinstance(result, Mapping):
                raise ValueError("completed-season Synergy callback must return raw facts")
            if {"status", "parity", "decision", "equal"} & set(result):
                raise ValueError(
                    "Synergy callback must return raw facts; parity is derived"
                )
            synergy_id = result.get("candidate_publication_id")
            if not isinstance(synergy_id, str) or not synergy_id.strip():
                raise ValueError(
                    "completed-season Synergy validation requires a candidate publication"
                )
            synergy_expected = self._expected_facts(result)
            if synergy_expected is None:
                raise ValueError("completed-season Synergy callback requires raw expected facts")
            synergy_publications = self._load_isolated_publications(
                {"synergy_play_types": synergy_id},
                season=season,
                cutoff=dates[-1],
            )
            self._derive_parity(
                synergy_publications, synergy_expected, season=season
            )
            synergy_status = "passed"
            unchanged = False
            if production_checked:
                after = self._pointer_snapshot(self.production_engine)
                unchanged = before == after
                if not unchanged:
                    raise ValueError("historical rehearsal changed a production pointer")
            return HistoricalRehearsalReport(
                season=season,
                environment=self.environment,
                status="passed",
                records=tuple(records),
                synergy_season_status=synergy_status,
                production_pointers_unchanged=unchanged,
                production_immutability_checked=production_checked,
            )
        except Exception as error:
            unchanged = False
            if production_checked:
                try:
                    after_failure = self._pointer_snapshot(self.production_engine)
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
                production_immutability_checked=production_checked,
                error=str(error)[:255],
            )

    def _pointer_snapshot(
        self, engine: Engine | None
    ) -> tuple[tuple[str, str | None, str | None, int], ...]:
        try:
            if engine is None:
                raise RuntimeError("production snapshot database is not configured")
            with sessionmaker(bind=engine, expire_on_commit=False)() as session:
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

    @staticmethod
    def _expected_facts(details: Mapping[str, Any]) -> Mapping[str, Any] | None:
        for key in ("expected_facts", "governed_facts", "legacy_facts", "facts"):
            value = details.get(key)
            if isinstance(value, Mapping) and value:
                return value
        return None

    def _load_isolated_publications(
        self,
        publication_ids: Mapping[str, Any],
        *,
        season: str,
        cutoff: date,
    ) -> Mapping[str, PublicationVersion]:
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
        publications: dict[str, PublicationVersion] = {}
        for stream_key, publication_id in pairs:
            row = by_id[publication_id]
            if row.stream_key != stream_key:
                raise ValueError("rehearsal publication stream does not match its key")
            if row.season != season or row.cutoff.date() != cutoff:
                raise ValueError("rehearsal publication season/cutoff mismatch")
            if row.status not in {"candidate", "active", "rollback"}:
                raise ValueError("rehearsal publication is not retained evidence")
            if not publication_payload_matches_checksum(row.payload, row.checksum):
                raise ValueError("rehearsal publication checksum mismatch")
            if stream_key in NBA_PUBLICATION_STREAM_KEYS:
                try:
                    verify_publication_authority(session, row)
                except ValueError as error:
                    raise ValueError(
                        "rehearsal publication authority mismatch"
                    ) from error
            self._validate_publication_payload(
                stream_key, row.payload, season=season
            )
            publications[stream_key] = row
        return publications

    def _derive_parity(
        self,
        publications: Mapping[str, PublicationVersion],
        expected_facts: Mapping[str, Any],
        *,
        season: str,
    ) -> dict[str, Any]:
        comparisons: dict[str, Any] = {}
        for stream_key, publication in publications.items():
            expected = expected_facts.get(stream_key)
            if expected is None:
                expected = expected_facts.get(publication.publication_id)
            if expected is None:
                raise ValueError(
                    f"raw expected facts are missing for {stream_key}"
                )
            equal, mode = self._compare_payloads(publication.payload, expected)
            comparison = {
                "equal": equal,
                "mode": mode,
                "publication_id": publication.publication_id,
                "checksum": publication.checksum,
            }
            if not equal:
                artifact = self._approved_artifact(
                    publication, stream_key=stream_key, season=season
                )
                if artifact is None:
                    raise ValueError(
                        f"{stream_key} parity is pending or rejected; persisted approved adjudication required"
                    )
                comparison["adjudication"] = {
                    "artifact_id": artifact.artifact_id,
                    "decision": artifact.decision,
                    "actor": artifact.adjudicated_by,
                }
            comparisons[stream_key] = comparison
        return {
            "equal": all(item["equal"] for item in comparisons.values()),
            "differences": [
                stream_key
                for stream_key, item in comparisons.items()
                if not item["equal"]
            ],
            "comparisons": comparisons,
            "season": season,
        }

    @staticmethod
    def _compare_payloads(actual: str, expected: Any) -> tuple[bool, str]:
        if isinstance(expected, Mapping) and set(expected) == {"payload"}:
            expected = expected["payload"]
        actual_bytes = actual.encode("utf-8")
        if isinstance(expected, bytes):
            expected_bytes = expected
        elif isinstance(expected, str):
            expected_bytes = expected.encode("utf-8")
        else:
            expected_bytes = json.dumps(
                expected, sort_keys=True, separators=(",", ":"), default=str
            ).encode("utf-8")
        if actual_bytes == expected_bytes:
            return True, "byte_exact"
        try:
            actual_document = json.loads(actual)
            expected_document = json.loads(expected_bytes.decode("utf-8"))
        except (TypeError, ValueError, UnicodeDecodeError):
            return False, "byte_mismatch"
        normalized_actual = json.dumps(
            actual_document, sort_keys=True, separators=(",", ":"), default=str
        )
        normalized_expected = json.dumps(
            expected_document, sort_keys=True, separators=(",", ":"), default=str
        )
        return normalized_actual == normalized_expected, "normalized_json"

    def _approved_artifact(
        self,
        publication: PublicationVersion,
        *,
        stream_key: str,
        season: str,
    ) -> LedgerParityArtifact | None:
        try:
            with self._isolated_session() as session:
                artifact = session.scalar(
                    select(LedgerParityArtifact).where(
                        LedgerParityArtifact.publication_id == publication.publication_id,
                        LedgerParityArtifact.stream_key == stream_key,
                        LedgerParityArtifact.season == season,
                        LedgerParityArtifact.cutoff == publication.cutoff,
                    ).order_by(LedgerParityArtifact.created_at.desc()).limit(1)
                )
                if artifact is None or artifact.payload_checksum != publication.checksum:
                    return None
                if artifact.status == "exact":
                    return None
                return artifact if artifact.decision == "approved" else None
        except Exception as error:
            raise ValueError("rehearsal parity artifact lookup failed") from error

    @staticmethod
    def _validate_publication_payload(
        stream_key: str, payload: str, *, season: str
    ) -> None:
        """Reject empty/arbitrary JSON in a rehearsal evidence publication."""

        decoder_streams = {
            "traditional_opponent_season", "traditional_opponent_l15",
            "assist_locations_season", "assist_locations_l15",
        } | NBA_PUBLICATION_STREAM_KEYS
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
            production_pointers_unchanged=False,
            production_immutability_checked=False,
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
