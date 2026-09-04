"""Durable format rebuilds of the traditional-opponent publication family.

This is the stateful half of the family that
``traditional_opponent_publications`` defines.  It is a separate module only
because it reaches the control plane, which in turn reaches the pure format
core; keeping them in one file would close that import cycle.  Conceptually
they are one deep module: formats, invariants, composition, promotion, and
rollback all belong to the family and to nothing else.

A *publication rebuild* regenerates every window of the family from Canonical
Game Ledger facts that did not change, because the rendered format changed.
It is deliberately its own lifecycle operation:

* it is not a composition job -- those are created by new or corrected
  observations, and the job for this cutoff may already be permanently
  successful;
* it is not failed-data repair -- nothing failed and no data is wrong;
* it is not initial parity activation -- that gate compared against a legacy
  diagnostic that no longer exists, and a format rebuild must not fabricate
  new legacy parity evidence for a cutover that already happened.

Overloading any of those would make operational history describe an event that
never occurred.

The operation is durable, leased, generation-fenced, and observable through
six bounded phases.  Composition and validation run without holding pointer
locks; only the final promotion opens a short transaction that locks both
pointers and revalidates every precondition before either moves.  Season and
Last 15 promote together or neither moves, so the product never observes a
mixed-format family.  A legitimate ledger correction always wins the race: it
changes the source or pointer state, and the rebuild terminates as stale and
requires a new operator request.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.domain.slate_time import slate_date_for_instant
from app.models.canonical_game_ledger import CanonicalGameLedgerGame
from app.models.collection_control import (
    CollectionManifest,
    CollectionObservation,
    PublicationObservation,
    PublicationPointer,
    PublicationRebuild,
    PublicationVersion,
)
from app.services.collection_control import (
    ControlPlaneError,
    FamilyPromotion,
    PublicationService,
    _aware,
    _uuid,
    utcnow,
)
from app.services.database_first_activation import (
    PublicationPayloadError,
    decode_team_window,
)
from app.services.ledger_derivations import window_ledger_checksum
from app.services.traditional_opponent_publications import (
    SUPPORTED_TRADITIONAL_OPPONENT_FORMATS,
    TRADITIONAL_OPPONENT_FAMILY,
    TRADITIONAL_OPPONENT_STREAM_KEYS,
    TRADITIONAL_OPPONENT_TARGET_FORMAT,
    TraditionalOpponentFormatError,
    normalize_traditional_opponent_window,
    traditional_opponent_window_kind,
)

SEASON_STREAM, L15_STREAM = TRADITIONAL_OPPONENT_STREAM_KEYS

#: The phases an operator may observe.  Nothing else is ever reported.
ACTIVE_REBUILD_STATES = ("queued", "composing", "validating", "promoting")
TERMINAL_REBUILD_STATES = ("succeeded", "failed")
REBUILD_STATES = ACTIVE_REBUILD_STATES + TERMINAL_REBUILD_STATES

#: How long one worker holds a rebuild before another may recover it.
DEFAULT_LEASE_SECONDS = 300


@dataclass(frozen=True, slots=True)
class FamilyExpectation:
    """The active pair and fences the operator approved this rebuild against.

    Every one of these is revalidated at promotion.  They are the whole
    concurrency contract: an accepted correction that moves either pointer
    makes the approved request stale rather than silently overwritten.
    """

    season_publication_id: str
    season_fence: int
    l15_publication_id: str
    l15_fence: int

    def for_stream(self, stream_key: str) -> tuple[str, int]:
        if stream_key == SEASON_STREAM:
            return self.season_publication_id, int(self.season_fence)
        if stream_key == L15_STREAM:
            return self.l15_publication_id, int(self.l15_fence)
        raise TraditionalOpponentFormatError("publication_family_mismatch")


@dataclass(frozen=True, slots=True)
class RebuildClaim:
    """Proof that one worker holds one rebuild at one exact generation.

    A worker command is invoked many times and every invocation shares its
    owner name, so a name alone cannot distinguish an abandoned pass from the
    pass that replaced it.  Claiming therefore advances the row's lease
    generation and hands back the value it advanced to; that number is the
    capability every later write must present.  An abandoned worker still
    holds the previous number and can no longer write anything.
    """

    rebuild_id: str
    owner: str
    generation: int


@dataclass(frozen=True, slots=True)
class RebuildSources:
    """The unchanged authority and facts a rebuild composes from.

    A rebuild never widens its own authority: season, cutoff, governed games,
    and the league roster all come from the active pair being replaced.
    """

    season: str
    cutoff: datetime
    as_of: date
    target_format: Any
    stream_keys: tuple[str, ...]
    governed_game_ids: tuple[str, ...]
    team_game_ids: Mapping[str, Mapping[int, frozenset[str]]]
    team_ids: frozenset[int]
    source_checksum: str


class TraditionalOpponentRebuildService:
    """Start, resume, observe, and roll back traditional-opponent rebuilds."""

    family = TRADITIONAL_OPPONENT_FAMILY
    stream_keys = TRADITIONAL_OPPONENT_STREAM_KEYS

    def __init__(
        self,
        engine: Engine,
        *,
        publication_service: PublicationService | None = None,
        compose: Callable[[RebuildSources], Mapping[str, Any]] | None = None,
        clock: Callable[[], datetime] = utcnow,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.engine = engine
        self.publications = publication_service or PublicationService(
            engine, clock=clock
        )
        self.compose = compose or ledger_composer(engine)
        self.clock = clock
        self.lease_seconds = int(lease_seconds)

    # --- Starting ---------------------------------------------------------

    def start(
        self,
        *,
        actor: str,
        reason: str,
        expected: FamilyExpectation,
        season: str | None = None,
        cutoff: datetime | None = None,
        session: Session | None = None,
    ) -> PublicationRebuild:
        """Record one durable rebuild, or return the one already recorded.

        ``season`` and ``cutoff``, when supplied, are assertions against the
        active pair.  They are never replacement authority: a request cannot
        widen what a rebuild is allowed to touch.
        """

        actor = str(actor or "").strip()[:128]
        if not actor:
            raise ControlPlaneError("actor_required")
        reason = str(reason or "").strip()
        if len(reason) < 3:
            raise ControlPlaneError("reason_required")
        target = TRADITIONAL_OPPONENT_TARGET_FORMAT
        request_checksum = self._request_checksum(
            actor=actor, reason=reason, expected=expected, target=target
        )
        now = self.clock()
        with self.publications._session_scope(session) as session:
            # Idempotency is decided from the request alone, before any live
            # state is read: a retry of a completed request must return its
            # receipt rather than fail against generations it already moved.
            active = self._latest(session, states=ACTIVE_REBUILD_STATES)
            if active is not None:
                if active.request_checksum == request_checksum:
                    return active
                raise ControlPlaneError("duplicate_active_operation")
            completed = self._latest(
                session, states=("succeeded",), request_checksum=request_checksum
            )
            if completed is not None:
                return completed
            authority = self._resolve_authority(
                session, expected=expected, season=season, cutoff=cutoff
            )
            rebuild = PublicationRebuild(
                rebuild_id=_uuid(),
                family=self.family,
                target_format=target.name,
                target_fingerprint=target.fingerprint,
                actor=actor,
                reason=reason[:255],
                request_checksum=request_checksum,
                expected_season_publication_id=expected.season_publication_id,
                expected_season_fence=int(expected.season_fence),
                expected_l15_publication_id=expected.l15_publication_id,
                expected_l15_fence=int(expected.l15_fence),
                season=authority["season"],
                cutoff=authority["cutoff"],
                manifest_id=authority["manifest_id"],
                event_catalog_publication_id=authority[
                    "event_catalog_publication_id"
                ],
                event_catalog_checksum=authority["event_catalog_checksum"],
                source_checksum=authority["source_checksum"],
                state="queued",
                attempts=0,
                generation=1,
                created_at=now,
                updated_at=now,
            )
            session.add(rebuild)
            session.flush()
        return rebuild

    # --- Working ----------------------------------------------------------

    def run(self, rebuild_id: str, *, owner: str = "worker") -> PublicationRebuild:
        """Compose, validate, stage, and promote one rebuild to completion."""

        staged = self.stage(rebuild_id, owner=owner)
        if staged.state in TERMINAL_REBUILD_STATES:
            return staged
        return self.promote(rebuild_id, owner=owner)

    def claimable(self, *, session: Session | None = None) -> tuple[str, ...]:
        """The unfinished rebuilds this worker could pick up right now.

        A rebuild is claimable when nobody holds it or the holder's lease has
        expired.  That is the whole restart story: the operation lives in the
        database, so a worker that died mid-phase leaves a row whose lease
        simply runs out, and the next pass resumes it from its recorded phase
        rather than from the beginning.
        """

        now = self.clock()
        with self.publications._session_scope(session) as session:
            rows = session.scalars(
                select(PublicationRebuild)
                .where(
                    PublicationRebuild.family == self.family,
                    PublicationRebuild.state.in_(ACTIVE_REBUILD_STATES),
                )
                .order_by(PublicationRebuild.created_at)
            ).all()
            return tuple(
                row.rebuild_id
                for row in rows
                if not row.lease_owner
                or row.lease_expires_at is None
                or _aware(row.lease_expires_at) <= now
            )

    def run_pending(
        self, *, owner: str = "worker", limit: int | None = None
    ) -> tuple[PublicationRebuild, ...]:
        """Drive every claimable rebuild of this family to a terminal state.

        This is the execution seam an operator command or scheduled pass
        calls.  Starting a rebuild only records the approved intent; this is
        what makes it progress.  A rebuild another worker claimed between the
        scan and the claim is skipped rather than contended for -- it is
        already being driven.
        """

        claimable = self.claimable()
        if limit is not None:
            claimable = claimable[: max(int(limit), 0)]
        finished = []
        for rebuild_id in claimable:
            try:
                finished.append(self.run(rebuild_id, owner=owner))
            except ControlPlaneError as error:
                if error.reason == "rebuild_lease_held":
                    continue
                raise
        return tuple(finished)

    def stage(self, rebuild_id: str, *, owner: str = "worker") -> PublicationRebuild:
        """Compose and validate both candidates without touching a pointer.

        Staging is deliberately outside every pointer lock: a rebuild may take
        as long as it takes without making current reads wait, and a stale
        result is caught at promotion rather than prevented by a long lock.
        """

        claimed, claim = self._claim(rebuild_id, owner=owner, state="composing")
        if claim is None:
            return claimed
        if claimed.staged_season_publication_id and claimed.staged_l15_publication_id:
            return self._transition(rebuild_id, claim=claim, state="validating")
        try:
            sources = self._sources(claimed)
            payloads = self.compose(sources)
            for stream_key in self.stream_keys:
                self._validate_target(stream_key, payloads[stream_key])
        except (TraditionalOpponentFormatError, PublicationPayloadError) as error:
            return self._fail(
                rebuild_id, claim=claim, code=self._failure_code(error)
            )
        except KeyError:
            return self._fail(
                rebuild_id, claim=claim, code="publication_candidate_invalid"
            )
        return self._stage_candidates(claimed, payloads, claim=claim)

    def promote(self, rebuild_id: str, *, owner: str = "worker") -> PublicationRebuild:
        """Revalidate every precondition and move both pointers, or neither.

        The pointer moves and this rebuild's own success write commit in one
        transaction.  Splitting them would leave a window in which a crash
        stranded promoted pointers behind a rebuild still recorded as
        in-flight, and the resumed attempt would then fail against pointer
        expectations its own earlier attempt had already satisfied.
        """

        claimed, claim = self._claim(rebuild_id, owner=owner, state="promoting")
        if claim is None:
            return claimed
        if not (
            claimed.staged_season_publication_id
            and claimed.staged_l15_publication_id
        ):
            return self._fail(
                rebuild_id, claim=claim, code="publication_candidate_invalid"
            )
        promotions = tuple(
            FamilyPromotion(
                stream_key=stream_key,
                candidate_publication_id=self._staged_id(claimed, stream_key),
                expected_publication_id=self._expected_id(claimed, stream_key),
                expected_fence=self._expected_fence(claimed, stream_key),
            )
            for stream_key in self.stream_keys
        )
        try:
            with self.publications._session_scope(None) as session:
                rebuild = self._locked(session, rebuild_id, claim=claim)
                if rebuild.state in TERMINAL_REBUILD_STATES:
                    return rebuild
                already = self._already_promoted(session, rebuild)
                if already is not None:
                    # A previous attempt moved the pointers and died before
                    # recording it.  The work is done; say so rather than
                    # reporting a stale precondition its own attempt created.
                    self._record_success(rebuild, promoted=already)
                    return rebuild
                # An accepted ledger correction always wins: it changes the
                # facts underneath the staged candidates, and a rebuild that
                # promoted anyway would quietly discard a truth correction.
                if self._source_checksum(
                    session,
                    season=rebuild.season,
                    game_ids=self._governed_game_ids(session, rebuild),
                ) != rebuild.source_checksum:
                    raise ControlPlaneError("stale_publication_family")
                self._assert_staged_still_valid(session, rebuild)
                promoted = self.publications.promote_publication_family(
                    promotions,
                    actor=rebuild.actor,
                    reason=rebuild.reason,
                    validate_payload=self._assert_target_payload,
                    validate_family=self._assert_coherent_family,
                    session=session,
                )
                self._record_success(rebuild, promoted={
                    version.stream_key: version for version in promoted
                })
                return rebuild
        except ControlPlaneError as error:
            if error.reason == "rebuild_lease_held":
                raise
            self._supersede_staged(rebuild_id, claim=claim)
            return self._fail(
                rebuild_id, claim=claim, code=self._promotion_code(error)
            )
        except (TraditionalOpponentFormatError, PublicationPayloadError) as error:
            self._supersede_staged(rebuild_id, claim=claim)
            return self._fail(
                rebuild_id, claim=claim, code=self._failure_code(error)
            )

    def _already_promoted(self, session, rebuild):
        """Return the promoted pair when the pointers already hold it.

        This is the resume path for a crash between the pointer commit and
        the success write.  It is deliberately narrow: the active publication
        of every window must be exactly the candidate *this* rebuild staged,
        so it can never mistake somebody else's promotion for its own.
        """

        promoted = {}
        for stream_key in self.stream_keys:
            staged_id = self._staged_id(rebuild, stream_key)
            pointer = session.scalar(
                select(PublicationPointer).where(
                    PublicationPointer.stream_key == stream_key
                )
            )
            if pointer is None or pointer.active_publication_id != staged_id:
                return None
            version = session.get(PublicationVersion, staged_id)
            if version is None or version.status != "active":
                return None
            promoted[stream_key] = version
        return promoted

    def _record_success(self, rebuild, *, promoted) -> None:
        """Write the success onto an already-locked, already-fenced row."""

        now = self.clock()
        rebuild.promoted_season_publication_id = promoted[
            SEASON_STREAM
        ].publication_id
        rebuild.promoted_season_checksum = promoted[SEASON_STREAM].checksum
        rebuild.promoted_l15_publication_id = promoted[L15_STREAM].publication_id
        rebuild.promoted_l15_checksum = promoted[L15_STREAM].checksum
        rebuild.state = "succeeded"
        rebuild.error_code = None
        rebuild.lease_owner = None
        rebuild.lease_expires_at = None
        rebuild.claimed_generation = None
        rebuild.completed_at = now
        rebuild.updated_at = now

    # --- Observing --------------------------------------------------------

    def status(self, rebuild_id: str, *, session: Session | None = None) -> dict:
        """Return the bounded operator view of one rebuild.

        Counts, identities, checksums, fingerprints, timestamps, phases, and a
        safe error code.  Never a game list, a payload, an actor, a credential,
        or a stack trace: the status surface is admin-only but it is still a
        different trust boundary from the payload it describes.
        """

        with self.publications._session_scope(session) as session:
            rebuild = session.get(PublicationRebuild, rebuild_id)
            if rebuild is None or rebuild.family != self.family:
                raise ControlPlaneError("rebuild_not_found")
            season_publication = session.get(
                PublicationVersion, rebuild.expected_season_publication_id
            )
            governed_game_count, team_count = self._population(season_publication)
            return {
                "rebuild_id": rebuild.rebuild_id,
                "family": rebuild.family,
                "state": rebuild.state,
                "target_format": rebuild.target_format,
                "target_fingerprint": rebuild.target_fingerprint,
                "actor_fingerprint": _fingerprint(rebuild.actor),
                "season": rebuild.season,
                "cutoff": _isoformat(rebuild.cutoff),
                "manifest_id": rebuild.manifest_id,
                "event_catalog_publication_id": (
                    rebuild.event_catalog_publication_id
                ),
                "source_checksum": rebuild.source_checksum,
                "governed_game_count": governed_game_count,
                "team_count": team_count,
                "attempts": int(rebuild.attempts or 0),
                "generation": int(rebuild.generation or 1),
                "expected": {
                    "season": {
                        "publication_id": rebuild.expected_season_publication_id,
                        "fence": int(rebuild.expected_season_fence),
                    },
                    "l15": {
                        "publication_id": rebuild.expected_l15_publication_id,
                        "fence": int(rebuild.expected_l15_fence),
                    },
                },
                "staged": {
                    "season": {
                        "publication_id": rebuild.staged_season_publication_id,
                        "checksum": rebuild.staged_season_checksum,
                    },
                    "l15": {
                        "publication_id": rebuild.staged_l15_publication_id,
                        "checksum": rebuild.staged_l15_checksum,
                    },
                },
                "promoted": {
                    "season": {
                        "publication_id": rebuild.promoted_season_publication_id,
                        "checksum": rebuild.promoted_season_checksum,
                    },
                    "l15": {
                        "publication_id": rebuild.promoted_l15_publication_id,
                        "checksum": rebuild.promoted_l15_checksum,
                    },
                },
                "error_code": rebuild.error_code,
                "created_at": _isoformat(rebuild.created_at),
                "updated_at": _isoformat(rebuild.updated_at),
                "completed_at": _isoformat(rebuild.completed_at),
            }

    # --- Rolling back -----------------------------------------------------

    def rollback(
        self,
        *,
        actor: str,
        reason: str,
        stream_keys: Sequence[str] | None = None,
        expected_fences: Mapping[str, int] | None = None,
        session: Session | None = None,
    ) -> tuple[PublicationVersion, ...]:
        """Move the whole family back one generation, or move none of it.

        A per-window rollback is refused rather than performed: it would leave
        the product observing one window in each format, which is exactly the
        state promotion exists to prevent.  A target this deployment can no
        longer read is refused as well -- an administrative action must not be
        able to knowingly create an outage.
        """

        del actor
        requested = tuple(stream_keys) if stream_keys is not None else self.stream_keys
        if set(requested) != set(self.stream_keys):
            raise ControlPlaneError("publication_family_coupled")
        try:
            return self.publications.rollback_publication_family(
                self.stream_keys,
                reason=reason,
                expected_fences=expected_fences,
                validate_payload=self._assert_supported_payload,
                validate_family=self._assert_coherent_family,
                session=session,
            )
        except TraditionalOpponentFormatError as error:
            raise ControlPlaneError(self._failure_code(error)) from error

    # --- Internals --------------------------------------------------------

    def _request_checksum(self, *, actor, reason, expected, target) -> str:
        return hashlib.sha256(json.dumps(
            {
                "family": self.family,
                "target_format": target.name,
                "target_fingerprint": target.fingerprint,
                "actor": actor,
                "reason": reason,
                "expected": [
                    expected.season_publication_id,
                    int(expected.season_fence),
                    expected.l15_publication_id,
                    int(expected.l15_fence),
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest()

    def _latest(self, session, *, states, request_checksum=None):
        statement = select(PublicationRebuild).where(
            PublicationRebuild.family == self.family,
            PublicationRebuild.state.in_(tuple(states)),
        )
        if request_checksum is not None:
            statement = statement.where(
                PublicationRebuild.request_checksum == request_checksum
            )
        return session.scalars(
            statement.order_by(PublicationRebuild.created_at.desc()).limit(1)
        ).first()

    def _resolve_authority(self, session, *, expected, season, cutoff) -> dict:
        """Read the active pair's own authority and prove it is rebuildable."""

        versions = {}
        for stream_key in self.stream_keys:
            pointer = session.scalar(
                select(PublicationPointer)
                .where(PublicationPointer.stream_key == stream_key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            expected_id, expected_fence = expected.for_stream(stream_key)
            if (
                pointer is None
                or not pointer.active_publication_id
                or pointer.active_publication_id != expected_id
                or int(pointer.fence) != expected_fence
            ):
                raise ControlPlaneError("stale_publication_family")
            version = session.get(PublicationVersion, expected_id)
            if version is None:
                raise ControlPlaneError("stale_publication_family")
            versions[stream_key] = version
        seasons = {version.season for version in versions.values()}
        cutoffs = {_aware(version.cutoff) for version in versions.values()}
        if len(seasons) != 1 or len(cutoffs) != 1:
            raise ControlPlaneError("publication_family_authority_mismatch")
        active_season = next(iter(seasons))
        active_cutoff = next(iter(cutoffs))
        # A ledger-composed active version carries its authority through its
        # accepted observations rather than on the row: the manifest is a
        # property of the evidence, not of the rendering.  Both windows must
        # rest on the same one, or they are not one family generation.
        manifest = self._shared_manifest(session, versions)
        # A supplied season or cutoff is an assertion about the pair, so a
        # mismatch is a stale request rather than a substituted authority.
        if season is not None and str(season) != active_season:
            raise ControlPlaneError("stale_publication_family")
        if cutoff is not None and _aware(cutoff) != active_cutoff:
            raise ControlPlaneError("stale_publication_family")
        formats = {
            self._payload_format(stream_key, version.payload)
            for stream_key, version in versions.items()
        }
        if len(formats) != 1:
            raise ControlPlaneError("publication_family_mixed_format")
        game_ids = _payload_game_ids(versions[SEASON_STREAM].payload)
        return {
            "season": active_season,
            "cutoff": active_cutoff,
            "manifest_id": manifest.manifest_id,
            "event_catalog_publication_id": (
                manifest.event_catalog_publication_id
            ),
            "event_catalog_checksum": manifest.event_catalog_checksum,
            "source_checksum": self._source_checksum(
                session, season=active_season, game_ids=game_ids
            ),
        }

    def _shared_manifest(self, session, versions) -> CollectionManifest:
        """Resolve the one manifest both windows' accepted evidence shares."""

        manifest_ids: set[str] = set()
        for version in versions.values():
            observation_ids = tuple(self._provenance(
                session, version.publication_id
            ))
            rows = session.scalars(select(CollectionObservation).where(
                CollectionObservation.observation_id.in_(observation_ids),
            )).all()
            if len(rows) != len(observation_ids):
                raise ControlPlaneError("ledger_provenance_not_accepted")
            manifest_ids |= {row.manifest_id for row in rows}
            # A version that also records authority on the row may not
            # disagree with the evidence it was composed from.
            if version.manifest_id is not None:
                manifest_ids.add(version.manifest_id)
        if len(manifest_ids) != 1 or None in manifest_ids:
            raise ControlPlaneError("publication_family_authority_mismatch")
        manifest = session.get(CollectionManifest, next(iter(manifest_ids)))
        if (
            manifest is None
            or not manifest.event_catalog_publication_id
            or not manifest.event_catalog_checksum
        ):
            raise ControlPlaneError("event_catalog_required")
        return manifest

    def _source_checksum(self, session, *, season, game_ids) -> str:
        """Fingerprint the exact accepted ledger facts the rebuild rests on."""

        table = CanonicalGameLedgerGame.__table__
        rows = session.execute(
            select(table.c.game_id, table.c.checksum).where(
                table.c.season == season, table.c.game_id.in_(tuple(game_ids))
            )
        ).all()
        checksums = {str(game_id): str(checksum) for game_id, checksum in rows}
        if set(checksums) != set(game_ids):
            raise ControlPlaneError("stale_publication_family")
        return window_ledger_checksum(game_ids, checksums)

    def _governed_game_ids(self, session, rebuild) -> tuple[str, ...]:
        version = session.get(
            PublicationVersion, rebuild.expected_season_publication_id
        )
        if version is None:
            raise ControlPlaneError("stale_publication_family")
        return _payload_game_ids(version.payload)

    def _sources(self, rebuild) -> RebuildSources:
        with self.publications._session_scope(None) as session:
            team_game_ids: dict[str, dict[int, frozenset[str]]] = {}
            team_ids: set[int] = set()
            for stream_key in self.stream_keys:
                version = session.get(
                    PublicationVersion, self._expected_id(rebuild, stream_key)
                )
                if version is None:
                    raise ControlPlaneError("stale_publication_family")
                rows = json.loads(version.payload)
                team_game_ids[stream_key] = {
                    int(row["team_id"]): frozenset(row["game_ids"]) for row in rows
                }
                team_ids |= set(team_game_ids[stream_key])
            governed = self._governed_game_ids(session, rebuild)
        cutoff = _aware(rebuild.cutoff)
        return RebuildSources(
            season=rebuild.season,
            cutoff=cutoff,
            as_of=slate_date_for_instant(cutoff),
            target_format=TRADITIONAL_OPPONENT_TARGET_FORMAT,
            stream_keys=self.stream_keys,
            governed_game_ids=governed,
            team_game_ids=team_game_ids,
            team_ids=frozenset(team_ids),
            source_checksum=rebuild.source_checksum,
        )

    def _stage_candidates(
        self, rebuild, payloads, *, claim: RebuildClaim
    ) -> PublicationRebuild:
        """Persist both candidates, reusing the active pair's provenance.

        Minting a candidate and superseding the one it replaces are durable
        mutations of the publication family, so they are fenced exactly like
        a pointer move: the claim is proved and the rebuild row locked in the
        *same* transaction that writes the candidates.  A pass that has
        already been superseded is therefore refused before it writes
        anything, rather than leaving candidate rows behind that no live
        rebuild accounts for.
        """

        staged: dict[str, PublicationVersion] = {}
        with self._fenced(rebuild.rebuild_id, claim=claim) as (session, locked):
            if locked.state in TERMINAL_REBUILD_STATES:
                return locked
            for stream_key in self.stream_keys:
                provenance = self._provenance(
                    session, self._expected_id(locked, stream_key)
                )
                staged[stream_key] = self.publications.compose_inactive_ledger(
                    stream_key,
                    season=locked.season,
                    cutoff=_aware(locked.cutoff),
                    payload=payloads[stream_key],
                    provenance=provenance,
                    reason=f"publication format rebuild to {locked.target_format}",
                    session=session,
                )
            locked.staged_season_publication_id = staged[
                SEASON_STREAM
            ].publication_id
            locked.staged_season_checksum = staged[SEASON_STREAM].checksum
            locked.staged_l15_publication_id = staged[L15_STREAM].publication_id
            locked.staged_l15_checksum = staged[L15_STREAM].checksum
            locked.state = "validating"
        return locked

    @staticmethod
    def _provenance(session, publication_id: str) -> dict[str, str]:
        """The accepted ledger provenance one active publication rests on.

        The stored evidence rows name the accepted observations; which game
        each one is evidence for is a property of the ledger, not of the
        rendering, so it is resolved from the ledger's own current source
        binding.  A correction that rebound a game therefore changes this map
        and makes the rebuild stale, which is the intended outcome.
        """

        observation_ids = tuple(sorted(session.scalars(
            select(PublicationObservation.observation_id).where(
                PublicationObservation.publication_id == publication_id,
            )
        ).all()))
        if not observation_ids:
            raise ControlPlaneError("ledger_provenance_required")
        table = CanonicalGameLedgerGame.__table__
        bound = dict(session.execute(
            select(table.c.source_observation_id, table.c.game_id).where(
                table.c.source_observation_id.in_(observation_ids)
            )
        ).all())
        if set(bound) != set(observation_ids):
            raise ControlPlaneError("ledger_provenance_not_accepted")
        return {
            str(observation_id): str(game_id)
            for observation_id, game_id in bound.items()
        }

    def _locked(self, session, rebuild_id: str, *, claim: RebuildClaim | None):
        """Lock one rebuild row and, when a claim is supplied, prove it holds.

        Ownership is the lease owner *and* the exact generation the holder
        claimed.  Because every claim advances the generation, a worker that
        was superseded -- even one running the same command under the same
        owner name -- presents a stale number and is refused.
        """

        rebuild = session.scalar(
            select(PublicationRebuild)
            .where(PublicationRebuild.rebuild_id == rebuild_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if rebuild is None or rebuild.family != self.family:
            raise ControlPlaneError("rebuild_not_found")
        if claim is not None and not (
            rebuild.lease_owner == claim.owner
            and int(rebuild.generation or 1) == claim.generation
            and rebuild.claimed_generation is not None
            and int(rebuild.claimed_generation) == claim.generation
        ):
            raise ControlPlaneError("rebuild_lease_held")
        return rebuild

    @contextmanager
    def _fenced(self, rebuild_id: str, *, claim: RebuildClaim | None):
        """Load one rebuild under a row lock, optionally proving the claim."""

        with self.publications._session_scope(None) as session:
            rebuild = self._locked(session, rebuild_id, claim=claim)
            yield session, rebuild
            rebuild.updated_at = self.clock()
            session.flush()

    def claim(
        self, rebuild_id: str, *, owner: str, state: str = "composing"
    ) -> RebuildClaim:
        """Take one rebuild for this worker, advancing its lease generation."""

        _rebuild, claim = self._claim(rebuild_id, owner=owner, state=state)
        if claim is None:
            raise ControlPlaneError("rebuild_not_claimable")
        return claim

    def _claim(
        self, rebuild_id: str, *, owner: str, state: str
    ) -> tuple[PublicationRebuild, RebuildClaim | None]:
        now = self.clock()
        with self._fenced(rebuild_id, claim=None) as (_session, rebuild):
            if rebuild.state in TERMINAL_REBUILD_STATES:
                return rebuild, None
            held_by_other = (
                rebuild.lease_owner
                and rebuild.lease_expires_at is not None
                and _aware(rebuild.lease_expires_at) > now
                and rebuild.lease_owner != owner
            )
            if held_by_other:
                raise ControlPlaneError("rebuild_lease_held")
            # Advancing on every claim is what retires the previous holder,
            # including a previous invocation of this same worker command.
            generation = int(rebuild.generation or 1) + 1
            rebuild.generation = generation
            rebuild.claimed_generation = generation
            rebuild.lease_owner = str(owner)[:128]
            rebuild.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            rebuild.attempts = int(rebuild.attempts or 0) + 1
            rebuild.state = state
            claim = RebuildClaim(
                rebuild_id=rebuild_id, owner=str(owner)[:128], generation=generation
            )
        return rebuild, claim

    def _transition(
        self, rebuild_id, *, claim: RebuildClaim, state, values=None
    ) -> PublicationRebuild:
        with self._fenced(rebuild_id, claim=claim) as (_session, rebuild):
            if rebuild.state in TERMINAL_REBUILD_STATES:
                # Someone already finished this rebuild; a later phase write
                # would be a regression, not progress.
                return rebuild
            for key, value in dict(values or {}).items():
                setattr(rebuild, key, value)
            rebuild.state = state
        return rebuild

    def _fail(self, rebuild_id, *, claim: RebuildClaim, code: str) -> PublicationRebuild:
        now = self.clock()
        with self._fenced(rebuild_id, claim=claim) as (_session, rebuild):
            if rebuild.state in TERMINAL_REBUILD_STATES:
                return rebuild
            rebuild.state = "failed"
            rebuild.error_code = code[:64]
            rebuild.lease_owner = None
            rebuild.lease_expires_at = None
            rebuild.claimed_generation = None
            rebuild.completed_at = now
        return rebuild

    def _supersede_staged(self, rebuild_id, *, claim: RebuildClaim) -> None:
        """Retain a valid but stale candidate as audit evidence only.

        It stays readable as the thing this rebuild proposed, and its
        ``superseded`` status makes it permanently ineligible for a later
        accidental activation.  It is fenced like every other write: a worker
        that lost the rebuild must not retire its successor's candidates.
        """

        with self._fenced(rebuild_id, claim=claim) as (session, rebuild):
            for publication_id in (
                rebuild.staged_season_publication_id,
                rebuild.staged_l15_publication_id,
            ):
                if not publication_id:
                    continue
                candidate = session.get(PublicationVersion, publication_id)
                if candidate is not None and candidate.status == "candidate":
                    candidate.status = "superseded"

    def _assert_staged_still_valid(self, session, rebuild) -> None:
        """Prove the staged pair is still exactly what this rebuild staged.

        The pointer fences prove nothing moved underneath; these prove the
        candidates themselves did not.  A candidate whose stored bytes no
        longer hash to the checksum this rebuild recorded is not the thing the
        operator approved, and a candidate whose accepted evidence was rebound
        by a ledger correction no longer rests on the authority it claimed.
        Either way the rebuild is stale rather than promotable.
        """

        for stream_key in self.stream_keys:
            staged_id = self._staged_id(rebuild, stream_key)
            candidate = session.get(PublicationVersion, staged_id)
            if candidate is None or candidate.checksum != self._staged_checksum(
                rebuild, stream_key
            ):
                raise ControlPlaneError("stale_publication_family")
            # ``_provenance`` resolves each recorded observation against the
            # ledger's *current* source binding, so a correction that rebound
            # any governed game raises here.
            staged_provenance = self._provenance(session, staged_id)
            expected_provenance = self._provenance(
                session, self._expected_id(rebuild, stream_key)
            )
            if staged_provenance != expected_provenance:
                raise ControlPlaneError("stale_publication_family")

    def _assert_coherent_family(self, session, targets) -> None:
        """Prove a set of publications is one coherent family generation.

        Promotion and rollback both move the family as a unit, so the pair
        being moved *to* has to be a pair that could have existed together:
        one format, one season, one cutoff, one authority.  Validating each
        window on its own would accept a Season from one generation beside a
        Last 15 from another, which is the mixed state the coupling exists to
        prevent.
        """

        formats, seasons, cutoffs, authorities = set(), set(), set(), set()
        for stream_key, version in targets:
            formats.add(self._payload_format(stream_key, version.payload))
            seasons.add(version.season)
            cutoffs.add(_aware(version.cutoff))
            authorities.add(self._authority_of(session, stream_key, version))
        if len(formats) != 1:
            raise ControlPlaneError("publication_family_mixed_format")
        if len(seasons) != 1 or len(cutoffs) != 1 or len(authorities) != 1:
            raise ControlPlaneError("publication_family_authority_mismatch")

    def _authority_of(self, session, stream_key: str, version):
        """The authority one publication actually rests on.

        A ledger-composed version records nothing on the row: its manifest and
        Event Catalog come from the accepted observations behind it.  Reading
        the null row fields would make every such publication look identical,
        so two targets from different manifests would compare equal and pass a
        coherence check that exists to catch exactly that.
        """

        if version.manifest_id is not None:
            return (
                version.manifest_id,
                version.event_catalog_publication_id,
                version.event_catalog_checksum,
            )
        manifest = self._shared_manifest(session, {stream_key: version})
        return (
            manifest.manifest_id,
            manifest.event_catalog_publication_id,
            manifest.event_catalog_checksum,
        )

    def _payload_format(self, stream_key: str, payload: str):
        window = normalize_traditional_opponent_window(
            decode_team_window(json.loads(payload), stream_key=stream_key),
            stream_key=stream_key,
        )
        return window.format

    def _validate_target(self, stream_key: str, payload: Any):
        """Prove one composed candidate is exactly the deployed target format."""

        traditional_opponent_window_kind(stream_key)
        window = normalize_traditional_opponent_window(
            decode_team_window(payload, stream_key=stream_key),
            stream_key=stream_key,
        )
        if window.format is not TRADITIONAL_OPPONENT_TARGET_FORMAT:
            raise TraditionalOpponentFormatError("publication_format_unsupported")
        return window

    def _assert_target_payload(self, stream_key: str, payload: str) -> None:
        self._validate_target(stream_key, json.loads(payload))

    def _assert_supported_payload(self, stream_key: str, payload: str) -> None:
        window = normalize_traditional_opponent_window(
            decode_team_window(json.loads(payload), stream_key=stream_key),
            stream_key=stream_key,
        )
        if window.format not in SUPPORTED_TRADITIONAL_OPPONENT_FORMATS:
            raise TraditionalOpponentFormatError("publication_format_unsupported")

    @staticmethod
    def _failure_code(error) -> str:
        reason = getattr(error, "reason", None)
        if reason in {
            "publication_format_unsupported",
            "publication_family_mismatch",
        }:
            return reason
        return "publication_candidate_invalid"

    @staticmethod
    def _promotion_code(error: ControlPlaneError) -> str:
        if error.reason in {
            "stale_composition",
            "stale_publication_family",
            "ledger_provenance_manifest_mismatch",
            # A correction that rebound or withdrew a game's accepted source
            # is exactly the race the rebuild must lose.
            "ledger_provenance_not_accepted",
            "ledger_provenance_required",
        }:
            return "stale_publication_family"
        return error.reason

    #: Which pair of columns each window of the family occupies.  Keeping the
    #: mapping in one place is what stops four accessors from drifting apart.
    _WINDOW_COLUMNS = {
        SEASON_STREAM: {
            "expected_id": "expected_season_publication_id",
            "expected_fence": "expected_season_fence",
            "staged_id": "staged_season_publication_id",
            "staged_checksum": "staged_season_checksum",
        },
        L15_STREAM: {
            "expected_id": "expected_l15_publication_id",
            "expected_fence": "expected_l15_fence",
            "staged_id": "staged_l15_publication_id",
            "staged_checksum": "staged_l15_checksum",
        },
    }

    @classmethod
    def _column(cls, rebuild, stream_key: str, role: str):
        try:
            return getattr(rebuild, cls._WINDOW_COLUMNS[stream_key][role])
        except KeyError:
            raise TraditionalOpponentFormatError(
                "publication_family_mismatch",
                f"{stream_key} is not a traditional-opponent window",
            ) from None

    @classmethod
    def _expected_id(cls, rebuild, stream_key: str) -> str:
        return cls._column(rebuild, stream_key, "expected_id")

    @classmethod
    def _expected_fence(cls, rebuild, stream_key: str) -> int:
        return int(cls._column(rebuild, stream_key, "expected_fence"))

    @classmethod
    def _staged_id(cls, rebuild, stream_key: str) -> str:
        return cls._column(rebuild, stream_key, "staged_id")

    @classmethod
    def _staged_checksum(cls, rebuild, stream_key: str) -> str:
        return cls._column(rebuild, stream_key, "staged_checksum")

    @staticmethod
    def _population(version) -> tuple[int, int]:
        if version is None:
            return 0, 0
        try:
            rows = json.loads(version.payload)
        except (TypeError, ValueError):
            return 0, 0
        games = {
            str(game_id) for row in rows for game_id in row.get("game_ids", ())
        }
        return len(games), len(rows)


class LedgerFactReader:
    """Load the exact governed games a rebuild is allowed to compose from."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def __call__(self, sources: RebuildSources):
        from app.services.canonical_game_ledger import (
            CanonicalGameLedgerRepository,
        )

        repository = CanonicalGameLedgerRepository(self.engine)
        games = []
        for game_id in sources.governed_game_ids:
            game = repository.get_game(game_id)
            if game is None:
                raise ControlPlaneError("stale_publication_family")
            games.append(game)
        return tuple(games)


def compose_from_ledger(
    sources: RebuildSources, *, games=None
) -> Mapping[str, Any]:
    """Re-materialize both windows from the same unchanged ledger facts.

    Nothing about the source selection is re-derived from today's calendar:
    the governed game set, the per-team Last 15 selection, the league roster,
    the season, and the cutoff all come from the pair being replaced.  Only
    the rendering changes, which is exactly what a format rebuild means.
    """

    from app.services.ledger_derivations import materialize_team_window

    if games is None:
        raise ControlPlaneError("rebuild_composer_unavailable")
    supplied = tuple(games)
    expected_game_ids = frozenset(sources.governed_game_ids)
    payloads: dict[str, Any] = {}
    for stream_key in sources.stream_keys:
        window = traditional_opponent_window_kind(stream_key)
        materialization = materialize_team_window(
            supplied,
            season=sources.season,
            as_of=sources.as_of,
            window_games=(15 if window == "l15" else None),
            expected_game_ids=expected_game_ids,
            expected_team_game_ids=(
                sources.team_game_ids[stream_key] if window == "l15" else None
            ),
            team_ids=sources.team_ids,
        )
        if not materialization.complete:
            raise ControlPlaneError("stale_publication_family")
        payloads[stream_key] = [
            _team_row(team) for team in materialization.teams
        ]
    return payloads


def _team_row(team) -> dict[str, Any]:
    """One published team row, in the exact stored envelope."""

    return {
        "team_id": int(team.team_id),
        "team_tricode": team.team_tricode,
        "game_ids": list(team.game_ids),
        "game_count": int(team.game_count),
        "counts": dict(team.counts),
        "team_minutes": float(team.team_minutes),
        "per48": dict(team.per48),
        "league_average": dict(team.league_average),
        "population_sigma": dict(team.population_sigma),
        "competition_rank": dict(team.competition_rank),
    }


def ledger_composer(engine: Engine) -> Callable[[RebuildSources], Mapping[str, Any]]:
    """The production composer: read the governed games, then materialize."""

    read_facts = LedgerFactReader(engine)

    def compose(sources: RebuildSources) -> Mapping[str, Any]:
        return compose_from_ledger(sources, games=read_facts(sources))

    return compose


def _payload_game_ids(payload: str) -> tuple[str, ...]:
    rows = json.loads(payload)
    return tuple(sorted({
        str(game_id) for row in rows for game_id in row.get("game_ids", ())
    }))


def _fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(str(value).strip().encode()).hexdigest()


def _isoformat(value: datetime | None) -> str | None:
    return None if value is None else _aware(value).isoformat()


__all__ = [
    "ACTIVE_REBUILD_STATES",
    "DEFAULT_LEASE_SECONDS",
    "REBUILD_STATES",
    "TERMINAL_REBUILD_STATES",
    "FamilyExpectation",
    "RebuildSources",
    "TraditionalOpponentRebuildService",
    "LedgerFactReader",
    "compose_from_ledger",
    "ledger_composer",
]
