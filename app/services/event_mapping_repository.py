"""Transactional persistence for provider event mapping decisions.

Governance is the one established for provider athletes: a single current row
per provider identity, an append-only decision log, durable rejections, typed
candidates an operator has to choose between, per-identity serialization, and an
observation clock that fences a read taken before the decision that governs the
identity.  The evidence and the canonical side are events rather than athletes,
so the columns and the matching rules differ; the rules about who may change
what do not.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from contextlib import contextmanager
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.domain.nba_events import is_postponed_event
from app.domain.utc import assume_utc
from app.errors import InvalidConfigurationError
from app.models.event_catalog import EventCatalogEntry
from app.models.event_mapping import (
    EventMappingDecision,
    EventMappingDecisionCandidate,
    EventMappingDecisionContradiction,
    EventMappingLock,
    EventMappingRejection,
    ProviderEventMapping,
)
from app.providers.dfs import EventEvidence
from app.providers.nba_stats import validate_canonical_season
from app.services.event_mapping_errors import (
    DEFAULT_EVENT_MAPPING_FAILURE_SUMMARY,
    EventMappingPersistenceError,
)
from app.services.event_resolver import (
    CLAIMING_EVENT_MAPPING_STATES,
    WITHDRAWN_EVENT_CLAIM_STATES,
    CanonicalEvent,
    EventResolution,
    EventResolutionState,
    EventResolver,
    event_match_window,
    stored_timestamp,
)
from app.utils.db import is_demo_database_url


LOGGER = logging.getLogger(__name__)


#: Unresolved outcomes retained as durable, typed observations.  They never
#: establish a canonical identity, because no single scheduled game was
#: identified, and each also withdraws a claim the board established: an
#: identity may not stay comparable while the evidence cannot say which game it
#: is.
UNRESOLVED_EVENT_OBSERVATION_STATES = frozenset(
    {
        EventResolutionState.AMBIGUOUS,
        EventResolutionState.UNMATCHED,
        EventResolutionState.REPLACEMENT_PENDING,
    }
)

#: Decision states whose candidates an operator still has to choose between.
_CANDIDATE_DECISION_STATES = frozenset(
    {state.value for state in UNRESOLVED_EVENT_OBSERVATION_STATES}
    | {EventResolutionState.MAPPING_CONFLICT.value}
)

#: Mapping states an operator established and a board observation may not
#: supersede.
_MANUAL_MAPPING_STATES = frozenset(
    {
        EventResolutionState.MANUAL_APPROVED.value,
        EventResolutionState.MANUAL_OVERRIDE.value,
    }
)

#: Decision states an operator recorded.  Each is its own event, so its
#: ``created_at`` is when the identity was decided and is the instant a board
#: observation has to postdate to still be news.
_OPERATOR_DECISION_STATES = frozenset(
    _MANUAL_MAPPING_STATES
    | {
        EventResolutionState.REJECTED.value,
        EventResolutionState.REJECTION_CLEARED.value,
    }
)

#: Decision states whose provider evidence is a direct board observation.
#: Operator decisions only copy evidence forward, and a mapping conflict records
#: the contradicting side, so neither may be read as what the provider reports.
_OBSERVED_EVIDENCE_DECISION_STATES = frozenset(
    {state.value for state in UNRESOLVED_EVENT_OBSERVATION_STATES}
    | {EventResolutionState.AUTO.value}
)

#: Decision states whose evidence was written onto the mapping row itself.  The
#: newest of these is what the row currently reports, so it is the marker a
#: later board observation has to beat.
_MAPPING_EVIDENCE_DECISION_STATES = frozenset(
    _MANUAL_MAPPING_STATES
    | {
        EventResolutionState.AUTO.value,
        EventResolutionState.MAPPING_CONFLICT.value,
    }
)

#: The typed provider evidence columns shared by the mapping and decision rows.
_PROVIDER_EVIDENCE_COLUMNS = (
    "provider_canonical_event_id",
    "provider_event_label",
    "provider_starts_at",
    "provider_status_label",
    "provider_home_team_id",
    "provider_home_team_canonical_id",
    "provider_home_team_name",
    "provider_home_team_abbreviation",
    "provider_away_team_id",
    "provider_away_team_canonical_id",
    "provider_away_team_name",
    "provider_away_team_abbreviation",
)

#: The typed columns one contradicting provider evidence is retained in.  The
#: end and update instants are here but not on the decision row: the decision
#: keeps one representative evidence, while a contradiction is everything the
#: markets disagreed over.
_CONTRADICTION_COLUMNS = (
    "provider_event_id",
    "provider_canonical_event_id",
    "provider_event_label",
    "provider_starts_at",
    "provider_ends_at",
    "provider_updated_at",
    "provider_status_label",
    "provider_home_team_id",
    "provider_home_team_canonical_id",
    "provider_home_team_name",
    "provider_home_team_abbreviation",
    "provider_away_team_id",
    "provider_away_team_canonical_id",
    "provider_away_team_name",
    "provider_away_team_abbreviation",
)

#: Contradiction columns a database returns without a time zone.
_CONTRADICTION_INSTANT_COLUMNS = frozenset(
    {"provider_starts_at", "provider_ends_at", "provider_updated_at"}
)

#: The canonical NBA game columns shared by the mapping and decision rows.
_CANONICAL_EVENT_COLUMNS = (
    "canonical_event_id",
    "canonical_scheduled_at",
    "canonical_home_team_id",
    "canonical_home_team_name",
    "canonical_home_team_tricode",
    "canonical_away_team_id",
    "canonical_away_team_name",
    "canonical_away_team_tricode",
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
            raise EventMappingPersistenceError(
                DEFAULT_EVENT_MAPPING_FAILURE_SUMMARY
            ) from exc

    return wrapper


def _utc(value: datetime | None = None) -> datetime:
    return assume_utc(value or datetime.now(timezone.utc))


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def _normalized_key(provider: str, provider_event_id: str) -> tuple[str, str]:
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be a non-empty string")
    if not isinstance(provider_event_id, str) or not provider_event_id.strip():
        raise ValueError("provider event ID must be a non-empty string")
    return provider.strip().casefold(), provider_event_id.strip()


def _normalized_keys(
    provider: str, provider_event_ids: Iterable[str]
) -> tuple[str, tuple[str, ...]]:
    """Normalize one provider and every identity it names, without duplicates."""

    normalized_provider, _ = _normalized_key(provider, "_placeholder")
    ordered: dict[str, None] = {}
    for provider_event_id in provider_event_ids:
        _, normalized_id = _normalized_key(provider, provider_event_id)
        ordered[normalized_id] = None
    return normalized_provider, tuple(ordered)


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


def _canonical_event_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("canonical NBA game ID must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class ProviderEventMappingRecord:
    """Current mapping state for one provider event identity."""

    provider: str
    provider_event_id: str
    mapping_state: str
    is_active: bool
    season: str | None
    canonical_event_id: str | None
    canonical_scheduled_at: str | None
    canonical_home_team_id: int | None
    canonical_home_team_name: str | None
    canonical_home_team_tricode: str | None
    canonical_away_team_id: int | None
    canonical_away_team_name: str | None
    canonical_away_team_tricode: str | None
    provider_canonical_event_id: str | None
    provider_event_label: str | None
    provider_starts_at: str | None
    provider_status_label: str | None
    provider_home_team_id: str | None
    provider_home_team_canonical_id: int | None
    provider_home_team_name: str | None
    provider_home_team_abbreviation: str | None
    provider_away_team_id: str | None
    provider_away_team_canonical_id: int | None
    provider_away_team_name: str | None
    provider_away_team_abbreviation: str | None
    conflict_canonical_event_id: str | None
    first_seen_at: str
    last_seen_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_event_id": self.provider_event_id,
            "mapping_state": self.mapping_state,
            "is_active": self.is_active,
            "season": self.season,
            "canonical_event_id": self.canonical_event_id,
            "canonical_scheduled_at": self.canonical_scheduled_at,
            "canonical_home_team_id": self.canonical_home_team_id,
            "canonical_home_team_name": self.canonical_home_team_name,
            "canonical_home_team_tricode": self.canonical_home_team_tricode,
            "canonical_away_team_id": self.canonical_away_team_id,
            "canonical_away_team_name": self.canonical_away_team_name,
            "canonical_away_team_tricode": self.canonical_away_team_tricode,
            "provider_canonical_event_id": self.provider_canonical_event_id,
            "provider_event_label": self.provider_event_label,
            "provider_starts_at": self.provider_starts_at,
            "provider_status_label": self.provider_status_label,
            "provider_home_team_id": self.provider_home_team_id,
            "provider_home_team_canonical_id": self.provider_home_team_canonical_id,
            "provider_home_team_name": self.provider_home_team_name,
            "provider_home_team_abbreviation": self.provider_home_team_abbreviation,
            "provider_away_team_id": self.provider_away_team_id,
            "provider_away_team_canonical_id": self.provider_away_team_canonical_id,
            "provider_away_team_name": self.provider_away_team_name,
            "provider_away_team_abbreviation": self.provider_away_team_abbreviation,
            "conflict_canonical_event_id": self.conflict_canonical_event_id,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
        }


@dataclass(frozen=True, slots=True)
class EventMappingCandidateRecord:
    """One canonical NBA game an unresolved observation could not choose."""

    canonical_event_id: str
    scheduled_at: str | None
    home_team_id: int | None
    home_team_name: str | None
    home_team_tricode: str | None
    away_team_id: int | None
    away_team_name: str | None
    away_team_tricode: str | None
    start_offset_seconds: int | None
    is_postponed: bool
    is_scheduled: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_event_id": self.canonical_event_id,
            "scheduled_at": self.scheduled_at,
            "home_team_id": self.home_team_id,
            "home_team_name": self.home_team_name,
            "home_team_tricode": self.home_team_tricode,
            "away_team_id": self.away_team_id,
            "away_team_name": self.away_team_name,
            "away_team_tricode": self.away_team_tricode,
            "start_offset_seconds": self.start_offset_seconds,
            "is_postponed": self.is_postponed,
            "is_scheduled": self.is_scheduled,
        }


@dataclass(frozen=True, slots=True)
class EventMappingEvidenceRecord:
    """One typed provider event evidence a contradictory observation asserted."""

    provider_event_id: str | None
    provider_canonical_event_id: str | None
    provider_event_label: str | None
    provider_starts_at: str | None
    provider_ends_at: str | None
    provider_updated_at: str | None
    provider_status_label: str | None
    provider_home_team_id: str | None
    provider_home_team_canonical_id: int | None
    provider_home_team_name: str | None
    provider_home_team_abbreviation: str | None
    provider_away_team_id: str | None
    provider_away_team_canonical_id: int | None
    provider_away_team_name: str | None
    provider_away_team_abbreviation: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_event_id": self.provider_event_id,
            "provider_canonical_event_id": self.provider_canonical_event_id,
            "provider_event_label": self.provider_event_label,
            "provider_starts_at": self.provider_starts_at,
            "provider_ends_at": self.provider_ends_at,
            "provider_updated_at": self.provider_updated_at,
            "provider_status_label": self.provider_status_label,
            "provider_home_team_id": self.provider_home_team_id,
            "provider_home_team_canonical_id": self.provider_home_team_canonical_id,
            "provider_home_team_name": self.provider_home_team_name,
            "provider_home_team_abbreviation": self.provider_home_team_abbreviation,
            "provider_away_team_id": self.provider_away_team_id,
            "provider_away_team_canonical_id": self.provider_away_team_canonical_id,
            "provider_away_team_name": self.provider_away_team_name,
            "provider_away_team_abbreviation": self.provider_away_team_abbreviation,
        }


@dataclass(frozen=True, slots=True)
class EventMappingDecisionRecord:
    """One append-only decision or durable unresolved observation."""

    id: int
    provider: str
    provider_event_id: str
    requested_season: str | None
    decision_state: str
    canonical_event_id: str | None
    canonical_scheduled_at: str | None
    canonical_home_team_id: int | None
    canonical_home_team_name: str | None
    canonical_home_team_tricode: str | None
    canonical_away_team_id: int | None
    canonical_away_team_name: str | None
    canonical_away_team_tricode: str | None
    provider_canonical_event_id: str | None
    provider_event_label: str | None
    provider_starts_at: str | None
    provider_status_label: str | None
    provider_home_team_id: str | None
    provider_home_team_canonical_id: int | None
    provider_home_team_name: str | None
    provider_home_team_abbreviation: str | None
    provider_away_team_id: str | None
    provider_away_team_canonical_id: int | None
    provider_away_team_name: str | None
    provider_away_team_abbreviation: str | None
    operator_id: str | None
    reason: str | None
    observed_at: str | None
    created_at: str
    candidates: tuple[EventMappingCandidateRecord, ...] = ()
    #: Every typed evidence a fail-closed observation contradicted itself over.
    #: The decision itself carries only the representative one.
    contradictory_evidence: tuple[EventMappingEvidenceRecord, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "provider_event_id": self.provider_event_id,
            "requested_season": self.requested_season,
            "decision_state": self.decision_state,
            "canonical_event_id": self.canonical_event_id,
            "canonical_scheduled_at": self.canonical_scheduled_at,
            "canonical_home_team_id": self.canonical_home_team_id,
            "canonical_home_team_name": self.canonical_home_team_name,
            "canonical_home_team_tricode": self.canonical_home_team_tricode,
            "canonical_away_team_id": self.canonical_away_team_id,
            "canonical_away_team_name": self.canonical_away_team_name,
            "canonical_away_team_tricode": self.canonical_away_team_tricode,
            "provider_canonical_event_id": self.provider_canonical_event_id,
            "provider_event_label": self.provider_event_label,
            "provider_starts_at": self.provider_starts_at,
            "provider_status_label": self.provider_status_label,
            "provider_home_team_id": self.provider_home_team_id,
            "provider_home_team_canonical_id": self.provider_home_team_canonical_id,
            "provider_home_team_name": self.provider_home_team_name,
            "provider_home_team_abbreviation": self.provider_home_team_abbreviation,
            "provider_away_team_id": self.provider_away_team_id,
            "provider_away_team_canonical_id": self.provider_away_team_canonical_id,
            "provider_away_team_name": self.provider_away_team_name,
            "provider_away_team_abbreviation": self.provider_away_team_abbreviation,
            "operator_id": self.operator_id,
            "reason": self.reason,
            "observed_at": self.observed_at,
            "created_at": self.created_at,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "contradictory_evidence": [
                evidence.to_dict() for evidence in self.contradictory_evidence
            ],
        }


@dataclass(frozen=True, slots=True)
class EventMappingConflictRecord:
    """One identity whose current state is an unreviewed mapping conflict.

    The current row carries the provider identity, its observed evidence, the
    canonical game that was established or approved, and the conflicting one;
    the decision that recorded the conflict carries the reason, the candidates,
    and the time an operator has to reason from.
    """

    mapping: ProviderEventMappingRecord
    latest_decision: EventMappingDecisionRecord | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping": self.mapping.to_dict(),
            "latest_decision": (
                None if self.latest_decision is None else self.latest_decision.to_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class EventMappingRejectionRecord:
    """Durable suppression state for one provider event identity."""

    provider: str
    provider_event_id: str
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
            "provider_event_id": self.provider_event_id,
            "is_active": self.is_active,
            "reason": self.reason,
            "operator_id": self.operator_id,
            "created_at": self.created_at,
            "cleared_at": self.cleared_at,
            "cleared_by": self.cleared_by,
            "clear_reason": self.clear_reason,
        }


@dataclass(frozen=True, slots=True)
class BoardEventMappingOutcome:
    """What one board observation turned out to mean for the identity.

    The board resolves each market before the mapping transaction opens, so the
    state the resolver computed is only a proposal: an operator can reject,
    approve, or override the identity in between, and a concurrent observation
    can promote it to a conflict.  This value keeps both sides -- the
    ``resolution`` the board observed and the ``state`` governance reached -- so
    a caller never has to re-read the repository and therefore cannot race the
    write it just made.
    """

    resolution: EventResolution
    state: EventResolutionState
    persisted: bool
    mapping: ProviderEventMappingRecord | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.resolution, EventResolution):
            raise ValueError("board event outcome requires an EventResolution")
        state = (
            self.state
            if isinstance(self.state, EventResolutionState)
            else EventResolutionState(self.state)
        )
        if self.mapping is not None and state not in CLAIMING_EVENT_MAPPING_STATES:
            raise ValueError("a fail-closed event outcome may not carry a mapping")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "persisted", bool(self.persisted))

    @property
    def observed_state(self) -> EventResolutionState:
        """What the board resolved before governance had its say."""

        return self.resolution.state

    @property
    def canonical_event_id(self) -> str | None:
        """The canonical NBA game this identity may be compared as.

        A durable identity supplies it only through the active governed mapping,
        so a suppressed, disputed, or withdrawn identity cannot reach a
        comparison through the game the pre-transaction resolution named.  An
        identity the provider gave no stable event ID for has no durable row at
        all, and its claiming resolution is the only thing that can answer for
        the current board -- it is reevaluated on the next read rather than
        remembered.
        """

        if not self.resolution.is_durable:
            if self.state in CLAIMING_EVENT_MAPPING_STATES:
                return self.resolution.canonical_event_id
            return None
        if self.mapping is None or self.state not in CLAIMING_EVENT_MAPPING_STATES:
            return None
        return self.mapping.canonical_event_id


@dataclass(frozen=True, slots=True)
class EventMappingPersistenceResult:
    """Outcome of one automatic observation or manual decision."""

    state: str
    persisted: bool
    mapping: ProviderEventMappingRecord | None = None
    decision: EventMappingDecisionRecord | None = None

    def board_outcome(self, resolution: EventResolution) -> BoardEventMappingOutcome:
        """Restate one resolution as the governed state this write reached."""

        state = EventResolutionState(self.state)
        return BoardEventMappingOutcome(
            resolution=resolution,
            state=state,
            persisted=self.persisted,
            mapping=self._claimed_mapping(state),
        )

    def _claimed_mapping(
        self, state: EventResolutionState
    ) -> ProviderEventMappingRecord | None:
        """The stored mapping, but only while it still asserts this claim.

        A governing read returns the row it found alongside the state that now
        holds, and those disagree exactly when the identity failed closed: a
        rejection leaves the deactivated row it suppressed, and a conflict
        leaves the game the mapping used to name.  Handing that row on would let
        a caller compare against a game governance has already taken out of
        comparisons, so it is withheld unless the row is active and says the
        same thing the governed state does.
        """

        mapping = self.mapping
        if mapping is None or state not in CLAIMING_EVENT_MAPPING_STATES:
            return None
        if not mapping.is_active or mapping.mapping_state != state.value:
            return None
        return mapping


class EventMappingRepository:
    """Own durable current state, append-only decisions, and suppressions."""

    _identity_locks: dict[tuple[int, str, str], threading.RLock] = {}
    _identity_locks_guard = threading.Lock()

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Any | None = None,
        match_window: timedelta | float | None = None,
        settings: Any | None = None,
        projection_replay: Any | None = None,
    ) -> None:
        self.engine = engine
        if is_demo_database_url(str(getattr(engine, "url", ""))):
            raise InvalidConfigurationError(
                "The bundled demo database is read-only and cannot store event mappings."
            )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._projection_replay = projection_replay
        # The window a governed decision is rechecked against is deployment
        # policy, so it is resolved once here -- at the composition point that
        # already configures the resolver -- and used by every in-lock recheck.
        # Re-reading settings inside the transaction would let a configuration
        # change land mid-decision; falling back to the reviewed default would
        # accept evidence a narrower configuration has already called a
        # different fixture.
        self.match_window = event_match_window(match_window, settings)

    def set_projection_replay(self, projection_replay: Any | None) -> None:
        """Attach the database-only projection replay callback after assembly."""

        self._projection_replay = projection_replay

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
                        insert(EventMappingLock.__table__).values(
                            provider=provider, provider_event_id=provider_id
                        )
                    )
            except IntegrityError:
                pass
            connection.execute(
                select(EventMappingLock.__table__)
                .where(
                    and_(
                        EventMappingLock.provider == provider,
                        EventMappingLock.provider_event_id == provider_id,
                    )
                )
                .with_for_update()
            ).one()
            yield connection

    # -- reads -------------------------------------------------------------

    @_translate_storage_failures
    def get_mapping(
        self, provider: str, provider_event_id: str
    ) -> ProviderEventMappingRecord | None:
        provider, provider_event_id = _normalized_key(provider, provider_event_id)
        with self.engine.connect() as connection:
            row = self._select_mapping(connection, provider, provider_event_id)
        return self._mapping_record(row)

    def get_active_mapping(
        self, provider: str, provider_event_id: str
    ) -> ProviderEventMappingRecord | None:
        mapping = self.get_mapping(provider, provider_event_id)
        return mapping if mapping is not None and mapping.is_active else None

    @_translate_storage_failures
    def get_rejection(
        self, provider: str, provider_event_id: str
    ) -> EventMappingRejectionRecord | None:
        provider, provider_event_id = _normalized_key(provider, provider_event_id)
        with self.engine.connect() as connection:
            row = self._select_rejection(connection, provider, provider_event_id)
        return self._rejection_record(row)

    @_translate_storage_failures
    def get_mappings(
        self, provider: str, provider_event_ids: Iterable[str]
    ) -> dict[str, ProviderEventMappingRecord]:
        """Return the stored mapping of every named identity, keyed by ID.

        One board names many fixtures, so it reads them in one statement
        rather than one per market.  An identity with no row is absent.
        """

        provider, provider_event_ids = _normalized_keys(provider, provider_event_ids)
        if not provider_event_ids:
            return {}
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(ProviderEventMapping.__table__).where(
                    and_(
                        ProviderEventMapping.provider == provider,
                        ProviderEventMapping.provider_event_id.in_(provider_event_ids),
                    )
                )
            ).mappings().all()
        return {str(row["provider_event_id"]): self._mapping_record(row) for row in rows}

    @_translate_storage_failures
    def get_rejections(
        self, provider: str, provider_event_ids: Iterable[str]
    ) -> dict[str, EventMappingRejectionRecord]:
        """Return the stored rejection of every named identity, keyed by ID."""

        provider, provider_event_ids = _normalized_keys(provider, provider_event_ids)
        if not provider_event_ids:
            return {}
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(EventMappingRejection.__table__).where(
                    and_(
                        EventMappingRejection.provider == provider,
                        EventMappingRejection.provider_event_id.in_(provider_event_ids),
                    )
                )
            ).mappings().all()
        return {str(row["provider_event_id"]): self._rejection_record(row) for row in rows}

    def is_rejected(self, provider: str, provider_event_id: str) -> bool:
        rejection = self.get_rejection(provider, provider_event_id)
        return bool(rejection and rejection.is_active)

    @_translate_storage_failures
    def list_mappings(
        self,
        *,
        provider: str | None = None,
        active_only: bool = False,
    ) -> list[ProviderEventMappingRecord]:
        """Return mapping rows, never those whose current state is a conflict.

        A conflict is inactive, so the active-only listing already omits it, but
        the full listing would otherwise name the same identity twice: once here
        without its evidence and once in ``list_conflicts`` with both canonical
        sides.  The conflict queue elaborates it exactly once, so the row is
        excluded here while every other inactive row stays visible.
        """

        statement = select(ProviderEventMapping.__table__).where(
            ProviderEventMapping.mapping_state
            != EventResolutionState.MAPPING_CONFLICT.value
        )
        if provider is not None:
            normalized_provider, _ = _normalized_key(provider, "_placeholder")
            statement = statement.where(
                ProviderEventMapping.provider == normalized_provider
            )
        if active_only:
            statement = statement.where(ProviderEventMapping.is_active.is_(True))
        statement = statement.order_by(
            ProviderEventMapping.provider, ProviderEventMapping.provider_event_id
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
    ) -> list[EventMappingRejectionRecord]:
        statement = select(EventMappingRejection.__table__)
        if provider is not None:
            normalized_provider, _ = _normalized_key(provider, "_placeholder")
            statement = statement.where(
                EventMappingRejection.provider == normalized_provider
            )
        if active_only:
            statement = statement.where(EventMappingRejection.is_active.is_(True))
        statement = statement.order_by(
            EventMappingRejection.provider, EventMappingRejection.provider_event_id
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._rejection_record(row) for row in rows if row is not None]

    @_translate_storage_failures
    def history(
        self,
        *,
        provider: str | None = None,
        provider_event_id: str | None = None,
        limit: int | None = None,
    ) -> list[EventMappingDecisionRecord]:
        statement = select(EventMappingDecision.__table__)
        if provider is not None:
            normalized_provider, _ = _normalized_key(provider, "_placeholder")
            statement = statement.where(
                EventMappingDecision.provider == normalized_provider
            )
        if provider_event_id is not None:
            _, normalized_id = _normalized_key("provider", provider_event_id)
            statement = statement.where(
                EventMappingDecision.provider_event_id == normalized_id
            )
        statement = statement.order_by(EventMappingDecision.id)
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
    ) -> list[EventMappingDecisionRecord]:
        """Return each identity whose latest decision is still unresolved.

        Ambiguous, unmatched, and pending-replacement evidence never becomes an
        active mapping, so the durable audit rows are the only record an
        operator can act on.  The latest decision per identity is chosen first,
        so a later automatic, manual, or rejection decision removes the identity
        from the operator's queue.
        """

        unresolved = {state.value for state in UNRESOLVED_EVENT_OBSERVATION_STATES}
        statement = select(EventMappingDecision.__table__)
        if provider is not None:
            normalized_provider, _ = _normalized_key(provider, "_placeholder")
            statement = statement.where(
                EventMappingDecision.provider == normalized_provider
            )
        statement = statement.order_by(EventMappingDecision.id)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
            latest: dict[tuple[str, str], Mapping[str, Any]] = {}
            for row in rows:
                latest[(row["provider"], row["provider_event_id"])] = row
            pending = [
                latest[key]
                for key in sorted(latest)
                if latest[key]["decision_state"] in unresolved
            ]
            return self._decision_records(connection, pending)

    @_translate_storage_failures
    def list_conflicts(
        self,
        *,
        provider: str | None = None,
    ) -> list[EventMappingConflictRecord]:
        """Return each identity whose current state is a mapping conflict.

        A conflict is inactive by construction, so it is absent from the
        active-only mapping listing, and it is not an unresolved observation
        state, so it is absent from that queue too.  Without its own queue an
        operator would see nothing left to act on for the one state that always
        requires an operator.
        """

        statement = select(ProviderEventMapping.__table__).where(
            ProviderEventMapping.mapping_state
            == EventResolutionState.MAPPING_CONFLICT.value
        )
        if provider is not None:
            normalized_provider, _ = _normalized_key(provider, "_placeholder")
            statement = statement.where(
                ProviderEventMapping.provider == normalized_provider
            )
        statement = statement.order_by(
            ProviderEventMapping.provider, ProviderEventMapping.provider_event_id
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
            return [
                EventMappingConflictRecord(
                    mapping=self._mapping_record(row),
                    latest_decision=self._decision_result(
                        connection,
                        self._latest_conflict_decision(
                            connection, row["provider"], row["provider_event_id"]
                        ),
                    ),
                )
                for row in rows
                if row is not None
            ]

    # -- automatic board observations -------------------------------------

    def record_resolution(
        self,
        resolution: EventResolution,
        *,
        recorded_at: datetime | None = None,
    ) -> EventMappingPersistenceResult:
        """Persist a qualifying result or a later evidence conflict."""

        try:
            return self.persist_auto_decision(resolution, recorded_at=recorded_at)
        except SQLAlchemyError as exc:
            raise EventMappingPersistenceError(
                DEFAULT_EVENT_MAPPING_FAILURE_SUMMARY
            ) from exc

    def persist_auto_decision(
        self,
        resolution: EventResolution,
        *,
        recorded_at: datetime | None = None,
    ) -> EventMappingPersistenceResult:
        """Persist the first qualifying automatic result transactionally.

        The unique source identity and idempotency key make repeated board
        reads harmless, and existing manual decisions are never overwritten.  An
        observation the provider gave no stable event ID for is returned as it
        stands: it may be compared on the current board and is reevaluated on
        the next read, but nothing durable is keyed on a fabricated identity.
        """

        if not isinstance(resolution, EventResolution):
            raise TypeError("resolution must be EventResolution")
        if not resolution.is_durable:
            return EventMappingPersistenceResult(resolution.state.value, False)
        if resolution.state is EventResolutionState.EVENT_CATALOG_UNAVAILABLE:
            # Without usable catalog data there is no comparison identity to
            # record; the normalized markets stay visible regardless.
            return EventMappingPersistenceResult(resolution.state.value, False)
        if not resolution.is_auto_qualifying:
            if resolution.state is EventResolutionState.MAPPING_CONFLICT:
                return self._persist_mapping_conflict(resolution, recorded_at=recorded_at)
            if resolution.state in UNRESOLVED_EVENT_OBSERVATION_STATES:
                return self._persist_unresolved_observation(
                    resolution, recorded_at=recorded_at
                )
            if resolution.state.value in _MANUAL_MAPPING_STATES:
                return self._observe_governed_mapping(resolution)
            return EventMappingPersistenceResult(resolution.state.value, False)

        provider, provider_id = self._resolution_key(resolution)
        now = _utc(recorded_at or self._clock())
        values = self._resolution_values(resolution)
        fingerprint = self._idempotency_key(resolution)

        with self._transaction(provider, provider_id) as connection:
            governed = self._governing_decision(connection, provider, provider_id)
            if governed is not None:
                return governed
            stale = self._stale_observation(connection, provider, provider_id, resolution)
            if stale is not None:
                return stale
            self._advance_observation_clock(connection, provider, provider_id, resolution)

            existing = self._select_mapping(connection, provider, provider_id, lock=True)
            if existing is not None:
                state = str(existing["mapping_state"])
                if state == EventResolutionState.MAPPING_CONFLICT.value:
                    return EventMappingPersistenceResult(
                        state, False, mapping=self._mapping_record(existing)
                    )
                claimed = EventResolver._claimed_event_id(existing)
                # Only a mapping that still asserts an automatic claim can
                # disagree with this observation.  A row left behind by a
                # cleared rejection is decided-and-undecided history, not a live
                # claim, so the canonical game it still carries must not queue a
                # conflict against fresh evidence.
                if claimed is not None and claimed != resolution.canonical_event_id:
                    return self._write_conflict(connection, existing, resolution, now=now)
                connection.execute(
                    update(ProviderEventMapping.__table__)
                    .where(
                        and_(
                            ProviderEventMapping.provider == provider,
                            ProviderEventMapping.provider_event_id == provider_id,
                        )
                    )
                    .values(
                        **values,
                        mapping_state=EventResolutionState.AUTO.value,
                        is_active=True,
                        # Whatever this row was before, it is now an automatic
                        # mapping, so it names no conflict for an operator.
                        conflict_canonical_event_id=None,
                        last_seen_at=now,
                    )
                )
            else:
                try:
                    with connection.begin_nested():
                        connection.execute(
                            insert(ProviderEventMapping.__table__).values(
                                **values,
                                provider=provider,
                                provider_event_id=provider_id,
                                mapping_state=EventResolutionState.AUTO.value,
                                is_active=True,
                                first_seen_at=now,
                                last_seen_at=now,
                            )
                        )
                except IntegrityError:
                    # A concurrent board read won the identity race.  It is safe
                    # to continue because the unique identity is now readable in
                    # this transaction.
                    existing = self._select_mapping(connection, provider, provider_id)
                    if existing is None:
                        raise
                    if existing["mapping_state"] in _MANUAL_MAPPING_STATES:
                        return EventMappingPersistenceResult(
                            str(existing["mapping_state"]),
                            False,
                            mapping=self._mapping_record(existing),
                        )

            decision = self._insert_decision(
                connection, resolution, now=now, idempotency_key=fingerprint
            )
            mapping = self._select_mapping(connection, provider, provider_id)
            return EventMappingPersistenceResult(
                EventResolutionState.AUTO.value,
                decision is not None,
                mapping=self._mapping_record(mapping),
                decision=self._decision_result(connection, decision),
            )

    def _observe_governed_mapping(
        self,
        resolution: EventResolution,
    ) -> EventMappingPersistenceResult:
        """Record that the board still observes the operator's own identity.

        The resolver takes the operator's mapping for provider evidence that
        agrees with it, so there is nothing to append and nothing to change.
        The read itself is still news about the identity -- the provider
        reported the approved event at that instant -- so it runs through the
        same serialized transaction as every other observation and raises the
        identity's observation clock.  A conflicting read taken earlier is then
        recognized as describing evidence the provider has since replaced,
        instead of unseating a governed mapping on a stale read.
        """

        provider, provider_id = self._resolution_key(resolution)
        with self._transaction(provider, provider_id) as connection:
            stale = self._stale_observation(connection, provider, provider_id, resolution)
            if stale is not None:
                return stale
            self._advance_observation_clock(connection, provider, provider_id, resolution)
            governed = self._governing_decision(connection, provider, provider_id)
            if governed is not None:
                return governed
            mapping = self._select_mapping(connection, provider, provider_id, lock=True)
            return EventMappingPersistenceResult(
                resolution.state.value if mapping is None else str(mapping["mapping_state"]),
                False,
                mapping=self._mapping_record(mapping),
            )

    def _persist_unresolved_observation(
        self,
        resolution: EventResolution,
        *,
        recorded_at: datetime | None,
    ) -> EventMappingPersistenceResult:
        """Retain one typed unresolved observation in the audit log.

        No canonical identity was established, so no active mapping is created;
        the idempotency key keeps repeated board reads to one durable
        observation per distinct evidence shape.  A claim the board established
        is withdrawn from comparisons, keeping the canonical game it named, so a
        later unambiguous observation can simply map it again and an operator
        can still see which game the identity used to be.
        """

        provider, provider_id = self._resolution_key(resolution)
        now = _utc(recorded_at or self._clock())
        with self._transaction(provider, provider_id) as connection:
            governed = self._governing_decision(connection, provider, provider_id)
            if governed is not None:
                return governed
            stale = self._stale_observation(connection, provider, provider_id, resolution)
            if stale is not None:
                return stale
            self._advance_observation_clock(connection, provider, provider_id, resolution)
            self._withdraw_claimed_mapping(
                connection, provider, provider_id, state=resolution.state, now=now
            )
            decision = self._insert_decision(
                connection,
                resolution,
                now=now,
                idempotency_key=self._idempotency_key(resolution),
            )
            return EventMappingPersistenceResult(
                resolution.state.value,
                decision is not None,
                decision=self._decision_result(connection, decision),
            )

    def _withdraw_claimed_mapping(
        self,
        connection: Connection,
        provider: str,
        provider_id: str,
        *,
        state: EventResolutionState,
        now: datetime,
    ) -> None:
        """Withdraw a claimed mapping this evidence can no longer support.

        The resolver found two equally near games, found none at all, or found
        the claimed game no longer scheduled, so the mapping must not reach a
        board comparison.  The row is kept -- it is still the durable record of
        which NBA game this provider identity was mapped to -- but it is
        deactivated and its state records why, so a later unambiguous
        observation can map it again.

        An already withdrawn row is withdrawn again for a *different* reason:
        the operator reads the current row to decide, so it has to say why the
        identity is out of comparisons now.  Repeating the state it already
        holds writes nothing, which keeps a repeated observation a no-op beyond
        the identity's observation clock.  Only a claim the board established is
        withdrawn: an operator's decision is governed elsewhere, and a conflict
        already awaits an operator.
        """

        existing = self._select_mapping(connection, provider, provider_id, lock=True)
        if existing is None:
            return
        current = str(existing["mapping_state"])
        if current == state.value:
            return
        claimed = current == EventResolutionState.AUTO.value and bool(
            existing["is_active"]
        )
        if not claimed and current not in WITHDRAWN_EVENT_CLAIM_STATES:
            return
        connection.execute(
            update(ProviderEventMapping.__table__)
            .where(
                and_(
                    ProviderEventMapping.provider == provider,
                    ProviderEventMapping.provider_event_id == provider_id,
                )
            )
            .values(
                mapping_state=state.value,
                is_active=False,
                # A withdrawal is not a disagreement between two canonical
                # games, so the row names no conflict for an operator.
                conflict_canonical_event_id=None,
                last_seen_at=now,
            )
        )

    def _persist_mapping_conflict(
        self,
        resolution: EventResolution,
        *,
        recorded_at: datetime | None,
    ) -> EventMappingPersistenceResult:
        provider, provider_id = self._resolution_key(resolution)
        now = _utc(recorded_at or self._clock())
        with self._transaction(provider, provider_id) as connection:
            governed = self._governing_decision(
                connection,
                provider,
                provider_id,
                promoting_conflict=True,
                resolution=resolution,
            )
            if governed is not None:
                return governed
            stale = self._stale_observation(connection, provider, provider_id, resolution)
            if stale is not None:
                return stale
            self._advance_observation_clock(connection, provider, provider_id, resolution)
            existing = self._select_mapping(connection, provider, provider_id, lock=True)
            if self._is_repeated_conflict(connection, existing, resolution):
                # The resolver read the conflict this row already records, so
                # there is nothing new to append and the retained conflicting
                # game must not be overwritten with an empty one.
                return EventMappingPersistenceResult(
                    EventResolutionState.MAPPING_CONFLICT.value,
                    False,
                    mapping=self._mapping_record(existing),
                )
            if existing is None:
                # Preserve a conflict observation even if the first observation
                # of this identity was itself conflicting.
                connection.execute(
                    insert(ProviderEventMapping.__table__).values(
                        **self._resolution_values(resolution),
                        provider=provider,
                        provider_event_id=provider_id,
                        mapping_state=EventResolutionState.MAPPING_CONFLICT.value,
                        is_active=False,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                existing = self._select_mapping(connection, provider, provider_id)
            return self._write_conflict(
                connection,
                existing,
                resolution,
                now=now,
                reason=resolution.reason or "mapping_conflict",
                promoting_conflict=True,
            )

    def _write_conflict(
        self,
        connection: Connection,
        existing: Mapping[str, Any],
        resolution: EventResolution,
        *,
        now: datetime,
        reason: str = "mapping_conflict",
        promoting_conflict: bool = False,
    ) -> EventMappingPersistenceResult:
        provider, provider_id = self._resolution_key(resolution)
        # The caller may have observed an older row.  Re-lock and re-read it
        # immediately before writing so a newer governed decision wins.
        governed = self._governing_decision(
            connection,
            provider,
            provider_id,
            promoting_conflict=promoting_conflict,
            resolution=resolution,
        )
        if governed is not None:
            return governed
        existing = self._select_mapping(connection, provider, provider_id, lock=True) or existing
        values = self._resolution_values(resolution)
        connection.execute(
            update(ProviderEventMapping.__table__)
            .where(
                and_(
                    ProviderEventMapping.provider == provider,
                    ProviderEventMapping.provider_event_id == provider_id,
                )
            )
            .values(
                **{column: values[column] for column in _PROVIDER_EVIDENCE_COLUMNS},
                mapping_state=EventResolutionState.MAPPING_CONFLICT.value,
                is_active=False,
                # Evidence that names no canonical game cannot erase the
                # conflicting game an operator still has to review.
                conflict_canonical_event_id=(
                    resolution.canonical_event_id
                    or existing.get("conflict_canonical_event_id")
                ),
                last_seen_at=now,
            )
        )
        decision = self._insert_decision(
            connection,
            resolution,
            now=now,
            idempotency_key=self._idempotency_key(resolution),
            state=EventResolutionState.MAPPING_CONFLICT.value,
            reason=reason,
        )
        mapping = self._select_mapping(connection, provider, provider_id)
        return EventMappingPersistenceResult(
            EventResolutionState.MAPPING_CONFLICT.value,
            decision is not None,
            mapping=self._mapping_record(mapping),
            decision=self._decision_result(connection, decision),
        )

    # -- operator actions --------------------------------------------------

    @_translate_storage_failures
    def approve(
        self,
        provider: str,
        provider_event_id: str,
        canonical_event_id: str,
        *,
        season: str | None = None,
        operator_id: str,
        reason: str,
        provider_evidence: EventEvidence | None = None,
    ) -> EventMappingPersistenceResult:
        return self._manual_map(
            EventResolutionState.MANUAL_APPROVED,
            provider,
            provider_event_id,
            canonical_event_id,
            season=season,
            operator_id=operator_id,
            reason=reason,
            provider_evidence=provider_evidence,
        )

    @_translate_storage_failures
    def override(
        self,
        provider: str,
        provider_event_id: str,
        canonical_event_id: str,
        *,
        season: str | None = None,
        operator_id: str,
        reason: str,
        provider_evidence: EventEvidence | None = None,
    ) -> EventMappingPersistenceResult:
        return self._manual_map(
            EventResolutionState.MANUAL_OVERRIDE,
            provider,
            provider_event_id,
            canonical_event_id,
            season=season,
            operator_id=operator_id,
            reason=reason,
            provider_evidence=provider_evidence,
        )

    @_translate_storage_failures
    def reject(
        self,
        provider: str,
        provider_event_id: str,
        *,
        operator_id: str,
        reason: str,
        season: str | None = None,
    ) -> EventMappingPersistenceResult:
        provider, provider_id = _normalized_key(provider, provider_event_id)
        operator_id = _operator(operator_id)
        reason = _reason(reason)
        requested_season = validate_canonical_season(season) if season else None
        now = _utc(self._clock())
        with self._transaction(provider, provider_id) as connection:
            existing_rejection = self._select_rejection(
                connection, provider, provider_id, lock=True
            )
            if existing_rejection is None:
                connection.execute(
                    insert(EventMappingRejection.__table__).values(
                        provider=provider,
                        provider_event_id=provider_id,
                        is_active=True,
                        reason=reason,
                        operator_id=operator_id,
                        created_at=now,
                    )
                )
            else:
                connection.execute(
                    update(EventMappingRejection.__table__)
                    .where(
                        and_(
                            EventMappingRejection.provider == provider,
                            EventMappingRejection.provider_event_id == provider_id,
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
            existing_mapping = self._select_mapping(
                connection, provider, provider_id, lock=True
            )
            if existing_mapping is not None:
                connection.execute(
                    update(ProviderEventMapping.__table__)
                    .where(
                        and_(
                            ProviderEventMapping.provider == provider,
                            ProviderEventMapping.provider_event_id == provider_id,
                        )
                    )
                    .values(
                        mapping_state=EventResolutionState.REJECTED.value,
                        is_active=False,
                        # A suppressed identity has no conflict left to review.
                        conflict_canonical_event_id=None,
                        last_seen_at=now,
                    )
                )
            decision = self._insert_manual_decision(
                connection,
                provider=provider,
                provider_id=provider_id,
                season=requested_season,
                state=EventResolutionState.REJECTED.value,
                operator_id=operator_id,
                reason=reason,
                existing=existing_mapping,
                now=now,
            )
            mapping = self._select_mapping(connection, provider, provider_id)
            return EventMappingPersistenceResult(
                EventResolutionState.REJECTED.value,
                True,
                mapping=self._mapping_record(mapping),
                decision=self._decision_result(connection, decision),
            )

    @_translate_storage_failures
    def clear_rejection(
        self,
        provider: str,
        provider_event_id: str,
        *,
        operator_id: str,
        reason: str,
    ) -> bool:
        provider, provider_id = _normalized_key(provider, provider_event_id)
        operator_id = _operator(operator_id)
        reason = _reason(reason)
        now = _utc(self._clock())
        with self._transaction(provider, provider_id) as connection:
            rejection = self._select_rejection(connection, provider, provider_id, lock=True)
            if rejection is None or not rejection["is_active"]:
                return False
            connection.execute(
                update(EventMappingRejection.__table__)
                .where(
                    and_(
                        EventMappingRejection.provider == provider,
                        EventMappingRejection.provider_event_id == provider_id,
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
                provider_event_id=provider_id,
                requested_season=None,
                decision_state=EventResolutionState.REJECTION_CLEARED.value,
                operator_id=operator_id,
                reason=reason,
                created_at=now,
            )
        return True

    def _manual_map(
        self,
        state: EventResolutionState,
        provider: str,
        provider_event_id: str,
        canonical_event_id: str,
        *,
        season: str | None,
        operator_id: str,
        reason: str,
        provider_evidence: EventEvidence | None = None,
    ) -> EventMappingPersistenceResult:
        provider, provider_id = _normalized_key(provider, provider_event_id)
        operator_id = _operator(operator_id)
        reason = _reason(reason)
        game_id = _canonical_event_id(canonical_event_id)
        requested_season = validate_canonical_season(season) if season else None
        now = _utc(self._clock())
        with self._transaction(provider, provider_id) as connection:
            existing = self._select_mapping(connection, provider, provider_id, lock=True)
            if requested_season is None and existing is not None:
                requested_season = existing["season"]
            if requested_season is None:
                raise ValueError("season is required for a new manual mapping")
            canonical = self._select_canonical_event(connection, requested_season, game_id)
            if canonical is None:
                raise ValueError("canonical NBA event is not in the requested season")
            rejection = self._select_rejection(connection, provider, provider_id, lock=True)
            if rejection is not None and rejection["is_active"]:
                raise ValueError("clear the active rejection before approving this identity")
            evidence_values = self._manual_evidence_values(
                connection,
                provider,
                provider_id,
                existing=existing,
                provider_evidence=provider_evidence,
            )
            mapping_values = self._canonical_values(canonical)
            if existing is None:
                connection.execute(
                    insert(ProviderEventMapping.__table__).values(
                        provider=provider,
                        provider_event_id=provider_id,
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
                    update(ProviderEventMapping.__table__)
                    .where(
                        and_(
                            ProviderEventMapping.provider == provider,
                            ProviderEventMapping.provider_event_id == provider_id,
                        )
                    )
                    .values(
                        mapping_state=state.value,
                        is_active=True,
                        season=requested_season,
                        **mapping_values,
                        **evidence_values,
                        last_seen_at=now,
                        conflict_canonical_event_id=None,
                    )
                )
            selected = CanonicalEvent.from_row(canonical)
            decision = self._insert_manual_decision(
                connection,
                provider=provider,
                provider_id=provider_id,
                season=requested_season,
                state=state.value,
                operator_id=operator_id,
                reason=reason,
                existing=existing,
                canonical=selected,
                evidence_values=evidence_values,
                now=now,
            )
            if decision is not None and selected.is_postponed:
                # The operator chose a game automatic mapping treats as ordinary
                # evidence but whose postponement is exactly what an audit has
                # to name, so the choice is recorded as an explicit candidate.
                self._insert_candidates(connection, int(decision["id"]), (selected,))
            mapping = self._select_mapping(connection, provider, provider_id)
            result = EventMappingPersistenceResult(
                state.value,
                True,
                mapping=self._mapping_record(mapping),
                decision=self._decision_result(connection, decision),
            )
        if self._projection_replay is not None:
            try:
                self._projection_replay(result)
            except Exception:
                LOGGER.exception(
                    "projection replay failed after event mapping commit",
                    extra={
                        "provider": provider,
                        "provider_event_id": provider_id,
                    },
                )
        return result

    # -- SQL helpers -------------------------------------------------------

    def _governing_decision(
        self,
        connection: Connection,
        provider: str,
        provider_id: str,
        *,
        promoting_conflict: bool = False,
        resolution: EventResolution | None = None,
    ) -> EventMappingPersistenceResult | None:
        """Return the governed state a board observation must not supersede.

        The resolver reads outside this serialized transaction, so its result
        may already be stale: an operator can approve, override, or reject the
        identity in between.  Re-reading the current mapping and rejection here,
        immediately before any append or write, keeps a stale unresolved
        observation from requeuing a decided identity and a stale conflict from
        replacing an active rejection.

        Manual precedence protects a governed mapping from automatic
        *overwrite*, not from fail-closed conflict detection, so a conflict is
        allowed to promote it (``promoting_conflict``) -- but only while the
        governed decision still contradicts the observed evidence, so a conflict
        an operator has already resolved cannot undo the resolution.  An active
        rejection governs either way: a suppressed identity has nothing to
        conflict with.
        """

        rejection = self._select_rejection(connection, provider, provider_id, lock=True)
        mapping = self._select_mapping(connection, provider, provider_id, lock=True)
        if rejection is not None and rejection["is_active"]:
            return EventMappingPersistenceResult(
                EventResolutionState.REJECTED.value,
                False,
                mapping=self._mapping_record(mapping),
            )
        if (
            mapping is not None
            and mapping["is_active"]
            and str(mapping["mapping_state"]) in _MANUAL_MAPPING_STATES
            and (
                not promoting_conflict
                or not self._conflict_still_applies(
                    connection, provider, provider_id, mapping, resolution
                )
            )
        ):
            return EventMappingPersistenceResult(
                str(mapping["mapping_state"]),
                False,
                mapping=self._mapping_record(mapping),
            )
        return None

    @classmethod
    def _stale_observation(
        cls,
        connection: Connection,
        provider: str,
        provider_id: str,
        resolution: EventResolution,
    ) -> EventMappingPersistenceResult | None:
        """Return the current state when this read predates the governing one.

        A provider read can be slow, retried, or replayed, so an observation may
        land long after the identity moved on: rejected and cleared by an
        operator, or mapped again from a newer read.  Landing it then would
        deactivate the newer mapping and queue a conflict between two canonical
        games that were never claimed at the same time.

        The read is therefore ordered by when the provider was observed against
        the newest governing instant for the identity -- an operator decision's
        ``created_at``, and the identity's observation clock.  Both are UTC
        instants of the event itself, never a persistence time.  An observation
        contemporaneous with the governing instant is not from before it, so
        only a strictly earlier read is fenced and replaying the same read stays
        idempotent.  A caller that reports no observation instant cannot be
        ordered and is left alone.
        """

        observed_at = resolution.observed_at
        if observed_at is None:
            return None
        governing = cls._governing_observation_instant(connection, provider, provider_id)
        if governing is None or _utc(observed_at) >= governing:
            return None
        mapping = cls._select_mapping(connection, provider, provider_id, lock=True)
        state = (
            resolution.state.value if mapping is None else str(mapping["mapping_state"])
        )
        return EventMappingPersistenceResult(
            state, False, mapping=cls._mapping_record(mapping)
        )

    @classmethod
    def _governing_observation_instant(
        cls,
        connection: Connection,
        provider: str,
        provider_id: str,
    ) -> datetime | None:
        """Return the newest instant this identity was decided or observed."""

        decided_at = connection.execute(
            select(EventMappingDecision.created_at)
            .where(
                and_(
                    EventMappingDecision.provider == provider,
                    EventMappingDecision.provider_event_id == provider_id,
                    EventMappingDecision.decision_state.in_(_OPERATOR_DECISION_STATES),
                )
            )
            .order_by(EventMappingDecision.created_at.desc())
            .limit(1)
        ).scalar()
        observed_at = cls._observation_clock(connection, provider, provider_id)
        instants = [_utc(value) for value in (decided_at, observed_at) if value is not None]
        return max(instants) if instants else None

    @staticmethod
    def _observation_clock(
        connection: Connection,
        provider: str,
        provider_id: str,
    ) -> datetime | None:
        """Read the durable high-water mark of this identity's provider reads."""

        return connection.execute(
            select(EventMappingLock.last_observed_at).where(
                and_(
                    EventMappingLock.provider == provider,
                    EventMappingLock.provider_event_id == provider_id,
                )
            )
        ).scalar()

    @classmethod
    def _advance_observation_clock(
        cls,
        connection: Connection,
        provider: str,
        provider_id: str,
        resolution: EventResolution,
    ) -> None:
        """Raise the identity's observation clock to this accepted read.

        The clock is kept on the identity's lock row rather than derived from
        the decision log, because the log suppresses a repeated observation on
        purpose: a second, equivalent read appends nothing and would otherwise
        leave the mark at the first read's instant, so a read taken between the
        two would still look like news.  The lock row is already selected for
        update in this transaction, so the comparison cannot straddle a
        concurrent write, and the mark only ever moves forward.
        """

        observed_at = resolution.observed_at
        if observed_at is None:
            return
        observed_at = _utc(observed_at)
        current = cls._observation_clock(connection, provider, provider_id)
        if current is not None and _utc(current) >= observed_at:
            return
        connection.execute(
            update(EventMappingLock.__table__)
            .where(
                and_(
                    EventMappingLock.provider == provider,
                    EventMappingLock.provider_event_id == provider_id,
                )
            )
            .values(last_observed_at=observed_at)
        )

    def _conflict_still_applies(
        self,
        connection: Connection,
        provider: str,
        provider_id: str,
        mapping: Mapping[str, Any],
        resolution: EventResolution | None,
    ) -> bool:
        """Report whether observed evidence still contradicts the manual state.

        A conflict is resolved outside this transaction: an operator can approve
        or override the very identity the conflict reports, and the stale read
        may only land afterwards.  Only the provider-side evidence decides, and
        it is compared with the governing manual decision read inside this
        transaction -- the operator who recorded it saw exactly that evidence.
        Observation order cannot stand in for that comparison: a read taken
        after the decision may still carry evidence the operator never reviewed.

        The operator's canonical *game* choice is deliberately not compared --
        unlike the provider's own canonical claim, which is evidence.  An
        operator who
        accepts the provider's observation may still keep or pick a canonical
        game the automatic side would not have chosen -- that disagreement is
        the decision, not a conflict.  A contradiction asserts several evidences
        at once, and any one of them the operator never reviewed unseats the
        approval, so all of them are compared.
        """

        if resolution is None:
            return True
        governed = (
            self._latest_manual_decision(connection, provider, provider_id) or mapping
        )
        # The recheck compares start times with the window this repository was
        # composed with -- the same policy the resolver applied outside the
        # transaction -- so a deployment that narrowed it does not have its
        # decision quietly rechecked against the reviewed default instead.
        return any(
            EventResolver._manual_evidence_conflicts(
                governed, evidence, self.match_window
            )
            for evidence in resolution.asserted_evidence
        )

    @staticmethod
    def _latest_manual_decision(
        connection: Connection,
        provider: str,
        provider_id: str,
    ) -> Mapping[str, Any] | None:
        return connection.execute(
            select(EventMappingDecision.__table__)
            .where(
                and_(
                    EventMappingDecision.provider == provider,
                    EventMappingDecision.provider_event_id == provider_id,
                    EventMappingDecision.decision_state.in_(_MANUAL_MAPPING_STATES),
                )
            )
            .order_by(EventMappingDecision.id.desc())
            .limit(1)
        ).mappings().one_or_none()

    @staticmethod
    def _latest_conflict_decision(
        connection: Connection,
        provider: str,
        provider_id: str,
    ) -> Mapping[str, Any] | None:
        return connection.execute(
            select(EventMappingDecision.__table__)
            .where(
                and_(
                    EventMappingDecision.provider == provider,
                    EventMappingDecision.provider_event_id == provider_id,
                    EventMappingDecision.decision_state
                    == EventResolutionState.MAPPING_CONFLICT.value,
                )
            )
            .order_by(EventMappingDecision.id.desc())
            .limit(1)
        ).mappings().one_or_none()

    @staticmethod
    def _select_mapping(
        connection: Connection,
        provider: str,
        provider_id: str,
        *,
        lock: bool = False,
    ) -> Mapping[str, Any] | None:
        statement = select(ProviderEventMapping.__table__).where(
            and_(
                ProviderEventMapping.provider == provider,
                ProviderEventMapping.provider_event_id == provider_id,
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
        statement = select(EventMappingRejection.__table__).where(
            and_(
                EventMappingRejection.provider == provider,
                EventMappingRejection.provider_event_id == provider_id,
            )
        )
        if lock:
            statement = statement.with_for_update()
        return connection.execute(statement).mappings().one_or_none()

    @staticmethod
    def _select_canonical_event(
        connection: Connection,
        season: str,
        game_id: str,
    ) -> Mapping[str, Any] | None:
        """Read one catalog game for a season, postponed or not.

        Automatic mapping only ever selects a game inside the reviewed window; a
        governed approve or override may select any scheduled game of the
        season, which is how an operator resolves a replacement identity.
        """

        row = connection.execute(
            select(EventCatalogEntry.__table__).where(
                and_(
                    EventCatalogEntry.season == season,
                    EventCatalogEntry.nba_game_id == game_id,
                )
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        event = dict(row)
        evidence = event.get("postponement_evidence")
        if evidence:
            try:
                event["postponement_evidence"] = json.loads(evidence)
            except (TypeError, ValueError):
                pass
        event["is_postponed"] = is_postponed_event(event)
        return event

    @staticmethod
    def _latest_decision(
        connection: Connection,
        provider: str,
        provider_id: str,
    ) -> Mapping[str, Any] | None:
        return connection.execute(
            select(EventMappingDecision.__table__)
            .where(
                and_(
                    EventMappingDecision.provider == provider,
                    EventMappingDecision.provider_event_id == provider_id,
                )
            )
            .order_by(EventMappingDecision.id.desc())
            .limit(1)
        ).mappings().one_or_none()

    @staticmethod
    def _latest_observed_evidence(
        connection: Connection,
        provider: str,
        provider_id: str,
    ) -> Mapping[str, Any] | None:
        """Read the newest board observation that carries provider evidence.

        Rejections, clearings, and evidence-free observations are skipped, so an
        identity that was observed, rejected, cleared, and then approved still
        records what the provider actually reported for it.
        """

        evidence_columns = [
            getattr(EventMappingDecision, column) for column in _PROVIDER_EVIDENCE_COLUMNS
        ]
        return connection.execute(
            select(EventMappingDecision.__table__)
            .where(
                and_(
                    EventMappingDecision.provider == provider,
                    EventMappingDecision.provider_event_id == provider_id,
                    EventMappingDecision.decision_state.in_(
                        _OBSERVED_EVIDENCE_DECISION_STATES
                    ),
                    or_(*(column.isnot(None) for column in evidence_columns)),
                )
            )
            .order_by(EventMappingDecision.id.desc())
            .limit(1)
        ).mappings().one_or_none()

    @staticmethod
    def _latest_mapping_evidence_decision_id(
        connection: Connection,
        provider: str,
        provider_id: str,
    ) -> int:
        """Return the ID of the decision the mapping row's evidence came from.

        Zero when no decision ever wrote evidence onto the row, which leaves any
        durable board observation newer than it.
        """

        decision_id = connection.execute(
            select(EventMappingDecision.id)
            .where(
                and_(
                    EventMappingDecision.provider == provider,
                    EventMappingDecision.provider_event_id == provider_id,
                    EventMappingDecision.decision_state.in_(
                        _MAPPING_EVIDENCE_DECISION_STATES
                    ),
                )
            )
            .order_by(EventMappingDecision.id.desc())
            .limit(1)
        ).scalar()
        return 0 if decision_id is None else int(decision_id)

    @classmethod
    def _latest_decision_key(
        cls,
        connection: Connection,
        provider: str,
        provider_id: str,
    ) -> str | None:
        latest = cls._latest_decision(connection, provider, provider_id)
        return None if latest is None else latest["idempotency_key"]

    @classmethod
    def _manual_evidence_values(
        cls,
        connection: Connection,
        provider: str,
        provider_id: str,
        *,
        existing: Mapping[str, Any] | None,
        provider_evidence: EventEvidence | None,
    ) -> dict[str, Any]:
        """Choose the typed provider evidence a manual decision records.

        An operator who supplies no evidence is not discarding what the provider
        reported, so the freshest evidence for the identity is carried forward
        -- and freshest is a question of order, not of which table it sits in.  A
        governing manual mapping is authoritative: an operator saw that evidence
        and no board read may supersede it.  Otherwise the newest durable board
        observation wins whenever it postdates the decision that last wrote the
        mapping row, because a rejected or otherwise stale mapping keeps
        reporting evidence the board has since replaced.
        """

        if provider_evidence is not None:
            return cls._evidence_values(provider_evidence)
        if (
            existing is not None
            and existing["is_active"]
            and str(existing["mapping_state"]) in _MANUAL_MAPPING_STATES
        ):
            return cls._existing_evidence_values(existing)
        observed = cls._latest_observed_evidence(connection, provider, provider_id)
        if observed is not None and (
            existing is None
            or int(observed["id"])
            > cls._latest_mapping_evidence_decision_id(connection, provider, provider_id)
        ):
            return cls._existing_evidence_values(observed)
        return cls._existing_evidence_values(existing)

    def _insert_decision(
        self,
        connection: Connection,
        resolution: EventResolution,
        *,
        now: datetime,
        idempotency_key: str,
        state: str | None = None,
        reason: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Append one automatic decision or unresolved observation."""

        provider, provider_id = self._resolution_key(resolution)
        if self._latest_decision_key(connection, provider, provider_id) == idempotency_key:
            # Only a repeated *consecutive* observation is a duplicate.  The
            # same evidence after a different observation or decision is a new
            # transition an operator has to be able to see.
            return None
        decision_state = state or resolution.state.value
        values = self._resolution_values(resolution)
        values.pop("season", None)
        values.update(
            provider=provider,
            provider_event_id=provider_id,
            requested_season=resolution.season,
            decision_state=decision_state,
            operator_id=None,
            reason=reason or resolution.reason,
            idempotency_key=idempotency_key,
            observed_at=resolution.observed_at,
            created_at=now,
        )
        decision = self._insert_decision_values(connection, **values)
        if decision is not None and decision_state in _CANDIDATE_DECISION_STATES:
            self._insert_candidates(
                connection, int(decision["id"]), resolution.candidates
            )
        if decision is not None:
            self._insert_contradictory_evidence(
                connection, int(decision["id"]), resolution.contradictory_evidence
            )
        return decision

    @staticmethod
    def _insert_candidates(
        connection: Connection,
        decision_id: int,
        candidates: tuple[CanonicalEvent, ...],
    ) -> None:
        """Retain the candidates an operator has to choose between.

        Only observations whose canonical identity is still open carry
        candidates -- unresolved evidence that established none, and a conflict
        whose canonical side is exactly what is disputed.  Each records how far
        it sat from the provider's start time and whether the season still
        schedules it, which is what makes the queue actionable.
        """

        if not candidates:
            return
        connection.execute(
            insert(EventMappingDecisionCandidate.__table__),
            [
                {
                    "decision_id": decision_id,
                    "canonical_event_id": candidate.nba_game_id,
                    "scheduled_at": (
                        None
                        if candidate.scheduled_at is None
                        else _utc(candidate.scheduled_at)
                    ),
                    "home_team_id": candidate.home_team_id,
                    "home_team_name": candidate.home_team_name,
                    "home_team_tricode": candidate.home_team_tricode,
                    "away_team_id": candidate.away_team_id,
                    "away_team_name": candidate.away_team_name,
                    "away_team_tricode": candidate.away_team_tricode,
                    "start_offset_seconds": candidate.start_offset_seconds,
                    "is_postponed": candidate.is_postponed,
                    "is_scheduled": candidate.is_scheduled,
                }
                for candidate in candidates
            ],
        )

    @classmethod
    def _insert_contradictory_evidence(
        cls,
        connection: Connection,
        decision_id: int,
        evidence: tuple[EventEvidence, ...],
    ) -> None:
        """Retain every evidence a contradictory observation asserted.

        The decision row records one representative evidence, so without these
        rows the rest of the contradiction would exist only in the observation
        that produced it and would be missing from the operator's conflict
        queue and the identity's history.  The rows are written in the
        repository's own total evidence order rather than the caller's, so the
        same set of contradicting markets is stored identically however it was
        observed.
        """

        if not evidence:
            return
        connection.execute(
            insert(EventMappingDecisionContradiction.__table__),
            [
                {"decision_id": decision_id, "evidence_index": index, **values}
                for index, values in enumerate(cls._ordered_contradiction(evidence))
            ],
        )

    @classmethod
    def _ordered_contradiction(
        cls,
        evidence: tuple[EventEvidence, ...],
    ) -> list[dict[str, Any]]:
        """One contradiction as typed values in a total, stable order."""

        return sorted(
            (cls._contradiction_values(item) for item in evidence),
            key=cls._contradiction_order,
        )

    @staticmethod
    def _contradiction_values(evidence: EventEvidence) -> dict[str, Any]:
        """Every retained field of one contradicting provider evidence."""

        if not isinstance(evidence, EventEvidence):
            raise TypeError("contradictory evidence must be EventEvidence")
        values: dict[str, Any] = {
            "provider_event_id": evidence.provider_id,
            "provider_canonical_event_id": evidence.canonical_id,
            "provider_event_label": evidence.label,
            "provider_status_label": evidence.status_label,
        }
        for column, value in (
            ("provider_starts_at", evidence.starts_at),
            ("provider_ends_at", evidence.ends_at),
            ("provider_updated_at", evidence.updated_at),
        ):
            instant = stored_timestamp(value)
            values[column] = None if instant is None else _utc(instant)
        for side in ("home", "away"):
            team = getattr(evidence, f"{side}_team")
            values[f"provider_{side}_team_id"] = team.provider_id if team else None
            values[f"provider_{side}_team_canonical_id"] = (
                team.canonical_id if team else None
            )
            values[f"provider_{side}_team_name"] = team.name if team else None
            values[f"provider_{side}_team_abbreviation"] = (
                team.abbreviation if team else None
            )
        return values

    @staticmethod
    def _contradiction_order(values: Mapping[str, Any]) -> list[tuple[bool, str]]:
        """A total order over contradicting evidence, on every field it carries.

        The fields are compared in their declared order -- identity first, then
        presentation, then each side of the matchup -- so what an evidence
        claims decides before how it is spelled.  A fact nothing reported orders
        after every reported one rather than comparing as an empty string, so
        two evidences compare equal only when they report exactly the same
        thing.
        """

        return [
            (values[column] is None, "" if values[column] is None else str(values[column]))
            for column in _CONTRADICTION_COLUMNS
        ]

    @staticmethod
    def _insert_decision_values(
        connection: Connection,
        *,
        provider: str,
        provider_event_id: str,
        requested_season: str | None,
        decision_state: str,
        operator_id: str | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
        observed_at: datetime | None = None,
        created_at: datetime,
        **evidence: Any,
    ) -> Mapping[str, Any] | None:
        values: dict[str, Any] = {
            "provider": provider,
            "provider_event_id": provider_event_id,
            "requested_season": requested_season,
            "decision_state": decision_state,
            "operator_id": operator_id,
            "reason": reason,
            "idempotency_key": idempotency_key,
            "observed_at": None if observed_at is None else _utc(observed_at),
            "created_at": created_at,
        }
        for column in _CANONICAL_EVENT_COLUMNS + _PROVIDER_EVIDENCE_COLUMNS:
            values[column] = evidence.pop(column, None)
        if evidence:
            raise TypeError(
                f"unsupported event decision columns: {sorted(evidence)}"
            )
        result = connection.execute(
            insert(EventMappingDecision.__table__).values(**values)
        )
        decision_id = result.inserted_primary_key[0]
        return connection.execute(
            select(EventMappingDecision.__table__).where(
                EventMappingDecision.id == decision_id
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
        canonical: CanonicalEvent | None = None,
        evidence_values: Mapping[str, Any] | None = None,
        now: datetime,
    ) -> Mapping[str, Any] | None:
        existing = existing or {}
        canonical_values = (
            self._canonical_values_from_event(canonical)
            if canonical is not None
            else {
                column: existing.get(column) for column in _CANONICAL_EVENT_COLUMNS
            }
        )
        source = evidence_values or existing
        return self._insert_decision_values(
            connection,
            provider=provider,
            provider_event_id=provider_id,
            requested_season=season,
            decision_state=state,
            operator_id=operator_id,
            reason=reason,
            created_at=now,
            **canonical_values,
            **{
                column: source.get(column) for column in _PROVIDER_EVIDENCE_COLUMNS
            },
        )

    @staticmethod
    def _resolution_key(resolution: EventResolution) -> tuple[str, str]:
        if not resolution.provider_event_id:
            raise ValueError("a persisted mapping requires provider event identity")
        return _normalized_key(resolution.provider, resolution.provider_event_id)

    @classmethod
    def _resolution_values(cls, resolution: EventResolution) -> dict[str, Any]:
        values: dict[str, Any] = {"season": resolution.season}
        values.update(cls._canonical_values_from_event(resolution.canonical_event))
        values.update(cls._evidence_values(resolution.provider_evidence))
        return values

    @staticmethod
    def _canonical_values_from_event(event: CanonicalEvent | None) -> dict[str, Any]:
        if event is None:
            return {column: None for column in _CANONICAL_EVENT_COLUMNS}
        return {
            "canonical_event_id": event.nba_game_id,
            "canonical_scheduled_at": (
                None if event.scheduled_at is None else _utc(event.scheduled_at)
            ),
            "canonical_home_team_id": event.home_team_id,
            "canonical_home_team_name": event.home_team_name,
            "canonical_home_team_tricode": event.home_team_tricode,
            "canonical_away_team_id": event.away_team_id,
            "canonical_away_team_name": event.away_team_name,
            "canonical_away_team_tricode": event.away_team_tricode,
        }

    @staticmethod
    def _canonical_values(canonical: Mapping[str, Any]) -> dict[str, Any]:
        scheduled_at = stored_timestamp(canonical.get("scheduled_at"))
        return {
            "canonical_event_id": str(canonical["nba_game_id"]),
            "canonical_scheduled_at": (
                None if scheduled_at is None else _utc(scheduled_at)
            ),
            "canonical_home_team_id": canonical.get("home_team_id"),
            "canonical_home_team_name": canonical.get("home_team_name"),
            "canonical_home_team_tricode": canonical.get("home_team_tricode"),
            "canonical_away_team_id": canonical.get("away_team_id"),
            "canonical_away_team_name": canonical.get("away_team_name"),
            "canonical_away_team_tricode": canonical.get("away_team_tricode"),
        }

    @staticmethod
    def _evidence_values(evidence: EventEvidence) -> dict[str, Any]:
        if not isinstance(evidence, EventEvidence):
            raise TypeError("provider_evidence must be EventEvidence")
        starts_at = stored_timestamp(evidence.starts_at)
        values: dict[str, Any] = {
            "provider_canonical_event_id": evidence.canonical_id,
            "provider_event_label": evidence.label,
            "provider_starts_at": None if starts_at is None else _utc(starts_at),
            "provider_status_label": evidence.status_label,
        }
        for side in ("home", "away"):
            team = getattr(evidence, f"{side}_team")
            values[f"provider_{side}_team_id"] = team.provider_id if team else None
            values[f"provider_{side}_team_canonical_id"] = (
                team.canonical_id if team else None
            )
            values[f"provider_{side}_team_name"] = team.name if team else None
            values[f"provider_{side}_team_abbreviation"] = (
                team.abbreviation if team else None
            )
        return values

    @staticmethod
    def _existing_evidence_values(existing: Mapping[str, Any] | None) -> dict[str, Any]:
        """Read a stored row's provider evidence back as typed values.

        The start time is normalized to an aware UTC instant, because SQLite
        returns it without a zone: copying that value forward would write a
        naive instant back, and comparing it with fresh evidence would read
        every repeat as news.
        """

        existing = existing or {}
        values = {column: existing.get(column) for column in _PROVIDER_EVIDENCE_COLUMNS}
        values["provider_starts_at"] = stored_timestamp(values["provider_starts_at"])
        return values

    @classmethod
    def _is_repeated_conflict(
        cls,
        connection: Connection,
        existing: Mapping[str, Any] | None,
        resolution: EventResolution,
    ) -> bool:
        """Report whether a conflict read repeats the recorded conflict.

        A resolver that starts from an existing conflict row carries no
        canonical game, so writing it again would only clear the retained
        conflicting game and append a duplicate decision.  Evidence the read
        simply does not carry is not a difference: one market of a snapshot may
        report a team another omits, so only a fact the read actually asserts is
        compared, and a repeat is a read that adds nothing the row does not
        already record.

        The mapping row holds one representative evidence, so the contradiction
        itself is compared with the rows the recorded conflict retained.  A read
        that changes any evidence the operator has to review -- including one
        the decision was not recorded on -- is a different conflict, however
        unchanged the representative looks.
        """

        if existing is None or resolution.canonical_event is not None:
            return False
        if existing["mapping_state"] != EventResolutionState.MAPPING_CONFLICT.value:
            return False
        values = cls._resolution_values(resolution)
        recorded = cls._existing_evidence_values(existing)
        if any(
            values[column] is not None and recorded[column] != values[column]
            for column in _PROVIDER_EVIDENCE_COLUMNS
        ):
            return False
        return cls._recorded_contradiction(
            connection, *cls._resolution_key(resolution)
        ) == [
            cls._contradiction_order(values)
            for values in cls._ordered_contradiction(resolution.contradictory_evidence)
        ]

    @staticmethod
    def _recorded_contradiction(
        connection: Connection,
        provider: str,
        provider_id: str,
    ) -> list[list[tuple[bool, str]]]:
        """The contradiction the identity's recorded conflict already retains."""

        decision_id = connection.execute(
            select(EventMappingDecision.id)
            .where(
                and_(
                    EventMappingDecision.provider == provider,
                    EventMappingDecision.provider_event_id == provider_id,
                    EventMappingDecision.decision_state
                    == EventResolutionState.MAPPING_CONFLICT.value,
                )
            )
            .order_by(EventMappingDecision.id.desc())
            .limit(1)
        ).scalar()
        if decision_id is None:
            return []
        rows = connection.execute(
            select(EventMappingDecisionContradiction.__table__)
            .where(EventMappingDecisionContradiction.decision_id == int(decision_id))
            .order_by(EventMappingDecisionContradiction.evidence_index)
        ).mappings().all()
        return [
            EventMappingRepository._contradiction_order(
                {
                    column: (
                        _utc(stored_timestamp(row[column]))
                        if column in _CONTRADICTION_INSTANT_COLUMNS
                        and row[column] is not None
                        else row[column]
                    )
                    for column in _CONTRADICTION_COLUMNS
                }
            )
            for row in rows
        ]

    @classmethod
    def _idempotency_key(cls, resolution: EventResolution) -> str:
        evidence = resolution.provider_evidence
        starts_at = stored_timestamp(evidence.starts_at)
        payload = {
            "provider": resolution.provider,
            "provider_event_id": evidence.provider_id,
            "season": resolution.season,
            "state": resolution.state.value,
            "canonical_event_id": resolution.canonical_event_id,
            "provider_canonical_event_id": evidence.canonical_id,
            "provider_event_label": evidence.label,
            "provider_starts_at": None if starts_at is None else _utc(starts_at).isoformat(),
            "provider_status_label": evidence.status_label,
            "provider_teams": [
                [
                    None if team is None else team.provider_id,
                    None if team is None else team.canonical_id,
                    None if team is None else team.name,
                    None if team is None else team.abbreviation,
                ]
                for team in (evidence.home_team, evidence.away_team)
            ],
            # Changed candidate evidence is different evidence for an operator
            # to review, so every typed candidate field the audit retains is
            # part of the fingerprint rather than the game ID alone.
            "candidates": sorted(
                (
                    [
                        candidate.nba_game_id,
                        None
                        if candidate.scheduled_at is None
                        else _utc(candidate.scheduled_at).isoformat(),
                        candidate.home_team_id,
                        candidate.away_team_id,
                        candidate.start_offset_seconds,
                        candidate.is_postponed,
                        candidate.is_scheduled,
                    ]
                    for candidate in resolution.candidates
                ),
                key=lambda candidate: str(candidate[0]),
            ),
            # A contradiction is fingerprinted by everything it contradicts, in
            # every field the audit retains, not only by the evidence it
            # happened to be recorded on.  A snapshot that lists the same
            # markets in another order is then the same observation, while a
            # read that changes any part of what an operator has to review --
            # including a part of it the decision was not recorded on -- is
            # news rather than a repeat.
            "contradictory_evidence": [
                {
                    column: _iso(value) if isinstance(value, datetime) else value
                    for column, value in values.items()
                }
                for values in cls._ordered_contradiction(
                    resolution.contradictory_evidence
                )
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _mapping_record(
        row: Mapping[str, Any] | None,
    ) -> ProviderEventMappingRecord | None:
        if row is None:
            return None
        return ProviderEventMappingRecord(
            provider=row["provider"],
            provider_event_id=row["provider_event_id"],
            mapping_state=row["mapping_state"],
            is_active=bool(row["is_active"]),
            season=row["season"],
            canonical_event_id=row["canonical_event_id"],
            canonical_scheduled_at=_iso(row["canonical_scheduled_at"]),
            canonical_home_team_id=row["canonical_home_team_id"],
            canonical_home_team_name=row["canonical_home_team_name"],
            canonical_home_team_tricode=row["canonical_home_team_tricode"],
            canonical_away_team_id=row["canonical_away_team_id"],
            canonical_away_team_name=row["canonical_away_team_name"],
            canonical_away_team_tricode=row["canonical_away_team_tricode"],
            provider_canonical_event_id=row["provider_canonical_event_id"],
            provider_event_label=row["provider_event_label"],
            provider_starts_at=_iso(row["provider_starts_at"]),
            provider_status_label=row["provider_status_label"],
            provider_home_team_id=row["provider_home_team_id"],
            provider_home_team_canonical_id=row["provider_home_team_canonical_id"],
            provider_home_team_name=row["provider_home_team_name"],
            provider_home_team_abbreviation=row["provider_home_team_abbreviation"],
            provider_away_team_id=row["provider_away_team_id"],
            provider_away_team_canonical_id=row["provider_away_team_canonical_id"],
            provider_away_team_name=row["provider_away_team_name"],
            provider_away_team_abbreviation=row["provider_away_team_abbreviation"],
            conflict_canonical_event_id=row["conflict_canonical_event_id"],
            first_seen_at=_iso(row["first_seen_at"]) or "",
            last_seen_at=_iso(row["last_seen_at"]) or "",
        )

    @classmethod
    def _decision_result(
        cls,
        connection: Connection,
        row: Mapping[str, Any] | None,
    ) -> EventMappingDecisionRecord | None:
        """Build one decision record with its durable candidates."""

        if row is None:
            return None
        (record,) = cls._decision_records(connection, [row])
        return record

    @classmethod
    def _decision_records(
        cls,
        connection: Connection,
        rows: Any,
    ) -> list[EventMappingDecisionRecord]:
        rows = [row for row in rows if row is not None]
        decision_ids = [int(row["id"]) for row in rows]
        candidates = cls._select_candidates(connection, decision_ids)
        contradictions = cls._select_contradictory_evidence(connection, decision_ids)
        return [
            cls._decision_record(
                row,
                candidates.get(int(row["id"]), ()),
                contradictions.get(int(row["id"]), ()),
            )
            for row in rows
        ]

    @staticmethod
    def _select_contradictory_evidence(
        connection: Connection,
        decision_ids: list[int],
    ) -> dict[int, tuple[EventMappingEvidenceRecord, ...]]:
        if not decision_ids:
            return {}
        rows = connection.execute(
            select(EventMappingDecisionContradiction.__table__)
            .where(EventMappingDecisionContradiction.decision_id.in_(decision_ids))
            .order_by(
                EventMappingDecisionContradiction.decision_id,
                EventMappingDecisionContradiction.evidence_index,
            )
        ).mappings().all()
        evidence: dict[int, tuple[EventMappingEvidenceRecord, ...]] = {}
        for row in rows:
            decision_id = int(row["decision_id"])
            evidence[decision_id] = evidence.get(decision_id, ()) + (
                EventMappingEvidenceRecord(
                    provider_event_id=row["provider_event_id"],
                    provider_canonical_event_id=row["provider_canonical_event_id"],
                    provider_event_label=row["provider_event_label"],
                    provider_starts_at=_iso(stored_timestamp(row["provider_starts_at"])),
                    provider_ends_at=_iso(stored_timestamp(row["provider_ends_at"])),
                    provider_updated_at=_iso(
                        stored_timestamp(row["provider_updated_at"])
                    ),
                    provider_status_label=row["provider_status_label"],
                    provider_home_team_id=row["provider_home_team_id"],
                    provider_home_team_canonical_id=row[
                        "provider_home_team_canonical_id"
                    ],
                    provider_home_team_name=row["provider_home_team_name"],
                    provider_home_team_abbreviation=row[
                        "provider_home_team_abbreviation"
                    ],
                    provider_away_team_id=row["provider_away_team_id"],
                    provider_away_team_canonical_id=row[
                        "provider_away_team_canonical_id"
                    ],
                    provider_away_team_name=row["provider_away_team_name"],
                    provider_away_team_abbreviation=row[
                        "provider_away_team_abbreviation"
                    ],
                ),
            )
        return evidence

    @staticmethod
    def _select_candidates(
        connection: Connection,
        decision_ids: list[int],
    ) -> dict[int, tuple[EventMappingCandidateRecord, ...]]:
        if not decision_ids:
            return {}
        rows = connection.execute(
            select(EventMappingDecisionCandidate.__table__)
            .where(EventMappingDecisionCandidate.decision_id.in_(decision_ids))
            .order_by(
                EventMappingDecisionCandidate.decision_id,
                EventMappingDecisionCandidate.canonical_event_id,
            )
        ).mappings().all()
        candidates: dict[int, tuple[EventMappingCandidateRecord, ...]] = {}
        for row in rows:
            decision_id = int(row["decision_id"])
            candidates[decision_id] = candidates.get(decision_id, ()) + (
                EventMappingCandidateRecord(
                    canonical_event_id=row["canonical_event_id"],
                    scheduled_at=_iso(row["scheduled_at"]),
                    home_team_id=row["home_team_id"],
                    home_team_name=row["home_team_name"],
                    home_team_tricode=row["home_team_tricode"],
                    away_team_id=row["away_team_id"],
                    away_team_name=row["away_team_name"],
                    away_team_tricode=row["away_team_tricode"],
                    start_offset_seconds=row["start_offset_seconds"],
                    is_postponed=bool(row["is_postponed"]),
                    is_scheduled=bool(row["is_scheduled"]),
                ),
            )
        return candidates

    @staticmethod
    def _decision_record(
        row: Mapping[str, Any] | None,
        candidates: tuple[EventMappingCandidateRecord, ...] = (),
        contradictory_evidence: tuple[EventMappingEvidenceRecord, ...] = (),
    ) -> EventMappingDecisionRecord | None:
        if row is None:
            return None
        return EventMappingDecisionRecord(
            id=int(row["id"]),
            provider=row["provider"],
            provider_event_id=row["provider_event_id"],
            requested_season=row["requested_season"],
            decision_state=row["decision_state"],
            canonical_event_id=row["canonical_event_id"],
            canonical_scheduled_at=_iso(row["canonical_scheduled_at"]),
            canonical_home_team_id=row["canonical_home_team_id"],
            canonical_home_team_name=row["canonical_home_team_name"],
            canonical_home_team_tricode=row["canonical_home_team_tricode"],
            canonical_away_team_id=row["canonical_away_team_id"],
            canonical_away_team_name=row["canonical_away_team_name"],
            canonical_away_team_tricode=row["canonical_away_team_tricode"],
            provider_canonical_event_id=row["provider_canonical_event_id"],
            provider_event_label=row["provider_event_label"],
            provider_starts_at=_iso(row["provider_starts_at"]),
            provider_status_label=row["provider_status_label"],
            provider_home_team_id=row["provider_home_team_id"],
            provider_home_team_canonical_id=row["provider_home_team_canonical_id"],
            provider_home_team_name=row["provider_home_team_name"],
            provider_home_team_abbreviation=row["provider_home_team_abbreviation"],
            provider_away_team_id=row["provider_away_team_id"],
            provider_away_team_canonical_id=row["provider_away_team_canonical_id"],
            provider_away_team_name=row["provider_away_team_name"],
            provider_away_team_abbreviation=row["provider_away_team_abbreviation"],
            operator_id=row["operator_id"],
            reason=row["reason"],
            observed_at=_iso(row["observed_at"]),
            created_at=_iso(row["created_at"]) or "",
            candidates=candidates,
            contradictory_evidence=contradictory_evidence,
        )

    @staticmethod
    def _rejection_record(
        row: Mapping[str, Any] | None,
    ) -> EventMappingRejectionRecord | None:
        if row is None:
            return None
        return EventMappingRejectionRecord(
            provider=row["provider"],
            provider_event_id=row["provider_event_id"],
            is_active=bool(row["is_active"]),
            reason=row["reason"],
            operator_id=row["operator_id"],
            created_at=_iso(row["created_at"]) or "",
            cleared_at=_iso(row["cleared_at"]),
            cleared_by=row["cleared_by"],
            clear_reason=row["clear_reason"],
        )


__all__ = [
    "UNRESOLVED_EVENT_OBSERVATION_STATES",
    "BoardEventMappingOutcome",
    "EventMappingCandidateRecord",
    "EventMappingConflictRecord",
    "EventMappingDecisionRecord",
    "EventMappingEvidenceRecord",
    "EventMappingPersistenceError",
    "EventMappingPersistenceResult",
    "EventMappingRejectionRecord",
    "EventMappingRepository",
    "ProviderEventMappingRecord",
]
