"""Transactional persistence for provider athlete mapping decisions."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from contextlib import contextmanager
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.errors import InvalidConfigurationError
from app.models.athlete_mapping import (
    AthleteMappingDecision,
    AthleteMappingDecisionCandidate,
    AthleteMappingDecisionContradiction,
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
    CLAIMING_MAPPING_STATES,
    WITHDRAWN_CLAIM_STATES,
    AthleteResolution,
    AthleteResolver,
    CanonicalAthlete,
    MappingResolutionState,
)
from app.utils.db import is_demo_database_url


LOGGER = logging.getLogger(__name__)


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

#: Unresolved observation states that also withdraw a claimed mapping from
#: board comparisons.  An identity may not stay comparable while the board says
#: which canonical athlete it is cannot be established from this evidence: the
#: catalog lists the mapped athlete as inactive for the requested season, names
#: two equally exact athletes, or no longer lists the claimed athlete at all.
#: The claim is suspended, not retracted, so the row keeps its canonical
#: athlete and the observation keeps its candidates.
_WITHDRAWING_OBSERVATION_STATES = (
    MappingResolutionState.INACTIVE_ONLY,
    MappingResolutionState.AMBIGUOUS,
    MappingResolutionState.UNMATCHED,
)

#: Decision states whose candidates an operator still has to choose between.
#: An unresolved observation established no canonical identity, and a mapping
#: conflict established one nobody may rely on: both leave the athletes the
#: evidence named as the only actionable record of what has to be reviewed.
_CANDIDATE_DECISION_STATES = frozenset(
    {state.value for state in UNRESOLVED_OBSERVATION_STATES}
    | {MappingResolutionState.MAPPING_CONFLICT.value}
)

#: Mapping states an operator established and a board observation may not
#: supersede.
_MANUAL_MAPPING_STATES = frozenset(
    {
        MappingResolutionState.MANUAL_APPROVED.value,
        MappingResolutionState.MANUAL_OVERRIDE.value,
    }
)

#: Decision states an operator recorded.  Each is its own event, so its
#: ``created_at`` is when the identity was decided and is the instant a board
#: observation has to postdate to still be news.
_OPERATOR_DECISION_STATES = frozenset(
    _MANUAL_MAPPING_STATES
    | {
        MappingResolutionState.REJECTED.value,
        MappingResolutionState.REJECTION_CLEARED.value,
    }
)

#: Decision states whose provider evidence is a direct board observation of the
#: identity.  Operator decisions only ever copy evidence forward, and a
#: mapping conflict records the *contradicting* side of a disagreement, so
#: neither may be read as what the provider reports for the identity.
_OBSERVED_EVIDENCE_DECISION_STATES = frozenset(
    {state.value for state in UNRESOLVED_OBSERVATION_STATES}
    | {MappingResolutionState.AUTO.value}
)

#: Decision states whose evidence was written onto the mapping row itself.  The
#: newest of these is what the mapping row currently reports, so it is the
#: chronological marker a later board observation has to beat.  A rejection or
#: its clearing carries no evidence of its own and only freezes what was
#: already there, so neither appears here.
_MAPPING_EVIDENCE_DECISION_STATES = frozenset(
    _MANUAL_MAPPING_STATES
    | {
        MappingResolutionState.AUTO.value,
        MappingResolutionState.MAPPING_CONFLICT.value,
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


def _normalized_keys(
    provider: str, provider_athlete_ids: Iterable[str]
) -> tuple[str, tuple[str, ...]]:
    """Normalize one provider and every identity it names, without duplicates."""

    normalized_provider, _ = _normalized_key(provider, "_placeholder")
    ordered: dict[str, None] = {}
    for provider_athlete_id in provider_athlete_ids:
        _, normalized_id = _normalized_key(provider, provider_athlete_id)
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
class MappingEvidenceRecord:
    """One typed provider evidence a contradictory observation asserted."""

    provider_athlete_id: str | None
    provider_canonical_id: int | None
    provider_name: str | None
    provider_team_id: str | None
    provider_team_canonical_id: int | None
    provider_team_name: str | None
    provider_team_abbreviation: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_athlete_id": self.provider_athlete_id,
            "provider_canonical_id": self.provider_canonical_id,
            "provider_name": self.provider_name,
            "provider_team_id": self.provider_team_id,
            "provider_team_canonical_id": self.provider_team_canonical_id,
            "provider_team_name": self.provider_team_name,
            "provider_team_abbreviation": self.provider_team_abbreviation,
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
    observed_at: str | None
    created_at: str
    candidates: tuple[MappingCandidateRecord, ...] = ()
    #: Every typed evidence a fail-closed observation contradicted itself
    #: over.  The decision itself carries only the representative one.
    contradictory_evidence: tuple[MappingEvidenceRecord, ...] = ()

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
            "observed_at": self.observed_at,
            "created_at": self.created_at,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "contradictory_evidence": [
                evidence.to_dict() for evidence in self.contradictory_evidence
            ],
        }


@dataclass(frozen=True, slots=True)
class MappingConflictRecord:
    """One identity whose current state is an unreviewed mapping conflict.

    The current row carries the provider identity, its observed evidence, the
    canonical side that was established or approved, and the conflicting
    candidate; the decision that recorded the conflict carries the reason and
    the time an operator has to reason from.
    """

    mapping: ProviderAthleteMappingRecord
    latest_decision: MappingDecisionRecord | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping": self.mapping.to_dict(),
            "latest_decision": (
                None if self.latest_decision is None else self.latest_decision.to_dict()
            ),
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
class BoardMappingOutcome:
    """What one board observation turned out to mean for the identity.

    The board resolves each market before the mapping transaction opens, so the
    state the resolver computed is only a proposal: an operator can reject,
    approve, or override the identity in between, and a concurrent observation
    can promote it to a conflict.  This value keeps both sides -- the
    ``resolution`` the board observed and the ``state`` governance actually
    reached -- so a caller never has to re-read the repository to learn which
    one holds, and therefore cannot race the write it just made.
    """

    resolution: AthleteResolution
    state: MappingResolutionState
    persisted: bool
    mapping: ProviderAthleteMappingRecord | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.resolution, AthleteResolution):
            raise ValueError("board mapping outcome requires an AthleteResolution")
        state = (
            self.state
            if isinstance(self.state, MappingResolutionState)
            else MappingResolutionState(self.state)
        )
        if self.mapping is not None and state not in CLAIMING_MAPPING_STATES:
            raise ValueError("a fail-closed mapping outcome may not carry a mapping")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "persisted", bool(self.persisted))

    @property
    def observed_state(self) -> MappingResolutionState:
        """What the board resolved before governance had its say."""

        return self.resolution.state

    @property
    def canonical_player_id(self) -> int | None:
        """The canonical athlete this identity may be compared as.

        Only the active governed mapping supplies it, so a suppressed,
        disputed, or withdrawn identity cannot reach a comparison through the
        canonical athlete the pre-transaction resolution happened to name.  A
        claiming state with no mapping in hand is no exception: a fenced read
        reports the state that governs the identity while storing nothing, so
        there is no durable claim behind the athlete it proposed.
        """

        if self.mapping is None or self.state not in CLAIMING_MAPPING_STATES:
            return None
        return self.mapping.canonical_player_id


@dataclass(frozen=True, slots=True)
class MappingPersistenceResult:
    """Outcome of one auto-observation or manual decision."""

    state: str
    persisted: bool
    mapping: ProviderAthleteMappingRecord | None = None
    decision: MappingDecisionRecord | None = None

    def board_outcome(self, resolution: AthleteResolution) -> BoardMappingOutcome:
        """Restate one resolution as the governed state this write reached."""

        state = MappingResolutionState(self.state)
        return BoardMappingOutcome(
            resolution=resolution,
            state=state,
            persisted=self.persisted,
            mapping=self._claimed_mapping(state),
        )

    def _claimed_mapping(
        self, state: MappingResolutionState
    ) -> ProviderAthleteMappingRecord | None:
        """The stored mapping, but only while it still asserts this claim.

        A governing read returns the row it found alongside the state that now
        holds, and those disagree exactly when the identity failed closed: a
        rejection leaves the deactivated row it suppressed, and a conflict
        leaves the athlete the mapping used to name.  Handing that row on would
        let a caller compare against a canonical athlete governance has already
        taken out of comparisons, so it is withheld unless the row itself is
        active and says the same thing the governed state does.
        """

        mapping = self.mapping
        if mapping is None or state not in CLAIMING_MAPPING_STATES:
            return None
        if not mapping.is_active or mapping.mapping_state != state.value:
            return None
        return mapping


class AthleteMappingRepository:
    """Own durable current state, append-only decisions, and suppressions."""

    _identity_locks: dict[tuple[int, str, str], threading.RLock] = {}
    _identity_locks_guard = threading.Lock()

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Any | None = None,
        projection_replay: Any | None = None,
    ) -> None:
        self.engine = engine
        if is_demo_database_url(str(getattr(engine, "url", ""))):
            raise InvalidConfigurationError(
                "The bundled demo database is read-only and cannot store athlete mappings."
            )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._projection_replay = projection_replay

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

    @_translate_storage_failures
    def get_mappings(
        self, provider: str, provider_athlete_ids: Iterable[str]
    ) -> dict[str, ProviderAthleteMappingRecord]:
        """Return the stored mapping of every named identity, keyed by ID.

        One board names hundreds of identities, so it reads them in one
        statement rather than one per market.  An identity with no row is
        absent from the result.
        """

        provider, provider_athlete_ids = _normalized_keys(provider, provider_athlete_ids)
        if not provider_athlete_ids:
            return {}
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(ProviderAthleteMapping.__table__).where(
                    and_(
                        ProviderAthleteMapping.provider == provider,
                        ProviderAthleteMapping.provider_athlete_id.in_(provider_athlete_ids),
                    )
                )
            ).mappings().all()
        return {
            str(row["provider_athlete_id"]): self._mapping_record(row) for row in rows
        }

    @_translate_storage_failures
    def get_rejections(
        self, provider: str, provider_athlete_ids: Iterable[str]
    ) -> dict[str, MappingRejectionRecord]:
        """Return the stored rejection of every named identity, keyed by ID."""

        provider, provider_athlete_ids = _normalized_keys(provider, provider_athlete_ids)
        if not provider_athlete_ids:
            return {}
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(AthleteMappingRejection.__table__).where(
                    and_(
                        AthleteMappingRejection.provider == provider,
                        AthleteMappingRejection.provider_athlete_id.in_(provider_athlete_ids),
                    )
                )
            ).mappings().all()
        return {
            str(row["provider_athlete_id"]): self._rejection_record(row) for row in rows
        }

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
        """Return mapping rows, never those whose current state is a conflict.

        A conflict is inactive, so the active-only listing already omits it, but
        the full listing would otherwise name the same identity twice: once here
        without its evidence and once in ``list_conflicts`` with both canonical
        sides.  The conflict queue elaborates it exactly once, so the row is
        excluded here while every other inactive row stays visible.
        """

        statement = select(ProviderAthleteMapping.__table__).where(
            ProviderAthleteMapping.mapping_state
            != MappingResolutionState.MAPPING_CONFLICT.value
        )
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

    @_translate_storage_failures
    def list_conflicts(
        self,
        *,
        provider: str | None = None,
    ) -> list[MappingConflictRecord]:
        """Return each identity whose current state is a mapping conflict.

        A conflict is inactive by construction, so it is absent from the
        active-only mapping listing, and it is not an unresolved observation
        state, so it is absent from that queue too.  Without its own queue an
        operator would see nothing left to act on for the one state that always
        requires an operator.  The conflicting row is paired with the decision
        that recorded the conflict rather than repeated as a mapping, so the
        queue names both canonical sides and the evidence behind them exactly
        once.
        """

        statement = select(ProviderAthleteMapping.__table__).where(
            ProviderAthleteMapping.mapping_state
            == MappingResolutionState.MAPPING_CONFLICT.value
        )
        if provider is not None:
            normalized_provider, _ = _normalized_key(provider, "_placeholder")
            statement = statement.where(
                ProviderAthleteMapping.provider == normalized_provider
            )
        statement = statement.order_by(
            ProviderAthleteMapping.provider,
            ProviderAthleteMapping.provider_athlete_id,
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
            return [
                MappingConflictRecord(
                    mapping=self._mapping_record(row),
                    latest_decision=self._decision_result(
                        connection,
                        self._latest_conflict_decision(
                            connection, row["provider"], row["provider_athlete_id"]
                        ),
                    ),
                )
                for row in rows
                if row is not None
            ]

    # -- automatic board observations -------------------------------------

    def persist_auto_decision(
        self,
        resolution: AthleteResolution,
        *,
        recorded_at: datetime | None = None,
    ) -> MappingPersistenceResult:
        """Persist the first qualifying automatic result transactionally.

        The unique source identity and idempotency key make repeated board
        reads harmless.  Existing manual decisions are never overwritten.
        """

        if not isinstance(resolution, AthleteResolution):
            raise TypeError("resolution must be AthleteResolution")
        if not resolution.is_auto_qualifying:
            if resolution.state is MappingResolutionState.MAPPING_CONFLICT:
                return self._persist_mapping_conflict(resolution, recorded_at=recorded_at)
            if resolution.state in UNRESOLVED_OBSERVATION_STATES:
                return self._persist_unresolved_observation(
                    resolution, recorded_at=recorded_at
                )
            if resolution.state.value in _MANUAL_MAPPING_STATES:
                return self._observe_governed_mapping(resolution)
            return MappingPersistenceResult(resolution.state.value, False)
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
                if state == MappingResolutionState.MAPPING_CONFLICT.value:
                    return MappingPersistenceResult(
                        state,
                        False,
                        mapping=self._mapping_record(existing),
                    )
                previous_id = existing["canonical_player_id"]
                # Only a mapping that still asserts an automatic canonical
                # claim can disagree with this observation.  A row left behind
                # by a cleared rejection is a decided-and-undecided history,
                # not a live claim, so the canonical ID it still carries must
                # not queue a conflict against fresh evidence.
                if (
                    AthleteResolver._asserts_automatic_claim(existing)
                    and previous_id is not None
                    and int(previous_id) != resolution.canonical_player_id
                ):
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
                        # Whatever this row was before, it is now an automatic
                        # mapping, so it names no conflict for an operator.
                        conflict_canonical_player_id=None,
                        conflict_canonical_name=None,
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
                    if existing["mapping_state"] in _MANUAL_MAPPING_STATES:
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

    def _observe_governed_mapping(
        self,
        resolution: AthleteResolution,
    ) -> MappingPersistenceResult:
        """Record that the board still observes the operator's own identity.

        The resolver takes the operator's mapping for provider evidence that
        agrees with it, so there is nothing to append and nothing to change:
        the mapping already says this, and a duplicate audit row would only
        repeat the decision the operator made.  The read itself is still news
        about the identity, though -- the provider reported the approved
        athlete at that instant -- so it runs through the same serialized
        transaction as every other observation and raises the identity's
        observation clock.  A conflicting read taken earlier is then recognized
        as describing evidence the provider has since replaced, instead of
        unseating a governed mapping on the strength of a stale read.
        """

        provider, provider_id = self._resolution_key(resolution)
        with self._transaction(provider, provider_id) as connection:
            stale = self._stale_observation(connection, provider, provider_id, resolution)
            if stale is not None:
                return stale
            self._advance_observation_clock(connection, provider, provider_id, resolution)
            # The governing state is re-read inside the lock: an operator may
            # have rejected or remapped the identity while this read was in
            # flight, and the caller is told what actually governs it now.
            governed = self._governing_decision(connection, provider, provider_id)
            if governed is not None:
                return governed
            mapping = self._select_mapping(connection, provider, provider_id, lock=True)
            return MappingPersistenceResult(
                resolution.state.value if mapping is None else str(mapping["mapping_state"]),
                False,
                mapping=self._mapping_record(mapping),
            )

    def _persist_unresolved_observation(
        self,
        resolution: AthleteResolution,
        *,
        recorded_at: datetime | None,
    ) -> MappingPersistenceResult:
        """Retain one typed unresolved observation in the audit log.

        No current mapping row is created because no canonical identity was
        established; the idempotency key keeps repeated board reads to one
        durable observation per distinct evidence shape.  Some states do change
        an existing mapping, because an identity cannot stay mapped while the
        board says it should not be compared: team-conflict evidence promotes
        it to a conflict for an operator to review, and
        ``_WITHDRAWING_OBSERVATION_STATES`` withdraw it from comparisons -- an
        already withdrawn row included, so its state keeps naming the reason
        that currently holds.  The
        candidates and evidence stay on the appended observation either way, so
        the operator's unresolved queue keeps everything the decision was made
        on.
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
            if resolution.state in _WITHDRAWING_OBSERVATION_STATES:
                self._withdraw_claimed_mapping(
                    connection,
                    provider,
                    provider_id,
                    state=resolution.state,
                    now=now,
                )
            if resolution.state is MappingResolutionState.TEAM_CONFLICT:
                # The active mapping has to be read inside this serialized
                # transaction.  A lookup taken before the lock can miss an
                # automatic mapping a concurrent board read commits in the
                # meantime, which would leave the identity actively mapped
                # while only an observation records the disagreement.
                active = self._select_mapping(connection, provider, provider_id, lock=True)
                if active is not None and bool(active["is_active"]):
                    return self._write_conflict(connection, active, resolution, now=now)
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

    def _withdraw_claimed_mapping(
        self,
        connection: Connection,
        provider: str,
        provider_id: str,
        *,
        state: MappingResolutionState,
        now: datetime,
    ) -> None:
        """Withdraw a claimed mapping this evidence can no longer support.

        The resolver found the athlete only as inactive for the requested
        season, found two equally exact ones and could choose neither, or found
        the claimed athlete gone from the catalog, so the mapping must not
        reach a board comparison.  The row is kept -- it is still the durable
        record of which canonical athlete this provider identity was mapped to
        -- but it is deactivated, and its state records why, so a later
        unambiguous observation can simply map it again.

        An already withdrawn row is withdrawn again for a *different* reason:
        the operator reads the current row to decide, so it has to say why the
        identity is out of comparisons now rather than why it first left them.
        Repeating the state it already holds writes nothing, which keeps a
        repeated observation a no-op beyond the identity's observation clock.

        Only a claim the board established is withdrawn.  An operator's
        decision is governed elsewhere, and a conflict already awaits an
        operator, so neither is moved by evidence like this.
        """

        existing = self._select_mapping(connection, provider, provider_id, lock=True)
        if existing is None:
            return
        current = str(existing["mapping_state"])
        if current == state.value:
            return
        claimed = current == MappingResolutionState.AUTO.value and bool(
            existing["is_active"]
        )
        if not claimed and current not in WITHDRAWN_CLAIM_STATES:
            return
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
                is_active=False,
                # A withdrawal is not a disagreement between two canonical
                # athletes, so the row names no conflict for an operator.
                conflict_canonical_player_id=None,
                conflict_canonical_name=None,
                last_seen_at=now,
            )
        )

    def record_resolution(
        self,
        resolution: AthleteResolution,
        *,
        recorded_at: datetime | None = None,
    ) -> MappingPersistenceResult:
        """Persist a qualifying result or a later evidence conflict."""

        try:
            return self.persist_auto_decision(resolution, recorded_at=recorded_at)
        except SQLAlchemyError as exc:
            raise AthleteMappingPersistenceError(DEFAULT_MAPPING_FAILURE_SUMMARY) from exc

    def _persist_mapping_conflict(
        self,
        resolution: AthleteResolution,
        *,
        recorded_at: datetime | None,
    ) -> MappingPersistenceResult:
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
            if self._is_repeated_conflict(existing, resolution):
                # The resolver read the conflict this row already records, so
                # there is nothing new to append and the retained conflict
                # candidate must not be overwritten with an empty one.
                return MappingPersistenceResult(
                    MappingResolutionState.MAPPING_CONFLICT.value,
                    False,
                    mapping=self._mapping_record(existing),
                )
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
        resolution: AthleteResolution,
        *,
        now: datetime,
        reason: str = "mapping_conflict",
        promoting_conflict: bool = False,
    ) -> MappingPersistenceResult:
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
                # Evidence that names no canonical athlete cannot erase the
                # conflicting candidate an operator still has to review.
                conflict_canonical_player_id=(
                    resolution.canonical_player_id
                    if resolution.canonical_athlete
                    else existing.get("conflict_canonical_player_id")
                ),
                conflict_canonical_name=(
                    resolution.canonical_athlete.display_name
                    if resolution.canonical_athlete
                    else existing.get("conflict_canonical_name")
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
            reason=reason,
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
                        # A suppressed identity has no conflict left to review.
                        conflict_canonical_player_id=None,
                        conflict_canonical_name=None,
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
            selected = CanonicalAthlete.from_row(canonical)
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
            if decision is not None and not selected.is_active_for_season:
                # The operator chose a row automatic mapping would refuse, so
                # the audit has to name it as an inactive selection.
                self._insert_candidates(connection, int(decision["id"]), (selected,))
            mapping = self._select_mapping(connection, provider, provider_id)
            result = MappingPersistenceResult(
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
                    "projection replay failed after athlete mapping commit",
                    extra={
                        "provider": provider,
                        "provider_athlete_id": provider_id,
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
        resolution: AthleteResolution | None = None,
    ) -> MappingPersistenceResult | None:
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
        governed decision still contradicts the observed evidence, so a
        conflict an operator has already resolved cannot undo the resolution.
        An active rejection governs either way: a suppressed identity has
        nothing to conflict with.
        """

        rejection = self._select_rejection(connection, provider, provider_id, lock=True)
        mapping = self._select_mapping(connection, provider, provider_id, lock=True)
        if rejection is not None and rejection["is_active"]:
            return MappingPersistenceResult(
                MappingResolutionState.REJECTED.value,
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
                    connection,
                    provider,
                    provider_id,
                    mapping,
                    resolution,
                )
            )
        ):
            return MappingPersistenceResult(
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
        resolution: AthleteResolution,
    ) -> MappingPersistenceResult | None:
        """Return the current state when this read predates the governing one.

        A provider read can be slow, retried, or replayed, so an observation
        may land long after the identity moved on: rejected and cleared by an
        operator, or mapped again from a newer read.  Landing it then would
        deactivate the newer mapping and queue a conflict between two
        canonical athletes that were never claimed at the same time.

        The read is therefore ordered by when the provider was observed
        against the newest governing instant for the identity -- an operator
        decision's ``created_at``, and the identity's observation clock.  Both
        are UTC instants of the event itself, never a persistence time.  An
        observation contemporaneous with the governing instant is not from
        before it, so only a strictly earlier read is fenced and replaying the
        same read stays idempotent.  A caller that reports no observation
        instant cannot be ordered and is left alone.
        """

        observed_at = resolution.observed_at
        if observed_at is None:
            return None
        governing = cls._governing_observation_instant(connection, provider, provider_id)
        if governing is None or _utc(observed_at) >= governing:
            return None
        mapping = cls._select_mapping(connection, provider, provider_id, lock=True)
        state = (
            resolution.state.value
            if mapping is None
            else str(mapping["mapping_state"])
        )
        return MappingPersistenceResult(state, False, mapping=cls._mapping_record(mapping))

    @classmethod
    def _governing_observation_instant(
        cls,
        connection: Connection,
        provider: str,
        provider_id: str,
    ) -> datetime | None:
        """Return the newest instant this identity was decided or observed."""

        decided_at = connection.execute(
            select(AthleteMappingDecision.created_at)
            .where(
                and_(
                    AthleteMappingDecision.provider == provider,
                    AthleteMappingDecision.provider_athlete_id == provider_id,
                    AthleteMappingDecision.decision_state.in_(_OPERATOR_DECISION_STATES),
                )
            )
            .order_by(AthleteMappingDecision.created_at.desc())
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
            select(AthleteMappingLock.last_observed_at).where(
                and_(
                    AthleteMappingLock.provider == provider,
                    AthleteMappingLock.provider_athlete_id == provider_id,
                )
            )
        ).scalar()

    @classmethod
    def _advance_observation_clock(
        cls,
        connection: Connection,
        provider: str,
        provider_id: str,
        resolution: AthleteResolution,
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
            update(AthleteMappingLock.__table__)
            .where(
                and_(
                    AthleteMappingLock.provider == provider,
                    AthleteMappingLock.provider_athlete_id == provider_id,
                )
            )
            .values(last_observed_at=observed_at)
        )

    @classmethod
    def _conflict_still_applies(
        cls,
        connection: Connection,
        provider: str,
        provider_id: str,
        mapping: Mapping[str, Any],
        resolution: AthleteResolution | None,
    ) -> bool:
        """Report whether observed evidence still contradicts the manual state.

        A conflict is resolved outside this transaction: an operator can
        approve or override the very identity the conflict reports, and the
        stale read may only land afterwards.  Only the provider-side evidence
        decides, and it is compared with the governing manual decision read
        inside this transaction -- the operator who recorded it saw exactly
        that evidence.  Observation order cannot stand in for that comparison:
        a read taken after the decision may still carry evidence the operator
        never reviewed.  Ordering only fences reads from before the decision,
        and ``_stale_observation`` has already applied it.

        The canonical choice is deliberately not compared.  An operator who
        accepts the provider's observation may still keep or pick a canonical
        athlete the automatic side would not have chosen -- that disagreement
        is the decision, not a conflict -- so a stale automatic candidate must
        never undo it.  Evidence the governed decision does contradict is
        evidence nobody reviewed, and still fails closed.

        A contradiction asserts several evidences at once, and any one of them
        the operator never reviewed unseats the approval, so all of them are
        compared.  One of a snapshot's contradicting markets may well report
        exactly the approved athlete; letting that one answer for the identity
        would leave the mapping active on evidence the very same read disputes,
        and which market was listed first would decide.
        """

        if resolution is None:
            return True
        governed = cls._latest_manual_decision(connection, provider, provider_id) or mapping
        return any(
            AthleteResolver._manual_evidence_conflicts(governed, evidence)
            for evidence in resolution.asserted_evidence
        )

    @staticmethod
    def _latest_manual_decision(
        connection: Connection,
        provider: str,
        provider_id: str,
    ) -> Mapping[str, Any] | None:
        return connection.execute(
            select(AthleteMappingDecision.__table__)
            .where(
                and_(
                    AthleteMappingDecision.provider == provider,
                    AthleteMappingDecision.provider_athlete_id == provider_id,
                    AthleteMappingDecision.decision_state.in_(_MANUAL_MAPPING_STATES),
                )
            )
            .order_by(AthleteMappingDecision.id.desc())
            .limit(1)
        ).mappings().one_or_none()

    @staticmethod
    def _latest_conflict_decision(
        connection: Connection,
        provider: str,
        provider_id: str,
    ) -> Mapping[str, Any] | None:
        return connection.execute(
            select(AthleteMappingDecision.__table__)
            .where(
                and_(
                    AthleteMappingDecision.provider == provider,
                    AthleteMappingDecision.provider_athlete_id == provider_id,
                    AthleteMappingDecision.decision_state
                    == MappingResolutionState.MAPPING_CONFLICT.value,
                )
            )
            .order_by(AthleteMappingDecision.id.desc())
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
        """Read one catalog row for a season, active for the season or not.

        Automatic mapping still requires an active season row because the
        resolver only ever matches those.  A governed approve or override may
        select an inactive-only row, which the decision audit records
        explicitly as a candidate that was not active for the season.
        """

        return connection.execute(
            select(AthleteCatalog.__table__).where(
                and_(
                    AthleteCatalog.season == season,
                    AthleteCatalog.player_id == player_id,
                )
            )
        ).mappings().one_or_none()

    @staticmethod
    def _latest_decision(
        connection: Connection,
        provider: str,
        provider_id: str,
    ) -> Mapping[str, Any] | None:
        return connection.execute(
            select(AthleteMappingDecision.__table__)
            .where(
                and_(
                    AthleteMappingDecision.provider == provider,
                    AthleteMappingDecision.provider_athlete_id == provider_id,
                )
            )
            .order_by(AthleteMappingDecision.id.desc())
            .limit(1)
        ).mappings().one_or_none()

    @staticmethod
    def _latest_observed_evidence(
        connection: Connection,
        provider: str,
        provider_id: str,
    ) -> Mapping[str, Any] | None:
        """Read the newest board observation that carries provider evidence.

        Rejections, clearings, and evidence-free observations are skipped, so
        an identity that was observed, rejected, cleared, and then approved
        still records what the provider actually reported for it.
        """

        evidence_columns = (
            AthleteMappingDecision.provider_name,
            AthleteMappingDecision.provider_team_id,
            AthleteMappingDecision.provider_team_canonical_id,
            AthleteMappingDecision.provider_team_name,
            AthleteMappingDecision.provider_team_abbreviation,
        )
        return connection.execute(
            select(AthleteMappingDecision.__table__)
            .where(
                and_(
                    AthleteMappingDecision.provider == provider,
                    AthleteMappingDecision.provider_athlete_id == provider_id,
                    AthleteMappingDecision.decision_state.in_(
                        _OBSERVED_EVIDENCE_DECISION_STATES
                    ),
                    or_(*(column.isnot(None) for column in evidence_columns)),
                )
            )
            .order_by(AthleteMappingDecision.id.desc())
            .limit(1)
        ).mappings().one_or_none()

    @staticmethod
    def _latest_mapping_evidence_decision_id(
        connection: Connection,
        provider: str,
        provider_id: str,
    ) -> int:
        """Return the ID of the decision the mapping row's evidence came from.

        Zero when no decision ever wrote evidence onto the row, which leaves
        any durable board observation newer than it.
        """

        decision_id = connection.execute(
            select(AthleteMappingDecision.id)
            .where(
                and_(
                    AthleteMappingDecision.provider == provider,
                    AthleteMappingDecision.provider_athlete_id == provider_id,
                    AthleteMappingDecision.decision_state.in_(
                        _MAPPING_EVIDENCE_DECISION_STATES
                    ),
                )
            )
            .order_by(AthleteMappingDecision.id.desc())
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
        provider_evidence: AthleteEvidence | None,
    ) -> dict[str, Any]:
        """Choose the typed provider evidence a manual decision records.

        An operator who supplies no evidence is not discarding what the
        provider reported, so the freshest evidence for the identity is carried
        forward -- and freshest is a question of order, not of which table it
        sits in.  A governing manual mapping is authoritative: an operator saw
        that evidence and no board read may supersede it.  Otherwise the newest
        durable board observation wins whenever it postdates the decision that
        last wrote the mapping row, because a rejected or otherwise stale
        mapping keeps reporting evidence the board has since replaced.  Both
        sides are read inside the same identity transaction so the choice
        cannot straddle a concurrent write.
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
        resolution: AthleteResolution,
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
        candidates: tuple[CanonicalAthlete, ...],
    ) -> None:
        """Retain the candidates an operator has to choose between.

        Only observations whose canonical identity is still open carry
        candidates -- unresolved evidence that established none, and a conflict
        whose canonical side is exactly what is disputed.  A decided mapping
        already names its canonical athlete.
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
    def _insert_contradictory_evidence(
        connection: Connection,
        decision_id: int,
        evidence: tuple[AthleteEvidence, ...],
    ) -> None:
        """Retain every evidence a contradictory observation asserted.

        The decision row records one representative evidence, so without these
        rows the rest of the contradiction would exist only in the observation
        that produced it and would be missing from the operator's conflict
        queue and the identity's history.  The board already orders the
        evidence deterministically, and the stored index keeps that order.
        """

        if not evidence:
            return
        connection.execute(
            insert(AthleteMappingDecisionContradiction.__table__),
            [
                {
                    "decision_id": decision_id,
                    "evidence_index": index,
                    "provider_athlete_id": item.provider_id,
                    "provider_canonical_id": item.canonical_id,
                    "provider_name": item.name,
                    "provider_team_id": item.team.provider_id if item.team else None,
                    "provider_team_canonical_id": (
                        item.team.canonical_id if item.team else None
                    ),
                    "provider_team_name": item.team.name if item.team else None,
                    "provider_team_abbreviation": (
                        item.team.abbreviation if item.team else None
                    ),
                }
                for index, item in enumerate(evidence)
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
        observed_at: datetime | None = None,
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
            "observed_at": None if observed_at is None else _utc(observed_at),
            "created_at": created_at,
        }
        result = connection.execute(
            insert(AthleteMappingDecision.__table__).values(**values)
        )
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

    @classmethod
    def _is_repeated_conflict(
        cls,
        existing: Mapping[str, Any] | None,
        resolution: AthleteResolution,
    ) -> bool:
        """Report whether a conflict read repeats the recorded conflict.

        A resolver that starts from an existing conflict row carries no
        canonical athlete, so writing it again would only clear the retained
        conflict candidate and append a duplicate decision.

        Evidence the read simply does not carry is not a difference: one market
        of a snapshot may report a team another omits, and reading the same
        snapshot again in another order would otherwise overwrite the row with
        the sparser observation and append a conflict per read.  Only a fact
        the read actually asserts is compared, so a repeat is a read that adds
        nothing the row does not already record.
        """

        if existing is None or resolution.canonical_athlete is not None:
            return False
        if existing["mapping_state"] != MappingResolutionState.MAPPING_CONFLICT.value:
            return False
        values = cls._resolution_values(resolution)
        return all(
            values[field] is None or existing[field] == values[field]
            for field in (
                "provider_name",
                "provider_team_id",
                "provider_team_canonical_id",
                "provider_team_name",
                "provider_team_abbreviation",
            )
        )

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
            # Changed candidate evidence is different evidence for an operator
            # to review, so every typed candidate field the audit retains is
            # part of the fingerprint rather than the player ID alone.
            "candidates": sorted(
                (
                    [
                        candidate.player_id,
                        candidate.display_name,
                        candidate.team_id,
                        candidate.team_name,
                        candidate.team_abbreviation,
                        candidate.is_active_for_season,
                    ]
                    for candidate in resolution.candidates
                ),
                key=lambda candidate: candidate[0],
            ),
            # A contradiction is fingerprinted by everything it contradicts,
            # not only the evidence it happened to be recorded on, and sorting
            # it keeps a snapshot that lists the same markets in another order
            # the same observation rather than a new one.
            "contradictory_evidence": sorted(
                (
                    [
                        item.name,
                        item.canonical_id,
                        item.team.provider_id if item.team else None,
                        item.team.canonical_id if item.team else None,
                        item.team.name if item.team else None,
                        item.team.abbreviation if item.team else None,
                    ]
                    for item in resolution.contradictory_evidence
                ),
                key=lambda item: [str(value) for value in item],
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
        (record,) = cls._decision_records(connection, [row])
        return record

    @classmethod
    def _decision_records(
        cls,
        connection: Connection,
        rows: Any,
    ) -> list[MappingDecisionRecord]:
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
    def _select_contradictory_evidence(
        connection: Connection,
        decision_ids: list[int],
    ) -> dict[int, tuple[MappingEvidenceRecord, ...]]:
        if not decision_ids:
            return {}
        rows = connection.execute(
            select(AthleteMappingDecisionContradiction.__table__)
            .where(AthleteMappingDecisionContradiction.decision_id.in_(decision_ids))
            .order_by(
                AthleteMappingDecisionContradiction.decision_id,
                AthleteMappingDecisionContradiction.evidence_index,
            )
        ).mappings().all()
        evidence: dict[int, tuple[MappingEvidenceRecord, ...]] = {}
        for row in rows:
            decision_id = int(row["decision_id"])
            evidence[decision_id] = evidence.get(decision_id, ()) + (
                MappingEvidenceRecord(
                    provider_athlete_id=row["provider_athlete_id"],
                    provider_canonical_id=row["provider_canonical_id"],
                    provider_name=row["provider_name"],
                    provider_team_id=row["provider_team_id"],
                    provider_team_canonical_id=row["provider_team_canonical_id"],
                    provider_team_name=row["provider_team_name"],
                    provider_team_abbreviation=row["provider_team_abbreviation"],
                ),
            )
        return evidence

    @staticmethod
    def _decision_record(
        row: Mapping[str, Any] | None,
        candidates: tuple[MappingCandidateRecord, ...] = (),
        contradictory_evidence: tuple[MappingEvidenceRecord, ...] = (),
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
            observed_at=_iso(row["observed_at"]),
            created_at=_iso(row["created_at"]) or "",
            candidates=candidates,
            contradictory_evidence=contradictory_evidence,
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
    "CLAIMING_MAPPING_STATES",
    "UNRESOLVED_OBSERVATION_STATES",
    "AthleteMappingRepository",
    "AthleteMappingPersistenceError",
    "BoardMappingOutcome",
    "MappingCandidateRecord",
    "MappingConflictRecord",
    "MappingDecisionRecord",
    "MappingEvidenceRecord",
    "MappingPersistenceResult",
    "MappingRejectionRecord",
    "ProviderAthleteMappingRecord",
]
