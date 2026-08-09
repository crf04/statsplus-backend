"""Transactional persistence for provider athlete mapping decisions."""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from sqlalchemy import and_, insert, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.errors import InvalidConfigurationError
from app.models.athlete_mapping import (
    AthleteMappingDecision,
    AthleteMappingDecisionCandidate,
    AthleteMappingLock,
    AthleteMappingRejection,
    ProviderAthleteMapping,
)
from app.models.athlete_catalog import AthleteCatalog
from app.providers.dfs import AthleteEvidence
from app.providers.nba_stats import validate_canonical_season
from app.services.athlete_mapping_errors import (
    DEFAULT_MAPPING_FAILURE_SUMMARY,
    AthleteMappingPersistenceError,
)
from app.services.athlete_resolver import (
    AthleteResolution,
    CanonicalAthlete,
    MappingResolutionState,
)
from app.utils.db import is_demo_database_url


#: Unresolved outcomes retained as durable, typed observations.  They never
#: create current mapping state because no canonical identity was established.
UNRESOLVED_OBSERVATION_STATES = frozenset(
    {
        MappingResolutionState.UNMATCHED,
        MappingResolutionState.AMBIGUOUS,
        MappingResolutionState.INACTIVE_ONLY,
        MappingResolutionState.TEAM_CONFLICT,
    }
)


def _translate_storage_failures(method):
    """Translate storage failures at the repository boundary.

    Every public read and operator write presents one failure type, so callers
    never have to know that SQLAlchemy is the storage layer.
    """

    @wraps(method)
    def wrapper(*args: Any, **kwargs: Any):
        try:
            return method(*args, **kwargs)
        except SQLAlchemyError as exc:
            raise AthleteMappingPersistenceError(
                DEFAULT_MAPPING_FAILURE_SUMMARY
            ) from exc

    return wrapper


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def _normalized_key(provider: str, provider_athlete_id: str) -> tuple[str, str]:
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be a non-empty string")
    if not isinstance(provider_athlete_id, str) or not provider_athlete_id.strip():
        raise ValueError("provider athlete ID must be a non-empty string")
    return provider.strip().casefold(), provider_athlete_id.strip()


def _operator(operator_id: str) -> str:
    if not isinstance(operator_id, str) or not operator_id.strip():
        raise ValueError("operator identity is required")
    value = operator_id.strip()
    if len(value) > 128:
        raise ValueError("operator identity is too long")
    return value


def _reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("an operator reason is required")
    value = reason.strip()
    if len(value) > 2000:
        raise ValueError("operator reason is too long")
    return value


@dataclass(frozen=True, slots=True)
class ProviderAthleteMappingRecord:
    """Current mapping state for one provider athlete identity."""

    provider: str
    provider_athlete_id: str
    mapping_state: str
    is_active: bool
    season: str | None
    canonical_player_id: int | None
    canonical_name: str | None
    canonical_team_id: int | None
    canonical_team_name: str | None
    canonical_team_abbreviation: str | None
    provider_name: str | None
    provider_team_id: str | None
    provider_team_canonical_id: int | None
    provider_team_name: str | None
    provider_team_abbreviation: str | None
    conflict_canonical_player_id: int | None
    conflict_canonical_name: str | None
    first_seen_at: str
    last_seen_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_athlete_id": self.provider_athlete_id,
            "mapping_state": self.mapping_state,
            "is_active": self.is_active,
            "season": self.season,
            "canonical_player_id": self.canonical_player_id,
            "canonical_name": self.canonical_name,
            "canonical_team_id": self.canonical_team_id,
            "canonical_team_name": self.canonical_team_name,
            "canonical_team_abbreviation": self.canonical_team_abbreviation,
            "provider_name": self.provider_name,
            "provider_team_id": self.provider_team_id,
            "provider_team_canonical_id": self.provider_team_canonical_id,
            "provider_team_name": self.provider_team_name,
            "provider_team_abbreviation": self.provider_team_abbreviation,
            "conflict_canonical_player_id": self.conflict_canonical_player_id,
            "conflict_canonical_name": self.conflict_canonical_name,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
        }


@dataclass(frozen=True, slots=True)
class MappingCandidateRecord:
    """One canonical athlete an unresolved observation could not choose."""

    canonical_player_id: int
    canonical_name: str | None
    canonical_team_id: int | None
    canonical_team_name: str | None
    canonical_team_abbreviation: str | None
    is_active_for_season: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_player_id": self.canonical_player_id,
            "canonical_name": self.canonical_name,
            "canonical_team_id": self.canonical_team_id,
            "canonical_team_name": self.canonical_team_name,
            "canonical_team_abbreviation": self.canonical_team_abbreviation,
            "is_active_for_season": self.is_active_for_season,
        }


@dataclass(frozen=True, slots=True)
class MappingDecisionRecord:
    """One append-only decision or durable unresolved observation."""

    id: int
    provider: str
    provider_athlete_id: str
    requested_season: str | None
    decision_state: str
    canonical_player_id: int | None
    canonical_name: str | None
    canonical_team_id: int | None
    canonical_team_name: str | None
    canonical_team_abbreviation: str | None
    provider_name: str | None
    provider_team_id: str | None
    provider_team_canonical_id: int | None
    provider_team_name: str | None
    provider_team_abbreviation: str | None
    operator_id: str | None
    reason: str | None
    created_at: str
    candidates: tuple[MappingCandidateRecord, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "provider_athlete_id": self.provider_athlete_id,
            "requested_season": self.requested_season,
            "decision_state": self.decision_state,
            "canonical_player_id": self.canonical_player_id,
            "canonical_name": self.canonical_name,
            "canonical_team_id": self.canonical_team_id,
            "canonical_team_name": self.canonical_team_name,
            "canonical_team_abbreviation": self.canonical_team_abbreviation,
            "provider_name": self.provider_name,
            "provider_team_id": self.provider_team_id,
            "provider_team_canonical_id": self.provider_team_canonical_id,
            "provider_team_name": self.provider_team_name,
            "provider_team_abbreviation": self.provider_team_abbreviation,
            "operator_id": self.operator_id,
            "reason": self.reason,
            "created_at": self.created_at,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class MappingRejectionRecord:
    """Durable suppression state for one provider athlete identity."""

    provider: str
    provider_athlete_id: str
    is_active: bool
    reason: str
    operator_id: str
    created_at: str
    cleared_at: str | None
    cleared_by: str | None
    clear_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_athlete_id": self.provider_athlete_id,
            "is_active": self.is_active,
            "reason": self.reason,
            "operator_id": self.operator_id,
            "created_at": self.created_at,
            "cleared_at": self.cleared_at,
            "cleared_by": self.cleared_by,
            "clear_reason": self.clear_reason,
        }


@dataclass(frozen=True, slots=True)
class MappingPersistenceResult:
    """Outcome of one auto-observation or manual decision."""

    state: str
    persisted: bool
    mapping: ProviderAthleteMappingRecord | None = None
    decision: MappingDecisionRecord | None = None


class AthleteMappingRepository:
    """Own durable current state, append-only decisions, and suppressions."""

    _identity_locks: dict[tuple[int, str, str], threading.RLock] = {}
    _identity_locks_guard = threading.Lock()

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Any | None = None,
    ) -> None:
        self.engine = engine
        if is_demo_database_url(str(getattr(engine, "url", ""))):
            raise InvalidConfigurationError(
                "The bundled demo database is read-only and cannot store athlete mappings."
            )
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @contextmanager
    def _transaction(self, provider: str, provider_id: str):
        """Serialize an identity before reading or changing any state.

        The in-process lock prevents SQLite writers from racing before the
        durable lock row exists; the row itself is selected for update on
        databases that support row locks.
        """
        key = (id(self.engine), provider, provider_id)
        with self._identity_locks_guard:
            lock = self._identity_locks.setdefault(key, threading.RLock())
        with lock, self.engine.begin() as connection:
            try:
                # The savepoint must be released or rolled back by leaving the
                # block, so the duplicate is caught outside it.  PostgreSQL
                # otherwise leaves the surrounding transaction aborted.
                with connection.begin_nested():
                    connection.execute(
                        insert(AthleteMappingLock.__table__).values(
                            provider=provider, provider_athlete_id=provider_id
                        )
                    )
            except IntegrityError:
                pass
            connection.execute(
                select(AthleteMappingLock.__table__)
                .where(
                    and_(
                        AthleteMappingLock.provider == provider,
                        AthleteMappingLock.provider_athlete_id == provider_id,
                    )
                )
                .with_for_update()
            ).one()
            yield connection

    # -- reads -------------------------------------------------------------

    @_translate_storage_failures
    def get_mapping(
        self, provider: str, provider_athlete_id: str
    ) -> ProviderAthleteMappingRecord | None:
        provider, provider_athlete_id = _normalized_key(provider, provider_athlete_id)
        with self.engine.connect() as connection:
            row = connection.execute(
                select(ProviderAthleteMapping.__table__).where(
                    and_(
                        ProviderAthleteMapping.provider == provider,
                        ProviderAthleteMapping.provider_athlete_id == provider_athlete_id,
                    )
                )
            ).mappings().one_or_none()
        return self._mapping_record(row)

    def get_active_mapping(
        self, provider: str, provider_athlete_id: str
    ) -> ProviderAthleteMappingRecord | None:
        mapping = self.get_mapping(provider, provider_athlete_id)
        return mapping if mapping is not None and mapping.is_active else None

    @_translate_storage_failures
    def get_rejection(
        self, provider: str, provider_athlete_id: str
    ) -> MappingRejectionRecord | None:
        provider, provider_athlete_id = _normalized_key(provider, provider_athlete_id)
        with self.engine.connect() as connection:
            row = connection.execute(
                select(AthleteMappingRejection.__table__).where(
                    and_(
                        AthleteMappingRejection.provider == provider,
                        AthleteMappingRejection.provider_athlete_id == provider_athlete_id,
                    )
                )
            ).mappings().one_or_none()
        return self._rejection_record(row)

    def is_rejected(self, provider: str, provider_athlete_id: str) -> bool:
        rejection = self.get_rejection(provider, provider_athlete_id)
        return bool(rejection and rejection.is_active)

    @_translate_storage_failures
    def list_mappings(
        self,
        *,
        provider: str | None = None,
        active_only: bool = False,
    ) -> list[ProviderAthleteMappingRecord]:
        statement = select(ProviderAthleteMapping.__table__)
        if provider is not None:
            normalized_provider, _ = _normalized_key(provider, "_placeholder")
            statement = statement.where(ProviderAthleteMapping.provider == normalized_provider)
        if active_only:
            statement = statement.where(ProviderAthleteMapping.is_active.is_(True))
        statement = statement.order_by(
            ProviderAthleteMapping.provider,
            ProviderAthleteMapping.provider_athlete_id,
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._mapping_record(row) for row in rows if row is not None]

    @_translate_storage_failures
    def list_rejections(
        self,
        *,
        provider: str | None = None,
        active_only: bool = True,
    ) -> list[MappingRejectionRecord]:
        statement = select(AthleteMappingRejection.__table__)
        if provider is not None:
            normalized_provider, _ = _normalized_key(provider, "_placeholder")
            statement = statement.where(AthleteMappingRejection.provider == normalized_provider)
        if active_only:
            statement = statement.where(AthleteMappingRejection.is_active.is_(True))
        statement = statement.order_by(
            AthleteMappingRejection.provider,
            AthleteMappingRejection.provider_athlete_id,
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._rejection_record(row) for row in rows if row is not None]

    @_translate_storage_failures
    def history(
        self,
        *,
        provider: str | None = None,
        provider_athlete_id: str | None = None,
        limit: int | None = None,
    ) -> list[MappingDecisionRecord]:
        statement = select(AthleteMappingDecision.__table__)
        if provider is not None:
            normalized_provider, _ = _normalized_key(provider, "_placeholder")
            statement = statement.where(AthleteMappingDecision.provider == normalized_provider)
        if provider_athlete_id is not None:
            _, normalized_id = _normalized_key("provider", provider_athlete_id)
            statement = statement.where(
                AthleteMappingDecision.provider_athlete_id == normalized_id
            )
        statement = statement.order_by(AthleteMappingDecision.id)
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ValueError("history limit must be a positive integer")
            statement = statement.limit(limit)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
            return self._decision_records(connection, rows)

    @_translate_storage_failures
    def list_unresolved(
        self,
        *,
        provider: str | None = None,
    ) -> list[MappingDecisionRecord]:
        """Return each identity whose latest decision is still unresolved.

        Ambiguous, inactive-only, unmatched, and team-conflict evidence never
        becomes current mapping state, so the durable audit rows are the only
        record an operator can act on.  The latest decision per identity is
        chosen first, so a later automatic, manual, or rejection decision
        removes the identity from the operator's queue.
        """

        unresolved = {state.value for state in UNRESOLVED_OBSERVATION_STATES}
        statement = select(AthleteMappingDecision.__table__)
        if provider is not None:
            normalized_provider, _ = _normalized_key(provider, "_placeholder")
            statement = statement.where(
                AthleteMappingDecision.provider == normalized_provider
            )
        statement = statement.order_by(AthleteMappingDecision.id)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
            latest: dict[tuple[str, str], Mapping[str, Any]] = {}
            for row in rows:
                latest[(row["provider"], row["provider_athlete_id"])] = row
            pending = [
                latest[key]
                for key in sorted(latest)
                if latest[key]["decision_state"] in unresolved
            ]
            return self._decision_records(connection, pending)

    # -- automatic board observations -------------------------------------

    def persist_auto_decision(
        self,
        resolution: AthleteResolution,
        *,
        observed_at: datetime | None = None,
    ) -> MappingPersistenceResult:
        """Persist the first qualifying automatic result transactionally.

        The unique source identity and idempotency key make repeated board
        reads harmless.  Existing manual decisions are never overwritten.
        """

        if not isinstance(resolution, AthleteResolution):
            raise TypeError("resolution must be AthleteResolution")
        if not resolution.is_auto_qualifying:
            if resolution.state is MappingResolutionState.MAPPING_CONFLICT:
                return self._persist_mapping_conflict(resolution, observed_at=observed_at)
            if resolution.state is MappingResolutionState.TEAM_CONFLICT:
                current = self.get_active_mapping(*self._resolution_key(resolution))
                if current is not None:
                    return self._persist_mapping_conflict(
                        resolution, observed_at=observed_at
                    )
            if resolution.state in UNRESOLVED_OBSERVATION_STATES:
                return self._persist_unresolved_observation(
                    resolution, observed_at=observed_at
                )
            return MappingPersistenceResult(resolution.state.value, False)
        provider, provider_id = self._resolution_key(resolution)
        now = _utc(observed_at or self._clock())
        values = self._resolution_values(resolution)
        fingerprint = self._idempotency_key(resolution)

        with self._transaction(provider, provider_id) as connection:
            rejection = self._select_rejection(connection, provider, provider_id)
            if rejection is not None and rejection["is_active"]:
                mapping = self._select_mapping(connection, provider, provider_id)
                return MappingPersistenceResult(
                    MappingResolutionState.REJECTED.value,
                    False,
                    mapping=self._mapping_record(mapping),
                )

            existing = self._select_mapping(connection, provider, provider_id, lock=True)
            if existing is not None:
                state = str(existing["mapping_state"])
                if state in {
                    MappingResolutionState.MANUAL_APPROVED.value,
                    MappingResolutionState.MANUAL_OVERRIDE.value,
                } and existing["is_active"]:
                    return MappingPersistenceResult(
                        state,
                        False,
                        mapping=self._mapping_record(existing),
                    )
                if state == MappingResolutionState.MAPPING_CONFLICT.value:
                    return MappingPersistenceResult(
                        state,
                        False,
                        mapping=self._mapping_record(existing),
                    )
                previous_id = existing["canonical_player_id"]
                if previous_id is not None and int(previous_id) != resolution.canonical_player_id:
                    return self._write_conflict(
                        connection,
                        existing,
                        resolution,
                        now=now,
                    )
                connection.execute(
                    update(ProviderAthleteMapping.__table__)
                    .where(
                        and_(
                            ProviderAthleteMapping.provider == provider,
                            ProviderAthleteMapping.provider_athlete_id == provider_id,
                        )
                    )
                    .values(
                        **values,
                        mapping_state=MappingResolutionState.AUTO.value,
                        is_active=True,
                        last_seen_at=now,
                    )
                )
            else:
                try:
                    with connection.begin_nested():
                        connection.execute(
                            insert(ProviderAthleteMapping.__table__).values(
                                **values,
                                provider=provider,
                                provider_athlete_id=provider_id,
                                mapping_state=MappingResolutionState.AUTO.value,
                                is_active=True,
                                first_seen_at=now,
                                last_seen_at=now,
                            )
                        )
                except IntegrityError:
                    # A concurrent board read won the identity race.  It is
                    # safe to continue because the unique identity is now
                    # readable in this transaction.
                    existing = self._select_mapping(connection, provider, provider_id)
                    if existing is None:
                        raise
                    if existing["mapping_state"] in {
                        MappingResolutionState.MANUAL_APPROVED.value,
                        MappingResolutionState.MANUAL_OVERRIDE.value,
                    }:
                        return MappingPersistenceResult(
                            str(existing["mapping_state"]),
                            False,
                            mapping=self._mapping_record(existing),
                        )

            decision = self._insert_decision(
                connection,
                resolution,
                now=now,
                idempotency_key=fingerprint,
            )
            mapping = self._select_mapping(connection, provider, provider_id)
            return MappingPersistenceResult(
                MappingResolutionState.AUTO.value,
                decision is not None,
                mapping=self._mapping_record(mapping),
                decision=self._decision_result(connection, decision),
            )

    def _persist_unresolved_observation(
        self,
        resolution: AthleteResolution,
        *,
        observed_at: datetime | None,
    ) -> MappingPersistenceResult:
        """Retain one typed unresolved observation in the audit log.

        No current mapping row is written because no canonical identity was
        established; the idempotency key keeps repeated board reads to one
        durable observation per distinct evidence shape.
        """

        provider, provider_id = self._resolution_key(resolution)
        now = _utc(observed_at or self._clock())
        with self._transaction(provider, provider_id) as connection:
            decision = self._insert_decision(
                connection,
                resolution,
                now=now,
                idempotency_key=self._idempotency_key(resolution),
            )
            return MappingPersistenceResult(
                resolution.state.value,
                decision is not None,
                decision=self._decision_result(connection, decision),
            )

    def record_resolution(
        self,
        resolution: AthleteResolution,
        *,
        observed_at: datetime | None = None,
    ) -> MappingPersistenceResult:
        """Persist a qualifying result or a later evidence conflict."""

        try:
            return self.persist_auto_decision(resolution, observed_at=observed_at)
        except SQLAlchemyError as exc:
            raise AthleteMappingPersistenceError(DEFAULT_MAPPING_FAILURE_SUMMARY) from exc

    def _persist_mapping_conflict(
        self,
        resolution: AthleteResolution,
        *,
        observed_at: datetime | None,
    ) -> MappingPersistenceResult:
        provider, provider_id = self._resolution_key(resolution)
        now = _utc(observed_at or self._clock())
        with self._transaction(provider, provider_id) as connection:
            existing = self._select_mapping(connection, provider, provider_id, lock=True)
            if existing is None:
                # Preserve a conflict observation even if the first source
                # observation was itself ambiguous or conflicting.
                values = self._resolution_values(resolution)
                connection.execute(
                    insert(ProviderAthleteMapping.__table__).values(
                        **values,
                        provider=provider,
                        provider_athlete_id=provider_id,
                        mapping_state=MappingResolutionState.MAPPING_CONFLICT.value,
                        is_active=False,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                existing = self._select_mapping(connection, provider, provider_id)
            result = self._write_conflict(connection, existing, resolution, now=now)
            return result

    def _write_conflict(
        self,
        connection: Connection,
        existing: Mapping[str, Any],
        resolution: AthleteResolution,
        *,
        now: datetime,
    ) -> MappingPersistenceResult:
        provider, provider_id = self._resolution_key(resolution)
        # The caller may have observed an older row.  Re-lock and re-read it
        # immediately before writing so a newer manual decision wins.
        current = self._select_mapping(connection, provider, provider_id, lock=True)
        if current is not None and current["is_active"] and current["mapping_state"] in {
            MappingResolutionState.MANUAL_APPROVED.value,
            MappingResolutionState.MANUAL_OVERRIDE.value,
        }:
            return MappingPersistenceResult(
                str(current["mapping_state"]), False, mapping=self._mapping_record(current)
            )
        existing = current or existing
        values = self._resolution_values(resolution)
        connection.execute(
            update(ProviderAthleteMapping.__table__)
            .where(
                and_(
                    ProviderAthleteMapping.provider == provider,
                    ProviderAthleteMapping.provider_athlete_id == provider_id,
                )
            )
            .values(
                provider_name=values["provider_name"],
                provider_team_id=values["provider_team_id"],
                provider_team_canonical_id=values["provider_team_canonical_id"],
                provider_team_name=values["provider_team_name"],
                provider_team_abbreviation=values["provider_team_abbreviation"],
                mapping_state=MappingResolutionState.MAPPING_CONFLICT.value,
                is_active=False,
                conflict_canonical_player_id=resolution.canonical_player_id,
                conflict_canonical_name=(
                    resolution.canonical_athlete.display_name
                    if resolution.canonical_athlete
                    else None
                ),
                last_seen_at=now,
            )
        )
        decision = self._insert_decision(
            connection,
            resolution,
            now=now,
            idempotency_key=self._idempotency_key(resolution),
            state=MappingResolutionState.MAPPING_CONFLICT.value,
            reason="mapping_conflict",
        )
        mapping = self._select_mapping(connection, provider, provider_id)
        return MappingPersistenceResult(
            MappingResolutionState.MAPPING_CONFLICT.value,
            decision is not None,
            mapping=self._mapping_record(mapping),
            decision=self._decision_result(connection, decision),
        )

    # -- operator actions --------------------------------------------------

    @_translate_storage_failures
    def approve(
        self,
        provider: str,
        provider_athlete_id: str,
        canonical_player_id: int,
        *,
        season: str | None = None,
        operator_id: str,
        reason: str,
        provider_evidence: AthleteEvidence | None = None,
    ) -> MappingPersistenceResult:
        return self._manual_map(
            MappingResolutionState.MANUAL_APPROVED,
            provider,
            provider_athlete_id,
            canonical_player_id,
            season=season,
            operator_id=operator_id,
            reason=reason,
            provider_evidence=provider_evidence,
        )

    @_translate_storage_failures
    def override(
        self,
        provider: str,
        provider_athlete_id: str,
        canonical_player_id: int,
        *,
        season: str | None = None,
        operator_id: str,
        reason: str,
        provider_evidence: AthleteEvidence | None = None,
    ) -> MappingPersistenceResult:
        return self._manual_map(
            MappingResolutionState.MANUAL_OVERRIDE,
            provider,
            provider_athlete_id,
            canonical_player_id,
            season=season,
            operator_id=operator_id,
            reason=reason,
            provider_evidence=provider_evidence,
        )

    @_translate_storage_failures
    def reject(
        self,
        provider: str,
        provider_athlete_id: str,
        *,
        operator_id: str,
        reason: str,
        season: str | None = None,
    ) -> MappingPersistenceResult:
        provider, provider_id = _normalized_key(provider, provider_athlete_id)
        operator_id = _operator(operator_id)
        reason = _reason(reason)
        requested_season = validate_canonical_season(season) if season else None
        now = _utc(self._clock())
        with self._transaction(provider, provider_id) as connection:
            existing_rejection = self._select_rejection(connection, provider, provider_id, lock=True)
            if existing_rejection is None:
                connection.execute(
                    insert(AthleteMappingRejection.__table__).values(
                        provider=provider,
                        provider_athlete_id=provider_id,
                        is_active=True,
                        reason=reason,
                        operator_id=operator_id,
                        created_at=now,
                    )
                )
            else:
                connection.execute(
                    update(AthleteMappingRejection.__table__)
                    .where(
                        and_(
                            AthleteMappingRejection.provider == provider,
                            AthleteMappingRejection.provider_athlete_id == provider_id,
                        )
                    )
                    .values(
                        is_active=True,
                        reason=reason,
                        operator_id=operator_id,
                        created_at=now,
                        cleared_at=None,
                        cleared_by=None,
                        clear_reason=None,
                    )
                )
            existing_mapping = self._select_mapping(connection, provider, provider_id, lock=True)
            if existing_mapping is not None:
                connection.execute(
                    update(ProviderAthleteMapping.__table__)
                    .where(
                        and_(
                            ProviderAthleteMapping.provider == provider,
                            ProviderAthleteMapping.provider_athlete_id == provider_id,
                        )
                    )
                    .values(
                        mapping_state=MappingResolutionState.REJECTED.value,
                        is_active=False,
                        last_seen_at=now,
                    )
                )
            decision = self._insert_manual_decision(
                connection,
                provider=provider,
                provider_id=provider_id,
                season=requested_season,
                state=MappingResolutionState.REJECTED.value,
                operator_id=operator_id,
                reason=reason,
                existing=existing_mapping,
                now=now,
            )
            mapping = self._select_mapping(connection, provider, provider_id)
            return MappingPersistenceResult(
                MappingResolutionState.REJECTED.value,
                True,
                mapping=self._mapping_record(mapping),
                decision=self._decision_result(connection, decision),
            )

    @_translate_storage_failures
    def clear_rejection(
        self,
        provider: str,
        provider_athlete_id: str,
        *,
        operator_id: str,
        reason: str,
    ) -> bool:
        provider, provider_id = _normalized_key(provider, provider_athlete_id)
        operator_id = _operator(operator_id)
        reason = _reason(reason)
        now = _utc(self._clock())
        with self._transaction(provider, provider_id) as connection:
            rejection = self._select_rejection(connection, provider, provider_id, lock=True)
            if rejection is None or not rejection["is_active"]:
                return False
            connection.execute(
                update(AthleteMappingRejection.__table__)
                .where(
                    and_(
                        AthleteMappingRejection.provider == provider,
                        AthleteMappingRejection.provider_athlete_id == provider_id,
                    )
                )
                .values(
                    is_active=False,
                    cleared_at=now,
                    cleared_by=operator_id,
                    clear_reason=reason,
                )
            )
            self._insert_decision_values(
                connection,
                provider=provider,
                provider_athlete_id=provider_id,
                requested_season=None,
                decision_state=MappingResolutionState.REJECTION_CLEARED.value,
                operator_id=operator_id,
                reason=reason,
                created_at=now,
            )
        return True

    def _manual_map(
        self,
        state: MappingResolutionState,
        provider: str,
        provider_athlete_id: str,
        canonical_player_id: int,
        *,
        season: str | None,
        operator_id: str,
        reason: str,
        provider_evidence: AthleteEvidence | None = None,
    ) -> MappingPersistenceResult:
        provider, provider_id = _normalized_key(provider, provider_athlete_id)
        operator_id = _operator(operator_id)
        reason = _reason(reason)
        if isinstance(canonical_player_id, bool) or not isinstance(canonical_player_id, int):
            raise ValueError("canonical player ID must be an integer")
        requested_season = validate_canonical_season(season) if season else None
        now = _utc(self._clock())
        with self._transaction(provider, provider_id) as connection:
            existing = self._select_mapping(connection, provider, provider_id, lock=True)
            if requested_season is None and existing is not None:
                requested_season = existing["season"]
            if requested_season is None:
                raise ValueError("season is required for a new manual mapping")
            canonical = self._select_canonical(
                connection,
                requested_season,
                canonical_player_id,
            )
            if canonical is None:
                raise ValueError("canonical athlete is not in the requested season")
            rejection = self._select_rejection(connection, provider, provider_id, lock=True)
            if rejection is not None and rejection["is_active"]:
                raise ValueError("clear the active rejection before approving this identity")
            evidence_values = self._evidence_values(provider_evidence) if provider_evidence else self._existing_evidence_values(existing)
            mapping_values = self._canonical_values(canonical)
            if existing is None:
                connection.execute(
                    insert(ProviderAthleteMapping.__table__).values(
                        provider=provider,
                        provider_athlete_id=provider_id,
                        mapping_state=state.value,
                        is_active=True,
                        season=requested_season,
                        **mapping_values,
                        **evidence_values,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            else:
                connection.execute(
                    update(ProviderAthleteMapping.__table__)
                    .where(
                        and_(
                            ProviderAthleteMapping.provider == provider,
                            ProviderAthleteMapping.provider_athlete_id == provider_id,
                        )
                    )
                    .values(
                        mapping_state=state.value,
                        is_active=True,
                        season=requested_season,
                        **mapping_values,
                        **evidence_values,
                        last_seen_at=now,
                        conflict_canonical_player_id=None,
                        conflict_canonical_name=None,
                    )
                )
            decision = self._insert_manual_decision(
                connection,
                provider=provider,
                provider_id=provider_id,
                season=requested_season,
                state=state.value,
                operator_id=operator_id,
                reason=reason,
                existing=existing,
                canonical=CanonicalAthlete.from_row(canonical),
                evidence_values=evidence_values,
                now=now,
            )
            mapping = self._select_mapping(connection, provider, provider_id)
            return MappingPersistenceResult(
                state.value,
                True,
                mapping=self._mapping_record(mapping),
                decision=self._decision_result(connection, decision),
            )

    # -- SQL helpers -------------------------------------------------------

    @staticmethod
    def _select_mapping(
        connection: Connection,
        provider: str,
        provider_id: str,
        *,
        lock: bool = False,
    ) -> Mapping[str, Any] | None:
        statement = select(ProviderAthleteMapping.__table__).where(
            and_(
                ProviderAthleteMapping.provider == provider,
                ProviderAthleteMapping.provider_athlete_id == provider_id,
            )
        )
        if lock:
            statement = statement.with_for_update()
        return connection.execute(statement).mappings().one_or_none()

    @staticmethod
    def _select_rejection(
        connection: Connection,
        provider: str,
        provider_id: str,
        *,
        lock: bool = False,
    ) -> Mapping[str, Any] | None:
        statement = select(AthleteMappingRejection.__table__).where(
            and_(
                AthleteMappingRejection.provider == provider,
                AthleteMappingRejection.provider_athlete_id == provider_id,
            )
        )
        if lock:
            statement = statement.with_for_update()
        return connection.execute(statement).mappings().one_or_none()

    @staticmethod
    def _select_canonical(
        connection: Connection,
        season: str,
        player_id: int,
    ) -> Mapping[str, Any] | None:
        return connection.execute(
            select(AthleteCatalog.__table__).where(
                and_(
                    AthleteCatalog.season == season,
                    AthleteCatalog.player_id == player_id,
                    AthleteCatalog.is_active_for_season.is_(True),
                )
            )
        ).mappings().one_or_none()

    def _insert_decision(
        self,
        connection: Connection,
        resolution: AthleteResolution,
        *,
        now: datetime,
        idempotency_key: str,
        state: str | None = None,
        reason: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Append one automatic decision or unresolved observation."""

        provider, provider_id = self._resolution_key(resolution)
        canonical = resolution.canonical_athlete
        decision_state = state or resolution.state.value
        values = self._resolution_values(resolution)
        values.pop("season", None)
        values.update(
            provider=provider,
            provider_athlete_id=provider_id,
            requested_season=resolution.season,
            decision_state=decision_state,
            canonical_player_id=(canonical.player_id if canonical else None),
            canonical_name=(canonical.display_name if canonical else None),
            canonical_team_id=(canonical.team_id if canonical else None),
            canonical_team_name=(canonical.team_name if canonical else None),
            canonical_team_abbreviation=(canonical.team_abbreviation if canonical else None),
            operator_id=None,
            reason=reason or resolution.reason,
            idempotency_key=idempotency_key,
            created_at=now,
        )
        decision = self._insert_decision_values(connection, **values)
        unresolved = {value.value for value in UNRESOLVED_OBSERVATION_STATES}
        if decision is not None and decision_state in unresolved:
            self._insert_candidates(
                connection, int(decision["id"]), resolution.candidates
            )
        return decision

    @staticmethod
    def _insert_candidates(
        connection: Connection,
        decision_id: int,
        candidates: tuple[CanonicalAthlete, ...],
    ) -> None:
        """Retain the candidates an operator has to choose between.

        Only observations that established no canonical identity carry
        candidates; a decided mapping already names its canonical athlete.
        """

        if not candidates:
            return
        connection.execute(
            insert(AthleteMappingDecisionCandidate.__table__),
            [
                {
                    "decision_id": decision_id,
                    "canonical_player_id": candidate.player_id,
                    "canonical_name": candidate.display_name,
                    "canonical_team_id": candidate.team_id,
                    "canonical_team_name": candidate.team_name,
                    "canonical_team_abbreviation": candidate.team_abbreviation,
                    "is_active_for_season": candidate.is_active_for_season,
                }
                for candidate in candidates
            ],
        )

    @staticmethod
    def _insert_decision_values(
        connection: Connection,
        *,
        provider: str,
        provider_athlete_id: str,
        requested_season: str | None,
        decision_state: str,
        canonical_player_id: int | None = None,
        canonical_name: str | None = None,
        canonical_team_id: int | None = None,
        canonical_team_name: str | None = None,
        canonical_team_abbreviation: str | None = None,
        provider_name: str | None = None,
        provider_team_id: str | None = None,
        provider_team_canonical_id: int | None = None,
        provider_team_name: str | None = None,
        provider_team_abbreviation: str | None = None,
        operator_id: str | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
        created_at: datetime,
    ) -> Mapping[str, Any] | None:
        values = {
            "provider": provider,
            "provider_athlete_id": provider_athlete_id,
            "requested_season": requested_season,
            "decision_state": decision_state,
            "canonical_player_id": canonical_player_id,
            "canonical_name": canonical_name,
            "canonical_team_id": canonical_team_id,
            "canonical_team_name": canonical_team_name,
            "canonical_team_abbreviation": canonical_team_abbreviation,
            "provider_name": provider_name,
            "provider_team_id": provider_team_id,
            "provider_team_canonical_id": provider_team_canonical_id,
            "provider_team_name": provider_team_name,
            "provider_team_abbreviation": provider_team_abbreviation,
            "operator_id": operator_id,
            "reason": reason,
            "idempotency_key": idempotency_key,
            "created_at": created_at,
        }
        try:
            with connection.begin_nested():
                result = connection.execute(
                    insert(AthleteMappingDecision.__table__).values(**values)
                )
        except IntegrityError:
            if idempotency_key is None:
                raise
            return None
        decision_id = result.inserted_primary_key[0]
        return connection.execute(
            select(AthleteMappingDecision.__table__).where(
                AthleteMappingDecision.id == decision_id
            )
        ).mappings().one()

    def _insert_manual_decision(
        self,
        connection: Connection,
        *,
        provider: str,
        provider_id: str,
        season: str | None,
        state: str,
        operator_id: str,
        reason: str,
        existing: Mapping[str, Any] | None,
        canonical: CanonicalAthlete | None = None,
        evidence_values: Mapping[str, Any] | None = None,
        now: datetime,
    ) -> Mapping[str, Any] | None:
        existing = existing or {}
        canonical = canonical or self._canonical_from_mapping(existing, season)
        return self._insert_decision_values(
            connection,
            provider=provider,
            provider_athlete_id=provider_id,
            requested_season=season,
            decision_state=state,
            canonical_player_id=(canonical.player_id if canonical else existing.get("canonical_player_id")),
            canonical_name=(canonical.display_name if canonical else existing.get("canonical_name")),
            canonical_team_id=(canonical.team_id if canonical else existing.get("canonical_team_id")),
            canonical_team_name=(canonical.team_name if canonical else existing.get("canonical_team_name")),
            canonical_team_abbreviation=(canonical.team_abbreviation if canonical else existing.get("canonical_team_abbreviation")),
            provider_name=(evidence_values or existing).get("provider_name"),
            provider_team_id=(evidence_values or existing).get("provider_team_id"),
            provider_team_canonical_id=(evidence_values or existing).get(
                "provider_team_canonical_id"
            ),
            provider_team_name=(evidence_values or existing).get("provider_team_name"),
            provider_team_abbreviation=(evidence_values or existing).get("provider_team_abbreviation"),
            operator_id=operator_id,
            reason=reason,
            created_at=now,
        )

    @staticmethod
    def _resolution_key(resolution: AthleteResolution) -> tuple[str, str]:
        if not resolution.provider_evidence.provider_id:
            raise ValueError("a persisted mapping requires provider athlete identity")
        return _normalized_key(resolution.provider, resolution.provider_evidence.provider_id)

    @staticmethod
    def _resolution_values(resolution: AthleteResolution) -> dict[str, Any]:
        evidence = resolution.provider_evidence
        team = evidence.team
        return {
            "season": resolution.season,
            "canonical_player_id": resolution.canonical_player_id,
            "canonical_name": (
                resolution.canonical_athlete.display_name
                if resolution.canonical_athlete
                else None
            ),
            "canonical_team_id": (
                resolution.canonical_athlete.team_id
                if resolution.canonical_athlete
                else None
            ),
            "canonical_team_name": (
                resolution.canonical_athlete.team_name
                if resolution.canonical_athlete
                else None
            ),
            "canonical_team_abbreviation": (
                resolution.canonical_athlete.team_abbreviation
                if resolution.canonical_athlete
                else None
            ),
            "provider_name": evidence.name,
            "provider_team_id": team.provider_id if team else None,
            "provider_team_canonical_id": team.canonical_id if team else None,
            "provider_team_name": team.name if team else None,
            "provider_team_abbreviation": team.abbreviation if team else None,
        }

    @staticmethod
    def _existing_evidence_values(existing: Mapping[str, Any] | None) -> dict[str, Any]:
        existing = existing or {}
        return {
            "provider_name": existing.get("provider_name"),
            "provider_team_id": existing.get("provider_team_id"),
            "provider_team_canonical_id": existing.get("provider_team_canonical_id"),
            "provider_team_name": existing.get("provider_team_name"),
            "provider_team_abbreviation": existing.get("provider_team_abbreviation"),
        }

    @staticmethod
    def _evidence_values(evidence: AthleteEvidence) -> dict[str, Any]:
        if not isinstance(evidence, AthleteEvidence):
            raise TypeError("provider_evidence must be AthleteEvidence")
        team = evidence.team
        return {
            "provider_name": evidence.name,
            "provider_team_id": team.provider_id if team else None,
            "provider_team_canonical_id": team.canonical_id if team else None,
            "provider_team_name": team.name if team else None,
            "provider_team_abbreviation": team.abbreviation if team else None,
        }

    @staticmethod
    def _canonical_values(canonical: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "canonical_player_id": int(canonical["player_id"]),
            "canonical_name": canonical.get("display_name"),
            "canonical_team_id": canonical.get("team_id"),
            "canonical_team_name": canonical.get("team_name"),
            "canonical_team_abbreviation": canonical.get("team_abbreviation"),
        }

    @staticmethod
    def _canonical_from_mapping(
        mapping: Mapping[str, Any], season: str | None
    ) -> CanonicalAthlete | None:
        player_id = mapping.get("canonical_player_id")
        if player_id is None:
            return None
        return CanonicalAthlete(
            season=str(mapping.get("season") or season or ""),
            player_id=int(player_id),
            display_name=str(mapping.get("canonical_name") or ""),
            roster_status="active",
            is_active=True,
            is_active_for_season=True,
            team_id=(
                None
                if mapping.get("canonical_team_id") is None
                else int(mapping["canonical_team_id"])
            ),
            team_name=mapping.get("canonical_team_name"),
            team_abbreviation=mapping.get("canonical_team_abbreviation"),
        )

    @staticmethod
    def _idempotency_key(resolution: AthleteResolution) -> str:
        evidence = resolution.provider_evidence
        team = evidence.team
        payload = {
            "provider": resolution.provider,
            "provider_athlete_id": evidence.provider_id,
            "season": resolution.season,
            "state": resolution.state.value,
            "canonical_player_id": resolution.canonical_player_id,
            "provider_name": evidence.name,
            "provider_team_id": team.provider_id if team else None,
            "provider_team_canonical_id": team.canonical_id if team else None,
            "provider_team_name": team.name if team else None,
            "provider_team_abbreviation": team.abbreviation if team else None,
            # A changed candidate set is different evidence for an operator to
            # review, so it must not be suppressed as a repeated observation.
            "candidates": sorted(
                candidate.player_id for candidate in resolution.candidates
            ),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _mapping_record(row: Mapping[str, Any] | None) -> ProviderAthleteMappingRecord | None:
        if row is None:
            return None
        return ProviderAthleteMappingRecord(
            provider=row["provider"],
            provider_athlete_id=row["provider_athlete_id"],
            mapping_state=row["mapping_state"],
            is_active=bool(row["is_active"]),
            season=row["season"],
            canonical_player_id=row["canonical_player_id"],
            canonical_name=row["canonical_name"],
            canonical_team_id=row["canonical_team_id"],
            canonical_team_name=row["canonical_team_name"],
            canonical_team_abbreviation=row["canonical_team_abbreviation"],
            provider_name=row["provider_name"],
            provider_team_id=row["provider_team_id"],
            provider_team_canonical_id=row["provider_team_canonical_id"],
            provider_team_name=row["provider_team_name"],
            provider_team_abbreviation=row["provider_team_abbreviation"],
            conflict_canonical_player_id=row["conflict_canonical_player_id"],
            conflict_canonical_name=row["conflict_canonical_name"],
            first_seen_at=_iso(row["first_seen_at"]) or "",
            last_seen_at=_iso(row["last_seen_at"]) or "",
        )

    @classmethod
    def _decision_result(
        cls,
        connection: Connection,
        row: Mapping[str, Any] | None,
    ) -> MappingDecisionRecord | None:
        """Build one decision record with its durable candidates."""

        if row is None:
            return None
        candidates = cls._select_candidates(connection, [int(row["id"])])
        return cls._decision_record(row, candidates.get(int(row["id"]), ()))

    @classmethod
    def _decision_records(
        cls,
        connection: Connection,
        rows: Any,
    ) -> list[MappingDecisionRecord]:
        rows = [row for row in rows if row is not None]
        candidates = cls._select_candidates(connection, [int(row["id"]) for row in rows])
        return [
            cls._decision_record(row, candidates.get(int(row["id"]), ())) for row in rows
        ]

    @staticmethod
    def _select_candidates(
        connection: Connection,
        decision_ids: list[int],
    ) -> dict[int, tuple[MappingCandidateRecord, ...]]:
        if not decision_ids:
            return {}
        rows = connection.execute(
            select(AthleteMappingDecisionCandidate.__table__)
            .where(AthleteMappingDecisionCandidate.decision_id.in_(decision_ids))
            .order_by(
                AthleteMappingDecisionCandidate.decision_id,
                AthleteMappingDecisionCandidate.canonical_player_id,
            )
        ).mappings().all()
        candidates: dict[int, tuple[MappingCandidateRecord, ...]] = {}
        for row in rows:
            decision_id = int(row["decision_id"])
            candidates[decision_id] = candidates.get(decision_id, ()) + (
                MappingCandidateRecord(
                    canonical_player_id=int(row["canonical_player_id"]),
                    canonical_name=row["canonical_name"],
                    canonical_team_id=row["canonical_team_id"],
                    canonical_team_name=row["canonical_team_name"],
                    canonical_team_abbreviation=row["canonical_team_abbreviation"],
                    is_active_for_season=bool(row["is_active_for_season"]),
                ),
            )
        return candidates

    @staticmethod
    def _decision_record(
        row: Mapping[str, Any] | None,
        candidates: tuple[MappingCandidateRecord, ...] = (),
    ) -> MappingDecisionRecord | None:
        if row is None:
            return None
        return MappingDecisionRecord(
            id=int(row["id"]),
            provider=row["provider"],
            provider_athlete_id=row["provider_athlete_id"],
            requested_season=row["requested_season"],
            decision_state=row["decision_state"],
            canonical_player_id=row["canonical_player_id"],
            canonical_name=row["canonical_name"],
            canonical_team_id=row["canonical_team_id"],
            canonical_team_name=row["canonical_team_name"],
            canonical_team_abbreviation=row["canonical_team_abbreviation"],
            provider_name=row["provider_name"],
            provider_team_id=row["provider_team_id"],
            provider_team_canonical_id=row["provider_team_canonical_id"],
            provider_team_name=row["provider_team_name"],
            provider_team_abbreviation=row["provider_team_abbreviation"],
            operator_id=row["operator_id"],
            reason=row["reason"],
            created_at=_iso(row["created_at"]) or "",
            candidates=candidates,
        )

    @staticmethod
    def _rejection_record(row: Mapping[str, Any] | None) -> MappingRejectionRecord | None:
        if row is None:
            return None
        return MappingRejectionRecord(
            provider=row["provider"],
            provider_athlete_id=row["provider_athlete_id"],
            is_active=bool(row["is_active"]),
            reason=row["reason"],
            operator_id=row["operator_id"],
            created_at=_iso(row["created_at"]) or "",
            cleared_at=_iso(row["cleared_at"]),
            cleared_by=row["cleared_by"],
            clear_reason=row["clear_reason"],
        )


__all__ = [
    "UNRESOLVED_OBSERVATION_STATES",
    "AthleteMappingRepository",
    "AthleteMappingPersistenceError",
    "MappingCandidateRecord",
    "MappingDecisionRecord",
    "MappingPersistenceResult",
    "MappingRejectionRecord",
    "ProviderAthleteMappingRecord",
]
