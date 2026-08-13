"""Database-first publication reads and activation validation seams.

The collection control plane owns immutable publication versions and one
fenced pointer per stream.  This module is the deliberately small read-side
authority used by the public Matchups path and by offline validation tools:
it never constructs a provider, never refreshes a legacy table, and serves an
active last-good version even when its age is stale.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isfinite
from typing import Any, Callable, Protocol

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.models.collection_control import (
    PublicationPointer,
    PublicationStream,
    PublicationVersion,
)
from app.services.collection_control import (
    ControlPlaneError,
    PublicationService,
)


UTC = timezone.utc


class PublicationPayloadError(ValueError):
    """An immutable publication payload is not safe to serve as facts."""


class LegacyWriteFenceProtocol(Protocol):
    """Typed seam used by every legacy writer inside its transaction."""

    def assert_writable(
        self, stream_key: str, *, connection: Connection | None = None
    ) -> None: ...

# These are intentionally independent of the provider refresh windows.  A
# publication's age describes the product fact, while a provider's age
# describes how it was obtained.
PUBLICATION_FRESHNESS_SECONDS: dict[str, int] = {
    "cutoff_current": 60 * 60,
    "daily_recheck": 24 * 60 * 60,
    "seven_day": 7 * 24 * 60 * 60,
    "request_time": 0,
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _payload_rows(payload: Any, *, stream_key: str) -> tuple[Mapping[str, Any], ...]:
    """Return only the documented row envelope; reject lossy coercions."""

    rows: Any = payload
    if isinstance(payload, Mapping):
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise PublicationPayloadError(
                f"{stream_key} publication must contain a rows list"
            )
    if not isinstance(rows, list):
        raise PublicationPayloadError(f"{stream_key} publication rows are invalid")
    result: list[Mapping[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise PublicationPayloadError(
                f"{stream_key} publication row {index} is not an object"
            )
        result.append(row)
    return tuple(result)


def _required(row: Mapping[str, Any], key: str, *, stream_key: str) -> Any:
    if key not in row or row[key] is None:
        raise PublicationPayloadError(
            f"{stream_key} publication row is missing {key}"
        )
    return row[key]


def _strict_int(value: Any, *, field: str, stream_key: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PublicationPayloadError(
            f"{stream_key} publication field {field} must be an integer"
        )
    if minimum is not None and value < minimum:
        raise PublicationPayloadError(
            f"{stream_key} publication field {field} is out of range"
        )
    return value


def _strict_float(
    value: Any,
    *,
    field: str,
    stream_key: str,
    minimum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise PublicationPayloadError(
            f"{stream_key} publication field {field} must be numeric"
        )
    if not isinstance(value, (int, float)):
        raise PublicationPayloadError(
            f"{stream_key} publication field {field} must be numeric"
        )
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise PublicationPayloadError(
            f"{stream_key} publication field {field} must be numeric"
        ) from error
    if not isfinite(result) or (minimum is not None and result < minimum):
        raise PublicationPayloadError(
            f"{stream_key} publication field {field} is out of range"
        )
    return result


def _strict_text(value: Any, *, field: str, stream_key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicationPayloadError(
            f"{stream_key} publication field {field} must be non-empty text"
        )
    return value.strip()


def _strict_date(value: Any, *, field: str, stream_key: str) -> date:
    if isinstance(value, datetime):
        return _utc(value).date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise PublicationPayloadError(
            f"{stream_key} publication field {field} must be an ISO date"
        )
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise PublicationPayloadError(
            f"{stream_key} publication field {field} must be an ISO date"
        ) from error


def _strict_mapping(value: Any, *, field: str, stream_key: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise PublicationPayloadError(
            f"{stream_key} publication field {field} must be a non-empty object"
        )
    return value


def decode_player_game_logs(
    payload: Any, *, season: str | None = None
) -> tuple[Any, ...]:
    """Decode the canonical player-log payload into repository records.

    The local import keeps the control-plane reader independent of the legacy
    table implementation while returning the exact type consumed by both the
    Matchups and Selection services.
    """

    from app.services.player_game_log_repository import PlayerGameLogRecord

    stream_key = "player_game_logs"
    decoded = []
    required = tuple(PlayerGameLogRecord.__dataclass_fields__)
    for row in _payload_rows(payload, stream_key=stream_key):
        for key in required:
            _required(row, key, stream_key=stream_key)
        row_season = _strict_text(row["season"], field="season", stream_key=stream_key)
        if season is not None and row_season != season:
            raise PublicationPayloadError("player_game_logs publication season mismatch")
        season_type = _strict_text(row["season_type"], field="season_type", stream_key=stream_key)
        if season_type != "Regular Season":
            raise PublicationPayloadError("player_game_logs publication is not Regular Season")
        if not isinstance(row["is_home"], bool):
            raise PublicationPayloadError("player_game_logs publication is_home must be boolean")
        values = {
            "season": row_season,
            "season_type": season_type,
            "player_id": _strict_int(row["player_id"], field="player_id", stream_key=stream_key, minimum=1),
            "game_id": _strict_text(row["game_id"], field="game_id", stream_key=stream_key),
            "player_name": _strict_text(row["player_name"], field="player_name", stream_key=stream_key),
            "game_date": _strict_date(row["game_date"], field="game_date", stream_key=stream_key),
            "team_id": _strict_int(row["team_id"], field="team_id", stream_key=stream_key, minimum=1),
            "team_tricode": _strict_text(row["team_tricode"], field="team_tricode", stream_key=stream_key),
            "opponent_team_id": _strict_int(row["opponent_team_id"], field="opponent_team_id", stream_key=stream_key, minimum=1),
            "opponent_team_tricode": _strict_text(row["opponent_team_tricode"], field="opponent_team_tricode", stream_key=stream_key),
            "is_home": row["is_home"],
            "minutes": _strict_float(row["minutes"], field="minutes", stream_key=stream_key, minimum=0),
        }
        integer_fields = (
            "points", "rebounds", "assists", "field_goals_made",
            "field_goals_attempted", "three_pointers_made",
            "three_pointers_attempted", "free_throws_made",
            "free_throws_attempted", "offensive_rebounds", "defensive_rebounds",
            "turnovers", "steals", "blocks", "personal_fouls",
        )
        for field in integer_fields:
            values[field] = _strict_int(
                row[field], field=field, stream_key=stream_key, minimum=0
            )
        decoded.append(PlayerGameLogRecord(**values))
    if not decoded:
        raise PublicationPayloadError("player_game_logs publication is empty")
    return tuple(decoded)


def decode_player_diet(
    payload: Any, *, base: str, retrieved_at: datetime
) -> tuple[Any, ...]:
    """Decode one governed player-Diet stream without filling defaults."""

    from app.services.player_diet import PlayerDietFact, PLAYER_DIET_BASES

    stream_key = {
        "play_types": "synergy_play_types",
        "shot_types": "grouped_shot_types",
        "shot_zones": "exact_shot_zones",
    }.get(base)
    if stream_key is None or base not in PLAYER_DIET_BASES:
        raise PublicationPayloadError(f"unsupported player Diet publication base {base}")
    if isinstance(payload, Mapping) and payload.get("base") not in (None, base):
        raise PublicationPayloadError(f"{stream_key} publication base mismatch")
    rows = _payload_rows(payload, stream_key=stream_key)
    result = []
    expected_units = {
        "play_types": "possessions",
        "shot_types": "field_goal_attempts",
        "shot_zones": "field_goal_attempts",
    }
    expected_providers = {
        "play_types": "nba_synergy",
        "shot_types": "nba_stats",
        "shot_zones": "nba_stats",
    }
    for row in rows:
        for field in (
            "player_id", "slice_key", "share", "volume", "games_played",
            "volume_unit", "provider",
        ):
            _required(row, field, stream_key=stream_key)
        if _strict_text(row["volume_unit"], field="volume_unit", stream_key=stream_key) != expected_units[base]:
            raise PublicationPayloadError(f"{stream_key} publication volume unit mismatch")
        if _strict_text(row["provider"], field="provider", stream_key=stream_key) != expected_providers[base]:
            raise PublicationPayloadError(f"{stream_key} publication provider mismatch")
        share = _strict_float(
            row["share"], field="share", stream_key=stream_key, minimum=0
        )
        if share > 1:
            raise PublicationPayloadError(f"{stream_key} publication share is out of range")
        result.append(PlayerDietFact(
            player_id=_strict_int(row["player_id"], field="player_id", stream_key=stream_key, minimum=1),
            base=base,
            slice_key=_strict_text(row["slice_key"], field="slice_key", stream_key=stream_key),
            share=share,
            volume=_strict_float(row["volume"], field="volume", stream_key=stream_key, minimum=0),
            games_played=_strict_int(row["games_played"], field="games_played", stream_key=stream_key, minimum=1),
            volume_unit=expected_units[base],
            provider=expected_providers[base],
        ))
    if not result:
        raise PublicationPayloadError(f"{stream_key} publication is empty")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class PublicationTeamWindowRow:
    """Validated row from a TeamWindowMetric publication."""

    team_id: int
    team_tricode: str
    game_ids: tuple[str, ...]
    game_count: int
    per48: Mapping[str, float]
    league_average: Mapping[str, float]
    population_sigma: Mapping[str, float]
    competition_rank: Mapping[str, int]


def decode_team_window(payload: Any, *, stream_key: str) -> tuple[PublicationTeamWindowRow, ...]:
    """Decode a complete, immutable team-window publication."""

    if stream_key not in {
        "traditional_opponent_season", "traditional_opponent_l15",
        "assist_locations_season", "assist_locations_l15",
    }:
        raise PublicationPayloadError(f"unsupported team-window publication {stream_key}")
    rows = _payload_rows(payload, stream_key=stream_key)
    decoded = []
    for row in rows:
        for field in (
            "team_id", "team_tricode", "game_ids", "game_count", "per48",
            "league_average", "population_sigma", "competition_rank",
        ):
            _required(row, field, stream_key=stream_key)
        game_ids = row["game_ids"]
        if not isinstance(game_ids, (list, tuple)) or any(
            not isinstance(value, str) or not value.strip() for value in game_ids
        ):
            raise PublicationPayloadError(f"{stream_key} publication game_ids are invalid")
        per48 = _strict_mapping(row["per48"], field="per48", stream_key=stream_key)
        average = _strict_mapping(row["league_average"], field="league_average", stream_key=stream_key)
        sigma = _strict_mapping(row["population_sigma"], field="population_sigma", stream_key=stream_key)
        ranks = _strict_mapping(row["competition_rank"], field="competition_rank", stream_key=stream_key)
        keys = set(per48)
        if keys != set(average) or keys != set(sigma) or keys != set(ranks):
            raise PublicationPayloadError(f"{stream_key} publication metric keys are inconsistent")
        numeric = {
            key: _strict_float(value, field=f"per48.{key}", stream_key=stream_key)
            for key, value in per48.items()
        }
        avg_numeric = {
            key: _strict_float(value, field=f"league_average.{key}", stream_key=stream_key)
            for key, value in average.items()
        }
        sigma_numeric = {
            key: _strict_float(value, field=f"population_sigma.{key}", stream_key=stream_key, minimum=0)
            for key, value in sigma.items()
        }
        rank_numeric = {
            key: _strict_int(value, field=f"competition_rank.{key}", stream_key=stream_key, minimum=1)
            for key, value in ranks.items()
        }
        decoded.append(PublicationTeamWindowRow(
            team_id=_strict_int(row["team_id"], field="team_id", stream_key=stream_key, minimum=1),
            team_tricode=_strict_text(row["team_tricode"], field="team_tricode", stream_key=stream_key),
            game_ids=tuple(game_ids),
            game_count=_strict_int(row["game_count"], field="game_count", stream_key=stream_key, minimum=1),
            per48=numeric,
            league_average=avg_numeric,
            population_sigma=sigma_numeric,
            competition_rank=rank_numeric,
        ))
    if not decoded:
        raise PublicationPayloadError(f"{stream_key} publication is empty")
    if len({row.team_id for row in decoded}) != len(decoded):
        raise PublicationPayloadError(f"{stream_key} publication repeats a team")
    if any(row.game_count != len(row.game_ids) for row in decoded):
        raise PublicationPayloadError(f"{stream_key} publication game count is inconsistent")
    return tuple(decoded)


@dataclass(frozen=True, slots=True)
class PublicationRead:
    """One immutable stream read and its bounded provenance."""

    stream_key: str
    publication_id: str | None
    season: str | None
    cutoff: str | None
    version: int | None
    status: str
    freshness: str
    age_seconds: int | None
    payload: Any | None
    source: str = "database"
    retrieved_at: datetime | None = None
    legacy_fallback_allowed: bool = False
    checksum: str | None = None

    @property
    def available(self) -> bool:
        # A rollback pointer still names a known-good immutable publication.
        # Keep its rollback status visible to callers instead of treating the
        # safety action itself as data loss.
        return self.payload is not None and self.status in {"active", "rollback", "stale"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream_key": self.stream_key,
            "publication_id": self.publication_id,
            "season": self.season,
            "coverage_cutoff": self.cutoff,
            "version": self.version,
            "status": self.status,
            "freshness": self.freshness,
            "age_seconds": self.age_seconds,
            "source": self.source,
            "legacy_fallback_allowed": self.legacy_fallback_allowed,
            "payload_checksum": self.checksum,
        }


class DatabaseFirstPublicationReader:
    """Read active publication pointers without provider or legacy fallback."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] | None = None,
        freshness_seconds: Mapping[str, int] | None = None,
    ) -> None:
        self.engine = engine
        self._session = sessionmaker(bind=engine, expire_on_commit=False)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.freshness_seconds = dict(
            freshness_seconds or PUBLICATION_FRESHNESS_SECONDS
        )

    def read(
        self,
        stream_key: str,
        *,
        season: str | None = None,
        require_active: bool = True,
    ) -> PublicationRead:
        """Return one active last-good publication, including stale values.

        ``require_active`` is useful for rehearsal reads of an inactive
        candidate.  Public callers leave it at its safe default.
        """

        with self._session() as session:
            stream = session.scalar(
                select(PublicationStream).where(
                    PublicationStream.stream_key == stream_key
                )
            )
            pointer = session.scalar(
                select(PublicationPointer).where(
                    PublicationPointer.stream_key == stream_key
                )
            )
            if stream is None:
                return self._missing(stream_key, "unavailable")
            if require_active and not bool(stream.enabled):
                return self._legacy_fallback(stream_key)
            if pointer is None or not pointer.active_publication_id:
                return self._missing(stream_key, "missing")
            publication = session.scalar(
                select(PublicationVersion).where(
                    PublicationVersion.publication_id == pointer.active_publication_id
                )
            )

        if publication is None or publication.status not in {"active", "rollback"}:
            return self._missing(stream_key, "missing")
        if season is not None and publication.season != season:
            return self._missing(stream_key, "missing")
        try:
            payload = json.loads(publication.payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            # A corrupt rendered document must never make the read path fall
            # back to a provider or to a partial prior attempt.
            return self._missing(stream_key, "unavailable")
        now = _utc(self.clock())
        age = max(0, int((now - _utc(publication.created_at)).total_seconds()))
        threshold = self.freshness_seconds.get(str(stream.freshness_rule))
        freshness = "fresh" if threshold is not None and age <= threshold else "stale"
        return PublicationRead(
            stream_key=stream_key,
            publication_id=publication.publication_id,
            season=publication.season,
            cutoff=_utc(publication.cutoff).isoformat(),
            version=int(publication.version),
            status="active" if publication.status == "active" else "rollback",
            freshness=freshness,
            age_seconds=age,
            payload=payload,
            retrieved_at=_utc(publication.created_at),
            checksum=publication.checksum,
        )

    def read_many(
        self,
        stream_keys: Iterable[str],
        *,
        season: str | None = None,
    ) -> dict[str, PublicationRead]:
        """Read streams independently; one missing stream never hides others."""

        return {
            stream_key: self.read(stream_key, season=season)
            for stream_key in sorted(set(stream_keys))
        }

    def metadata(
        self,
        stream_keys: Iterable[str],
        *,
        season: str | None = None,
    ) -> dict[str, Any]:
        reads = self.read_many(stream_keys, season=season)
        available = [read for read in reads.values() if read.available]
        cutoffs = {read.cutoff for read in available if read.cutoff}
        freshness = {read.freshness for read in available}
        return {
            "streams": {key: read.to_dict() for key, read in reads.items()},
            "mixed_cutoff": len(cutoffs) > 1,
            "mixed_freshness": len(freshness) > 1,
            "coverage_cutoffs": sorted(cutoffs),
        }

    @staticmethod
    def _missing(stream_key: str, status: str) -> PublicationRead:
        return PublicationRead(
            stream_key=stream_key,
            publication_id=None,
            season=None,
            cutoff=None,
            version=None,
            status=status,
            freshness="missing" if status == "missing" else "unavailable",
            age_seconds=None,
            payload=None,
        )

    @staticmethod
    def _legacy_fallback(stream_key: str) -> PublicationRead:
        return PublicationRead(
            stream_key=stream_key,
            publication_id=None,
            season=None,
            cutoff=None,
            version=None,
            status="inactive",
            freshness="legacy_fallback",
            age_seconds=None,
            payload=None,
            source="legacy_database",
            legacy_fallback_allowed=True,
        )


class DatabaseOnlyProviderGuard:
    """Test/assembly guard that fails on every provider attribute access."""

    def __init__(self, name: str = "provider") -> None:
        self.name = name

    def __getattr__(self, operation: str) -> Any:
        raise AssertionError(
            f"database-only Matchups read attempted {self.name}.{operation}"
        )


class LegacyWriteFence:
    """Reject legacy writes once the corresponding stream is activated."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def is_activated(
        self, stream_key: str, *, connection: Connection | None = None
    ) -> bool:
        def read(bound: Connection) -> bool:
            row = bound.execute(
                select(PublicationStream.enabled)
                .where(PublicationStream.stream_key == stream_key)
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise ControlPlaneError("legacy_write_fence_unavailable")
            return bool(row)

        if connection is not None:
            try:
                return read(connection)
            except SQLAlchemyError as error:
                raise ControlPlaneError("legacy_write_fence_unavailable") from error
        try:
            with self.engine.begin() as bound:
                return read(bound)
        except ControlPlaneError:
            raise
        except SQLAlchemyError as error:
            # A missing/partially migrated control plane must fail closed.  A
            # writer cannot infer that an absent row means it is safe to write.
            raise ControlPlaneError("legacy_write_fence_unavailable") from error

    def assert_writable(
        self, stream_key: str, *, connection: Connection | None = None
    ) -> None:
        if self.is_activated(stream_key, connection=connection):
            raise ControlPlaneError("legacy_write_fenced")

    def guard(self, stream_key: str) -> Callable[[], None]:
        return lambda: self.assert_writable(stream_key)


class DatabaseFirstActivationService:
    """Convenience facade used by rehearsal and operator tooling."""

    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] | None = None) -> None:
        self.publications = PublicationService(engine, clock=clock)
        self.reader = DatabaseFirstPublicationReader(engine, clock=clock)
        self.fence = LegacyWriteFence(engine)

    def activate(self, stream_key: str, *, actor: str, reason: str, **kwargs: Any) -> Any:
        if not kwargs.get("candidate_publication_id"):
            raise ControlPlaneError("publication_candidate_required")
        return self.publications.activate_stream(
            stream_key,
            actor=actor,
            reason=reason,
            require_candidate=True,
            **kwargs,
        )

    def rollback(self, stream_key: str, *, reason: str, expected_fence: int | None = None) -> Any:
        return self.publications.rollback(
            stream_key, reason=reason, expected_fence=expected_fence
        )


# Friendly names for callers that use the packet vocabulary.
PublicationReadRouter = DatabaseFirstPublicationReader
DatabaseFirstMatchupsReader = DatabaseFirstPublicationReader
ProviderCallGuard = DatabaseOnlyProviderGuard
ActivationService = DatabaseFirstActivationService


__all__ = [
    "ActivationService",
    "DatabaseFirstActivationService",
    "DatabaseFirstMatchupsReader",
    "DatabaseFirstPublicationReader",
    "DatabaseOnlyProviderGuard",
    "LegacyWriteFence",
    "LegacyWriteFenceProtocol",
    "PUBLICATION_FRESHNESS_SECONDS",
    "PublicationPayloadError",
    "PublicationRead",
    "PublicationTeamWindowRow",
    "PublicationReadRouter",
    "ProviderCallGuard",
    "decode_player_diet",
    "decode_player_game_logs",
    "decode_team_window",
]
