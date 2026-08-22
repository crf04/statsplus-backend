"""Bounded dual-run parity between legacy and ledger matchup materializers.

The ledger-owned Season and exact-L15 matchup materializer
(:class:`app.services.ledger_matchup_materialization.LedgerMatchupMaterializationService`)
and the legacy provider-aggregate writer
(:class:`app.services.team_matchup_refresh.TeamMatchupRefreshService`) both
produce the disposable ``team_matchup_facts`` read model for the same governed
season and cutoff.  The two materializers write the same surface rows, so their
outputs never coexist in one stored snapshot; each side is produced
independently and handed to the comparator as a provenance-bound
:class:`MatchupMaterialization`.

The ledger side is never accepted as hand-written JSON: it is derived from the
actual immutable candidate :class:`app.models.collection_control.PublicationVersion`,
re-verified against its checksum and its exact manifest and Event Catalog
authority, and byte-bound to that payload.  The legacy side is produced by the
injected legacy materializer seam and re-validated against the same governed
30-team roster and exact game sets.  Hard facts (identity, integer counts,
game sets, scope, authority, availability, completeness, and the byte contract)
are non-adjudicable; only the documented floating denominator/rate tolerance
may be adjudicated.

NBA-owned shot zones, grouped shot types, and Synergy play types are composed
from governed publications, never from the ledger, and have no dual-run.
"""

from __future__ import annotations

import json
import hashlib
import math
from collections.abc import Callable, Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.domain.publication_integrity import publication_payload_matches_checksum
from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.matchup_parity_contract import (
    CLASSIFICATION_AUTHORITY_MISMATCH,
    CLASSIFICATION_AVAILABILITY_DIFFERENCE,
    CLASSIFICATION_CUTOFF_MISMATCH,
    CLASSIFICATION_DENOMINATOR_TOLERANCE_EXCEEDED,
    CLASSIFICATION_DERIVED_RATE_DIFFERENCE,
    CLASSIFICATION_DUPLICATE_METRIC,
    CLASSIFICATION_EXTRA_METRIC,
    CLASSIFICATION_EXTRA_TEAM,
    CLASSIFICATION_GAME_SET_MISMATCH,
    CLASSIFICATION_INTEGER_COUNT_DIFFERENCE,
    CLASSIFICATION_INVALID_DENOMINATOR,
    CLASSIFICATION_L15_GAME_COUNT_MISMATCH,
    CLASSIFICATION_LEAGUE_INCOMPLETE,
    CLASSIFICATION_MISSING_LEGACY_TEAM,
    CLASSIFICATION_MISSING_LEDGER_TEAM,
    CLASSIFICATION_MISSING_METRIC,
    CLASSIFICATION_MISSING_SURFACE,
    CLASSIFICATION_NON_INTEGER_COUNT,
    CLASSIFICATION_RANKING_DIFFERENCE,
    CLASSIFICATION_SCOPE_MISMATCH,
    CLASSIFICATION_SERVED_RANK_MISMATCH,
    CLASSIFICATION_SERVED_RATE_MISMATCH,
    HARD_CLASSIFICATIONS,
    semantic_rule_is_approved,
    SOFT_CLASSIFICATIONS,
)
from app.domain.slate_time import slate_date_for_instant
from app.domain.utc import assume_utc
from app.domain.team_matchup_taxonomy import (
    LEDGER_OWNED_MATCHUP_SURFACES,
    matchup_stream_key,
)
from app.models.collection_control import PublicationVersion
from app.services.ledger_derivations import (
    MATCHUP_ASSIST_KEYS,
    MATCHUP_TRADITIONAL_KEYS,
    competition_ranks,
    nominal_window_minutes,
)
from app.services.ledger_lineage import LedgerLineage
from app.services.publication_authority import verify_publication_authority
from app.services.team_matchup_publications import PublicationGovernanceUnavailable
from app.services.team_matchup_repository import (
    TeamMatchupFact,
    TeamMatchupObservation,
    TeamMatchupSnapshotScope,
)
from app.services.team_matchup_refresh import (
    _nba_team_stats_request_descriptor,
    _pbp_totals_request_descriptor,
)

#: The single documented tolerance for floating denominators and derived
#: per-48 rates.  Integer counts compare exactly; only denominators (effective
#: team minutes) and the per-48 values recomputed from them admit this relative
#: tolerance.
MATCHUP_PARITY_TOLERANCE = 1e-9

#: Ledger-owned surfaces that participate in the dual-run (re-exported from the
#: canonical taxonomy so the comparator and stream keys never drift).
LEDGER_OWNED_SURFACES = LEDGER_OWNED_MATCHUP_SURFACES

#: Per-surface contracted count metrics: stat key -> canonical count metric.
_SURFACE_COUNT_KEYS = {
    "traditional": MATCHUP_TRADITIONAL_KEYS,
    "assist_locations": MATCHUP_ASSIST_KEYS,
}

#: Producer identities bound to each side of the dual-run.
PRODUCER_LEGACY = "legacy_team_matchup_refresh"
PRODUCER_LEDGER = "ledger_publication"

#: Per-48 scaling shared by both materializers.
_PER48 = 48.0

def matchup_surface_stream_keys(window: str) -> tuple[str, ...]:
    """Return every ledger-owned stream key for one governed window."""

    return tuple(
        matchup_stream_key(surface, window)
        for surface in LEDGER_OWNED_SURFACES
    )


class MatchupParityError(ValueError):
    """A dual-run input cannot be sourced from the required authority."""


@dataclass(frozen=True, slots=True)
class MatchupMaterialization:
    """One independently produced, provenance-bound materializer output."""

    season: str
    window: str
    cutoff: datetime
    facts: tuple[TeamMatchupFact, ...]
    observations: tuple[TeamMatchupObservation, ...]
    game_ids_by_team: Mapping[int, frozenset[str]]
    producer: str = PRODUCER_LEGACY
    manifest_id: str | None = None
    event_catalog_publication_id: str | None = None
    event_catalog_checksum: str | None = None
    publication_id: str | None = None
    payload_checksum: str | None = None
    provider_window_identity: str | None = None
    retained_last_good: bool = False
    served_per48: Mapping[tuple[int, str], float] = field(default_factory=dict)
    served_ranks: Mapping[tuple[int, str], int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LedgerTeamWindowRow:
    """One decoded team row from an immutable ledger publication payload."""

    team_id: int
    team_tricode: str
    game_ids: tuple[str, ...]
    counts: Mapping[str, float]
    team_minutes: float
    per48: Mapping[str, float] = field(default_factory=dict)
    competition_rank: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MatchupPublication:
    """One verified candidate publication and its exact immutable authority."""

    publication_id: str
    stream_key: str
    season: str
    cutoff: datetime
    payload_checksum: str
    manifest_id: str
    event_catalog_publication_id: str
    event_catalog_checksum: str
    rows: tuple[LedgerTeamWindowRow, ...]


@dataclass(frozen=True, slots=True)
class MatchupParityDifference:
    """One classified, bounded difference between the two materializers."""

    window: str
    surface: str | None
    team_id: int | None
    field: str
    ledger_value: object
    legacy_value: object
    classification: str

    def to_dict(self) -> dict[str, Any]:
        ledger_value = _json_value(self.ledger_value)
        legacy_value = _json_value(self.legacy_value)
        def checksum(value):
            return hashlib.sha256(json.dumps(
                value, sort_keys=True, separators=(",", ":"), default=str,
            ).encode("utf-8")).hexdigest()
        return {
            "window": self.window,
            "surface": self.surface,
            "team_id": self.team_id,
            "field": self.field,
            "ledger_value": ledger_value,
            "legacy_value": legacy_value,
            "ledger_checksum": checksum(ledger_value),
            "legacy_checksum": checksum(legacy_value),
            "classification": self.classification,
            "blocks_approval": self.classification in HARD_CLASSIFICATIONS,
        }


@dataclass(frozen=True, slots=True)
class MatchupParityReport:
    """Durable evidence for one surface in one Season/L15 dual-run window."""

    season: str
    window: str
    surface: str
    cutoff: datetime
    tolerance: float
    expected_team_ids: frozenset[int]
    league_complete: bool
    team_identities_exact: bool
    game_sets_exact: bool
    cutoffs_aligned: bool
    rankings_deterministic: bool
    compared_count: int
    ledger_publication_id: str | None = None
    ledger_payload_checksum: str | None = None
    differences: tuple[MatchupParityDifference, ...] = ()
    legacy_game_ids_by_team: Mapping[int, tuple[str, ...]] = field(default_factory=dict)
    ledger_game_ids_by_team: Mapping[int, tuple[str, ...]] = field(default_factory=dict)
    legacy_game_set_checksum: str | None = None
    ledger_game_set_checksum: str | None = None
    legacy_manifest_id: str | None = None
    legacy_event_catalog_publication_id: str | None = None
    legacy_event_catalog_checksum: str | None = None
    ledger_manifest_id: str | None = None
    ledger_event_catalog_publication_id: str | None = None
    ledger_event_catalog_checksum: str | None = None
    semantic_rule: str | None = None
    semantic_rule_reason: str | None = None

    @property
    def hard_failures(self) -> tuple[MatchupParityDifference, ...]:
        return tuple(
            difference for difference in self.differences
            if difference.classification in HARD_CLASSIFICATIONS
        )

    @property
    def semantic_differences(self) -> tuple[MatchupParityDifference, ...]:
        return tuple(
            difference for difference in self.differences
            if difference.classification in SOFT_CLASSIFICATIONS
        )

    @property
    def hard_failure(self) -> bool:
        return bool(self.hard_failures) or (
            bool(self.semantic_differences)
            and not semantic_rule_is_approved(
                self.semantic_rule, self.semantic_rule_reason
            )
        )

    @property
    def adjudication_required(self) -> bool:
        return bool(self.semantic_differences) and not self.hard_failure

    @property
    def exact(self) -> bool:
        """Whether every required comparison passed with no differences."""

        return (
            self.compared_count > 0
            and not self.hard_failure
            and not self.semantic_differences
            and self.league_complete
            and self.team_identities_exact
            and self.game_sets_exact
            and self.cutoffs_aligned
            and self.rankings_deterministic
        )

    @property
    def status(self) -> str:
        if self.hard_failure:
            return "failed"
        return "adjudication_required" if self.adjudication_required else "exact"

    def to_dict(self) -> dict[str, Any]:
        return {
            "season": self.season,
            "window": self.window,
            "surface": self.surface,
            "cutoff": self.cutoff.isoformat(),
            "tolerance": self.tolerance,
            "expected_team_ids": sorted(int(team_id) for team_id in self.expected_team_ids),
            "league_complete": self.league_complete,
            "team_identities_exact": self.team_identities_exact,
            "game_sets_exact": self.game_sets_exact,
            "cutoffs_aligned": self.cutoffs_aligned,
            "rankings_deterministic": self.rankings_deterministic,
            "compared_count": self.compared_count,
            "ledger_publication_id": self.ledger_publication_id,
            "ledger_payload_checksum": self.ledger_payload_checksum,
            "legacy_game_ids_by_team": {
                str(team_id): sorted(str(game_id) for game_id in game_ids)
                for team_id, game_ids in sorted(self.legacy_game_ids_by_team.items())
            },
            "ledger_game_ids_by_team": {
                str(team_id): sorted(str(game_id) for game_id in game_ids)
                for team_id, game_ids in sorted(self.ledger_game_ids_by_team.items())
            },
            "legacy_game_set_checksum": self.legacy_game_set_checksum,
            "ledger_game_set_checksum": self.ledger_game_set_checksum,
            "legacy_manifest_id": self.legacy_manifest_id,
            "legacy_event_catalog_publication_id": self.legacy_event_catalog_publication_id,
            "legacy_event_catalog_checksum": self.legacy_event_catalog_checksum,
            "ledger_manifest_id": self.ledger_manifest_id,
            "ledger_event_catalog_publication_id": self.ledger_event_catalog_publication_id,
            "ledger_event_catalog_checksum": self.ledger_event_catalog_checksum,
            "status": self.status,
            "semantic_rule": self.semantic_rule,
            "semantic_rule_reason": self.semantic_rule_reason,
            "differences": [difference.to_dict() for difference in self.differences],
        }


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (tuple, frozenset, set, list)):
        return sorted(str(item) for item in value)
    return value


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _window_for_stream(stream_key: str) -> str:
    if stream_key.endswith("_l15"):
        return "l15"
    if stream_key.endswith("_season"):
        return "season"
    raise MatchupParityError(f"stream key {stream_key} is not a matchup window stream")


def resolve_matchup_publication(
    session: Session,
    *,
    publication_id: str,
    stream_key: str,
    season: str,
    cutoff: datetime,
    manifest_id: str,
    event_catalog_publication_id: str,
    event_catalog_checksum: str,
) -> MatchupPublication:
    """Read and verify one candidate publication against its exact authority.

    The publication must name the requested stream, season, and exact aware
    cutoff, its payload must match its stored checksum byte-for-byte, and its
    bound manifest and immutable Event Catalog authority must equal the
    governed authority passed by the caller.  A copied, synthetic, or later
    same-cutoff reinterpretation therefore fails before any comparison.
    """

    if cutoff is None or cutoff.tzinfo is None:
        raise MatchupParityError("matchup parity requires an aware immutable cutoff")
    publication = session.scalar(
        select(PublicationVersion).where(
            PublicationVersion.publication_id == publication_id,
        )
    )
    if publication is None:
        raise MatchupParityError("publication_candidate_invalid")
    if (
        publication.stream_key != stream_key
        or publication.season != season
        or assume_utc(publication.cutoff) != _aware(cutoff)
        or publication.status != "candidate"
        or not publication_payload_matches_checksum(
            publication.payload, publication.checksum
        )
    ):
        raise MatchupParityError("publication_candidate_invalid")
    try:
        authority = verify_publication_authority(session, publication)
    except PublicationGovernanceUnavailable as error:
        raise MatchupParityError("publication_authority_invalid") from error
    if (
        authority.manifest_id != manifest_id
        or authority.event_catalog_publication_id != event_catalog_publication_id
        or authority.event_catalog_checksum != event_catalog_checksum
    ):
        raise MatchupParityError("publication_authority_mismatch")
    rows = _decode_ledger_rows(publication.payload, stream_key=stream_key)
    return MatchupPublication(
        publication_id=publication.publication_id,
        stream_key=stream_key,
        season=season,
        cutoff=_aware(cutoff),
        payload_checksum=publication.checksum,
        manifest_id=authority.manifest_id,
        event_catalog_publication_id=authority.event_catalog_publication_id,
        event_catalog_checksum=authority.event_catalog_checksum,
        rows=rows,
    )


def _decode_ledger_rows(payload: Any, *, stream_key: str) -> tuple[LedgerTeamWindowRow, ...]:
    """Decode the exact typed ledger materializer payload.

    The publication payload is the materializer's immutable typed envelope,
    not an operator-authored comparison document.  Keep this decoder stricter
    than the general read decoder: a parity candidate must be the canonical
    list emitted by the ledger materializer and must carry every derived field
    needed to prove its metric and window contract.
    """

    try:
        document = json.loads(payload, object_pairs_hook=_reject_duplicate_json_keys)
        if not isinstance(document, list):
            raise ValueError("ledger publication payload must be a row list")
        from app.services.database_first_activation import decode_team_window
        from app.services.ledger_derivations import (
            ASSIST_DERIVED_METRICS,
            TEAM_METRICS,
        )

        surface = (
            "assist_locations"
            if stream_key.startswith("assist_locations_")
            else "traditional"
        )
        # Reuse the public typed row decoder for scalar and envelope checks.
        # Empty assist is the sole scoped unavailable candidate; every other
        # empty or malformed payload remains invalid evidence.
        if document or surface != "assist_locations":
            decode_team_window(document, stream_key=stream_key)
        expected_metrics = frozenset(
            ASSIST_DERIVED_METRICS if surface == "assist_locations" else TEAM_METRICS
        )
        expected_keys = frozenset({
            "team_id", "team_tricode", "game_ids", "game_count", "per48",
            "league_average", "population_sigma", "competition_rank",
            "counts", "team_minutes",
        })
    except (ImportError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise MatchupParityError("publication_payload_invalid") from error

    rows = []
    seen_team_ids: set[int] = set()
    for row in document:
        try:
            if frozenset(row) != expected_keys:
                raise ValueError("ledger publication row keys are not canonical")
            team_id = row["team_id"]
            if isinstance(team_id, bool) or not isinstance(team_id, int) or team_id < 1:
                raise ValueError("ledger publication team identity is invalid")
            if team_id in seen_team_ids:
                raise ValueError("ledger publication repeats a team")
            seen_team_ids.add(team_id)
            team_tricode = row["team_tricode"]
            if (
                not isinstance(team_tricode, str)
                or team_tricode != NBA_TEAM_ID_TO_TRICODE.get(team_id)
            ):
                raise ValueError("ledger publication team identity is invalid")
            game_ids = row["game_ids"]
            if (
                not isinstance(game_ids, list)
                or any(not isinstance(game_id, str) or not game_id.strip() for game_id in game_ids)
                or len(game_ids) != len(set(game_ids))
                or row["game_count"] != len(game_ids)
            ):
                raise ValueError("ledger publication game set is invalid")
            counts_value = row["counts"]
            if not isinstance(counts_value, dict) or frozenset(counts_value) != expected_metrics:
                raise ValueError("ledger publication metric taxonomy is invalid")
            counts = {}
            for key, value in counts_value.items():
                if isinstance(value, bool):
                    raise ValueError("ledger publication count is invalid")
                number = float(value)
                if not math.isfinite(number) or number < 0 or not number.is_integer():
                    raise ValueError("ledger publication count is invalid")
                counts[str(key)] = number
            per48_value = row["per48"]
            if not isinstance(per48_value, dict) or frozenset(per48_value) != expected_metrics:
                raise ValueError("ledger publication per48 taxonomy is invalid")
            per48 = {}
            for key, value in per48_value.items():
                if isinstance(value, bool):
                    raise ValueError("ledger publication per48 value is invalid")
                number = float(value)
                if not math.isfinite(number) or number < 0:
                    raise ValueError("ledger publication per48 value is invalid")
                per48[str(key)] = number
            rank_value = row["competition_rank"]
            if not isinstance(rank_value, dict) or frozenset(rank_value) != expected_metrics:
                raise ValueError("ledger publication ranking taxonomy is invalid")
            competition_rank = {}
            for key, value in rank_value.items():
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError("ledger publication ranking is invalid")
                competition_rank[str(key)] = value
            team_minutes = row["team_minutes"]
            if isinstance(team_minutes, bool):
                raise ValueError("ledger publication denominator is invalid")
            team_minutes = float(team_minutes)
            if not math.isfinite(team_minutes) or team_minutes <= 0:
                raise ValueError("ledger publication denominator is invalid")
            rows.append(LedgerTeamWindowRow(
                team_id=team_id,
                team_tricode=team_tricode,
                game_ids=tuple(game_ids),
                counts=counts,
                team_minutes=team_minutes,
                per48=per48,
                competition_rank=competition_rank,
            ))
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
            raise MatchupParityError("publication_payload_invalid") from error
    if not rows and surface == "assist_locations":
        return ()
    if len(rows) != 30 or seen_team_ids != set(NBA_TEAM_ID_TO_TRICODE):
        raise MatchupParityError("publication_payload_invalid")
    return tuple(rows)


def materialization_from_publication(
    publication: MatchupPublication,
    *,
    surface: str,
) -> MatchupMaterialization:
    """Derive the provenance-bound ledger side from a verified publication."""

    if surface not in _SURFACE_COUNT_KEYS:
        raise MatchupParityError(f"unsupported matchup surface {surface}")
    stat_to_metric = _SURFACE_COUNT_KEYS[surface]
    served_per48 = {
        (row.team_id, stat_key): row.per48[metric]
        for row in publication.rows
        for stat_key, metric in stat_to_metric.items()
    }
    served_ranks = {
        (row.team_id, stat_key): row.competition_rank[metric]
        for row in publication.rows
        for stat_key, metric in stat_to_metric.items()
    }
    facts = tuple(
        TeamMatchupFact(
            team_id=row.team_id,
            base=surface,
            slice_key=stat_key,
            stat_key=stat_key,
            raw_value=float(row.counts[metric]),
            denominator_value=row.team_minutes,
            denominator_unit="minutes",
            provider="ledger",
            game_ids=row.game_ids,
        )
        for row in publication.rows
        for stat_key, metric in stat_to_metric.items()
    )
    return MatchupMaterialization(
        season=publication.season,
        window=_window_for_stream(publication.stream_key),
        cutoff=publication.cutoff,
        facts=facts,
        observations=(TeamMatchupObservation(surface=surface, status="available"),),
        game_ids_by_team={
            row.team_id: frozenset(row.game_ids) for row in publication.rows
        },
        producer=PRODUCER_LEDGER,
        manifest_id=publication.manifest_id,
        event_catalog_publication_id=publication.event_catalog_publication_id,
        event_catalog_checksum=publication.event_catalog_checksum,
        publication_id=publication.publication_id,
        payload_checksum=publication.payload_checksum,
        served_per48=served_per48,
        served_ranks=served_ranks,
    )


class LegacyMatchupSource(Protocol):
    """Produce one legacy materializer output for a governed season/cutoff."""

    def produce(
        self,
        *,
        season: str,
        window: str,
        cutoff: datetime,
        governance: Any,
        session: Session | None = None,
        surface: str | None = None,
    ) -> MatchupMaterialization: ...


class StoredLegacyMatchupSource:
    """Read the legacy materializer's persisted output and bind it to authority.

    The legacy provider-aggregate writer persists its facts into
    ``team_matchup_facts`` under providers ``nba_stats``/``pbp_stats``.  This
    source reads those actual rows and stamps the materialization with the
    governed immutable authority and the exact governed game sets, so a later
    synthetic document cannot stand in for the legacy materializer.
    """

    LEGACY_PROVIDERS = frozenset({"nba_stats", "pbp_stats"})

    def __init__(self, repository) -> None:
        self.repository = repository

    def produce(
        self,
        *,
        season: str,
        window: str,
        cutoff: datetime,
        governance: Any,
        session: Session | None = None,
        surface: str | None = None,
    ) -> MatchupMaterialization:
        as_of = slate_date_for_instant(cutoff)
        scope = TeamMatchupSnapshotScope(
            season, as_of, 15 if window == "l15" else None
        )
        snapshot = self.repository.get_snapshot(
            scope,
            connection=(session.connection() if session is not None else None),
            lock=session is not None,
        )
        facts = tuple(
            fact for fact in snapshot.facts
            if (
                fact.provider in self.LEGACY_PROVIDERS
                and fact.base in LEDGER_OWNED_SURFACES
                and (surface is None or fact.base == surface)
            )
        )
        observations = tuple(
            observation for observation in snapshot.observations
            if observation.surface in LEDGER_OWNED_SURFACES
            and (surface is None or observation.surface == surface)
        )
        expected_authority = (
            governance.manifest_id,
            governance.event_catalog_publication_id,
            governance.event_catalog_checksum,
        )
        retained_last_good = False
        if observations and all(
            observation.status == "unavailable" for observation in observations
        ):
            # The serving repository retains last-good facts when a new
            # provider attempt is unavailable. Same-authority facts remain
            # valid parity evidence; older-authority facts do not.
            fact_metadata = {
                (
                    fact.cutoff,
                    fact.manifest_id,
                    fact.event_catalog_publication_id,
                    fact.event_catalog_checksum,
                    fact.provider_window_identity,
                )
                for fact in facts
            }
            if len(fact_metadata) == 1:
                fact_cutoff, manifest_id, catalog_id, catalog_checksum, identity = (
                    next(iter(fact_metadata))
                )
                try:
                    same_cutoff = (
                        fact_cutoff is not None
                        and _aware(fact_cutoff) == _aware(cutoff)
                    )
                except MatchupParityError:
                    same_cutoff = False
                if (
                    same_cutoff
                    and (manifest_id, catalog_id, catalog_checksum)
                    == expected_authority
                    and isinstance(identity, str)
                    and identity.strip()
                ):
                    retained_last_good = True
                else:
                    facts = ()
            else:
                facts = ()
        if not observations or (
            not facts and not all(item.status == "unavailable" for item in observations)
        ):
            raise MatchupParityError("legacy materialization provenance unavailable")
        if retained_last_good:
            for observation in observations:
                try:
                    observation_cutoff_matches = (
                        observation.cutoff is not None
                        and _aware(observation.cutoff) == _aware(cutoff)
                    )
                except MatchupParityError:
                    observation_cutoff_matches = False
                if (
                    not observation_cutoff_matches
                    or (
                        observation.manifest_id,
                        observation.event_catalog_publication_id,
                        observation.event_catalog_checksum,
                    )
                    != expected_authority
                    or not isinstance(observation.provider_window_identity, str)
                    or not observation.provider_window_identity.strip()
                ):
                    raise MatchupParityError(
                        "legacy unavailable observation provenance mismatch"
                    )
        provenance_items = facts if retained_last_good else (*facts, *observations)
        persisted_metadata = {
            (
                item.cutoff,
                item.manifest_id,
                item.event_catalog_publication_id,
                item.event_catalog_checksum,
                item.provider_window_identity,
            )
            for item in provenance_items
        }
        if len(persisted_metadata) != 1:
            raise MatchupParityError("legacy materialization provenance mismatch")
        persisted_cutoff, manifest_id, catalog_id, catalog_checksum, window_identity = (
            next(iter(persisted_metadata))
        )
        try:
            cutoff_matches = _aware(persisted_cutoff) == _aware(cutoff)
        except MatchupParityError:
            cutoff_matches = False
        if (
            not cutoff_matches
            or (manifest_id, catalog_id, catalog_checksum) != expected_authority
            or not all(isinstance(value, str) and value.strip() for value in (
                manifest_id, catalog_id, catalog_checksum, window_identity
            ))
        ):
            raise MatchupParityError("legacy materialization provenance unavailable")
        # Provider rows are the legacy materializer's actual output.  Do not
        # stamp the governed expectation onto them: a provider response that
        # selected the wrong games must fail parity rather than be relabeled
        # as the governed window.  A disagreement between rows for one team
        # is represented as an empty set, which the comparator classifies as a
        # hard game-set failure.
        ids_by_team: dict[int, set[frozenset[str]]] = {}
        for fact in facts:
            ids_by_team.setdefault(int(fact.team_id), set()).add(
                frozenset(str(game_id) for game_id in fact.game_ids)
            )
        game_ids_by_team = {
            team_id: next(iter(game_sets)) if len(game_sets) == 1 else frozenset()
            for team_id, game_sets in ids_by_team.items()
        }
        try:
            identity = json.loads(window_identity)
            identity_teams = identity["teams"]
            provider_sources = identity.get("provider_sources")
            if provider_sources is None:
                provider_sources = (identity.get("provider_source"),)
            source_memberships = identity["provider_game_ids_by_source"]
            aggregate_requests = identity["aggregate_requests"]
            aggregate_request_checksum = identity["aggregate_request_checksum"]
            if not isinstance(aggregate_requests, dict):
                raise ValueError
            as_of = slate_date_for_instant(cutoff)
            selected_surfaces = (
                (surface,) if surface is not None else LEDGER_OWNED_SURFACES
            )
            selected_sources = {
                "traditional": "nba_stats.team_game_log",
                "assist_locations": "pbp_stats.team_game_log",
            }
            expected_sources = {selected_sources[item] for item in selected_surfaces}
            selected_aggregate_requests = {
                key: value for key, value in aggregate_requests.items()
                if key.split(":", 1)[0] in selected_surfaces
            }
            unavailable_candidate = (
                identity.get("status") == "unavailable"
                and not facts
                and all(item.status == "unavailable" for item in observations)
            )
            if window == "season":
                all_expected_requests = {
                    "traditional:league": _nba_team_stats_request_descriptor(
                        season=season, season_type="Regular Season",
                        team_id=None, last_n_games=0, date_from=None,
                        date_to=as_of.strftime("%m/%d/%Y"),
                    ),
                    "assist_locations:league": _pbp_totals_request_descriptor(
                        season=season, season_type="Regular Season",
                        team_id=None, from_date=None,
                        to_date=as_of.isoformat(),
                    ),
                }
                expected_aggregate_requests = {
                    key: value for key, value in all_expected_requests.items()
                    if key.split(":", 1)[0] in selected_surfaces
                }
                aggregate_requests_valid = (
                    selected_aggregate_requests == expected_aggregate_requests
                )
            else:
                boundaries_by_team = {}
                for fact in facts:
                    if fact.base not in LEDGER_OWNED_SURFACES:
                        continue
                    value = fact.window_start_date
                    existing = boundaries_by_team.setdefault(fact.team_id, value)
                    if existing != value or value is None:
                        raise ValueError
                request_team_ids = (
                    governance.team_ids if unavailable_candidate
                    else boundaries_by_team
                )
                expected_request_keys = {
                    f"{surface}:{team_id}"
                    for surface in selected_surfaces
                    for team_id in request_team_ids
                }
                aggregate_requests_valid = (
                    isinstance(aggregate_requests, dict)
                    and set(selected_aggregate_requests) == expected_request_keys
                )
                if unavailable_candidate:
                    governed_boundaries = getattr(
                        governance, "expected_l15_date_from_by_team", {}
                    )
                    aggregate_requests_valid = (
                        aggregate_requests_valid
                        and set(governed_boundaries) == set(governance.team_ids)
                    )
                    for team_id, start_text in governed_boundaries.items():
                        start = datetime.strptime(start_text, "%m/%d/%Y").date()
                        if "traditional" in selected_surfaces:
                            aggregate_requests_valid = aggregate_requests_valid and (
                                selected_aggregate_requests.get(
                                    f"traditional:{team_id}"
                                ) == _nba_team_stats_request_descriptor(
                                    season=season, season_type="Regular Season",
                                    team_id=team_id, last_n_games=15,
                                    date_from=start_text,
                                    date_to=as_of.strftime("%m/%d/%Y"),
                                )
                            )
                        if "assist_locations" in selected_surfaces:
                            aggregate_requests_valid = aggregate_requests_valid and (
                                selected_aggregate_requests.get(
                                    f"assist_locations:{team_id}"
                                ) == _pbp_totals_request_descriptor(
                                    season=season, season_type="Regular Season",
                                    team_id=team_id, from_date=start.isoformat(),
                                    to_date=as_of.isoformat(),
                                )
                            )
                for team_id, start in boundaries_by_team.items():
                    if "traditional" in selected_surfaces:
                        aggregate_requests_valid = aggregate_requests_valid and (
                            selected_aggregate_requests.get(f"traditional:{team_id}")
                            == _nba_team_stats_request_descriptor(
                                season=season, season_type="Regular Season",
                                team_id=team_id, last_n_games=15,
                                date_from=start.strftime("%m/%d/%Y"),
                                date_to=as_of.strftime("%m/%d/%Y"),
                            )
                        )
                    if "assist_locations" in selected_surfaces:
                        aggregate_requests_valid = aggregate_requests_valid and (
                            selected_aggregate_requests.get(f"assist_locations:{team_id}")
                            == _pbp_totals_request_descriptor(
                            season=season, season_type="Regular Season",
                            team_id=team_id, from_date=start.isoformat(),
                            to_date=as_of.isoformat(),
                        )
                        )
            if (
                identity["window"] != window
                or not expected_sources.issubset(set(provider_sources))
                or not isinstance(identity.get("collect_before"), str)
                or not isinstance(identity_teams, dict)
                or not isinstance(source_memberships, dict)
                or not expected_sources.issubset(set(source_memberships))
                or not aggregate_requests_valid
                or hashlib.sha256(json.dumps(
                    aggregate_requests, sort_keys=True, separators=(",", ":")
                ).encode()).hexdigest() != aggregate_request_checksum
            ):
                raise ValueError
            governed_collect_before = getattr(governance, "collect_before", None)
            if (
                governed_collect_before is None
                or _aware(datetime.fromisoformat(identity["collect_before"]))
                != _aware(governed_collect_before)
            ):
                raise ValueError
            actual_ids_by_team = {
                str(team_id): sorted(str(game_id) for game_id in game_ids)
                for team_id, game_ids in game_ids_by_team.items()
            }
            if unavailable_candidate:
                governed_ids = (
                    governance.expected_l15_game_ids
                    if window == "l15"
                    else governance.expected_season_game_ids
                )
                if (
                    set(identity_teams) != {
                        str(team_id) for team_id in governance.team_ids
                    }
                    or any(
                        evidence.get("provider_game_ids") != []
                        or sorted(evidence.get("authority_game_ids", ()))
                        != sorted(governed_ids[int(team_id)])
                        for team_id, evidence in identity_teams.items()
                    )
                    or any(source_memberships[source] != {} for source in expected_sources)
                ):
                    raise ValueError
                return MatchupMaterialization(
                    season=season, window=window, cutoff=cutoff,
                    facts=facts, observations=observations,
                    game_ids_by_team={
                        int(team_id): frozenset() for team_id in governance.team_ids
                    },
                    producer=PRODUCER_LEGACY,
                    manifest_id=manifest_id,
                    event_catalog_publication_id=catalog_id,
                    event_catalog_checksum=catalog_checksum,
                    provider_window_identity=window_identity,
                )
            for team_id, game_ids in actual_ids_by_team.items():
                evidence = identity_teams[team_id]
                if (
                    evidence["expected_games"] != len(game_ids)
                    or sorted(str(game_id) for game_id in evidence["authority_game_ids"])
                    != game_ids
                    or sorted(str(game_id) for game_id in evidence["provider_game_ids"])
                    != game_ids
                ):
                    raise ValueError
            for source in expected_sources:
                membership = source_memberships[source]
                if set(membership) != set(actual_ids_by_team):
                    raise ValueError
                for team_id, game_ids in actual_ids_by_team.items():
                    if sorted(str(game_id) for game_id in membership[team_id]) != game_ids:
                        raise ValueError
            if set(identity_teams) != set(actual_ids_by_team):
                raise ValueError
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
            raise MatchupParityError("legacy provider window identity unavailable") from error
        return MatchupMaterialization(
            season=season,
            window=window,
            cutoff=cutoff,
            facts=facts,
            observations=observations,
            game_ids_by_team=game_ids_by_team,
            producer=PRODUCER_LEGACY,
            manifest_id=manifest_id,
            event_catalog_publication_id=catalog_id,
            event_catalog_checksum=catalog_checksum,
            provider_window_identity=window_identity,
            retained_last_good=retained_last_good,
        )


class LedgerGovernanceReader(Protocol):
    """Resolve one exact immutable governed season/cutoff authority."""

    def read_for_composition(self, season: str, cutoff: datetime) -> Any: ...


def _facts_for_surface(
    facts: Iterable[TeamMatchupFact],
    *,
    surface: str,
) -> dict[tuple[int, str], TeamMatchupFact]:
    return {
        (int(fact.team_id), str(fact.stat_key)): fact
        for fact in facts
        if fact.base == surface
    }


def _duplicate_fact_keys(
    facts: Iterable[TeamMatchupFact],
    *,
    surface: str,
) -> frozenset[tuple[int, str]]:
    counts: dict[tuple[int, str], int] = {}
    for fact in facts:
        if fact.base != surface:
            continue
        key = (int(fact.team_id), str(fact.stat_key))
        counts[key] = counts.get(key, 0) + 1
    return frozenset(key for key, count in counts.items() if count > 1)


def _observation_for_surface(
    observations: Iterable[TeamMatchupObservation],
    *,
    surface: str,
) -> TeamMatchupObservation | None:
    for observation in observations:
        if observation.surface == surface:
            return observation
    return None


def _integral_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


def _minutes(value: float | None, unit: str | None) -> float | None:
    if value is None or unit is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    if unit == "seconds":
        value /= 60.0
    elif unit != "minutes":
        return None
    return value


def _aware(value: datetime) -> datetime:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise MatchupParityError("matchup parity requires an aware cutoff")
    return assume_utc(value)


def compare_matchup_materializations(
    legacy: MatchupMaterialization,
    ledger: MatchupMaterialization,
    *,
    surface: str,
    expected_team_ids: Iterable[int],
    expected_game_ids_by_team: Mapping[int, Iterable[str]],
    tolerance: float = MATCHUP_PARITY_TOLERANCE,
    semantic_rule: str | None = None,
    semantic_rule_reason: str | None = None,
) -> MatchupParityReport:
    """Compare two independently produced materializations for one surface.

    Hard facts must be exactly equal or the report fails; only floating
    denominators and derived per-48 rates admit ``tolerance`` and become
    adjudicable semantic differences.  Both sides must cover exactly the
    governed 30-team roster with every contracted metric, no extra teams or
    metrics and exact governed game sets (L15 exactly 15 games per team).
    Both observations must be available unless the legacy facts are explicitly
    marked as same-authority retained last-good evidence.
    """

    if surface not in _SURFACE_COUNT_KEYS:
        raise ValueError(f"unsupported matchup surface {surface}")
    if tolerance != MATCHUP_PARITY_TOLERANCE:
        raise ValueError(
            "matchup parity requires rel_tol=abs_tol=1e-9"
        )

    expected = frozenset(int(team_id) for team_id in expected_team_ids)
    if expected != frozenset(NBA_TEAM_ID_TO_TRICODE):
        raise ValueError("expected_team_ids must name exactly the canonical 30 teams")

    differences: list[MatchupParityDifference] = []
    window = legacy.window

    # Scope: season, window, and exact aware cutoff must agree.
    try:
        cutoffs_aligned = (
            legacy.cutoff is not None
            and ledger.cutoff is not None
            and _aware(legacy.cutoff) == _aware(ledger.cutoff)
        )
    except MatchupParityError:
        cutoffs_aligned = False
    if not cutoffs_aligned:
        differences.append(MatchupParityDifference(
            window, surface, None, "cutoff", ledger.cutoff, legacy.cutoff,
            CLASSIFICATION_CUTOFF_MISMATCH,
        ))
    if legacy.season != ledger.season or legacy.window != ledger.window:
        differences.append(MatchupParityDifference(
            window, surface, None, "scope",
            (ledger.season, ledger.window), (legacy.season, legacy.window),
            CLASSIFICATION_SCOPE_MISMATCH,
        ))
    legacy_authority = (
        legacy.manifest_id,
        legacy.event_catalog_publication_id,
        legacy.event_catalog_checksum,
    )
    ledger_authority = (
        ledger.manifest_id,
        ledger.event_catalog_publication_id,
        ledger.event_catalog_checksum,
    )
    if (
        legacy_authority != ledger_authority
        or any(value is None or value == "" for value in legacy_authority)
        or any(value is None or value == "" for value in ledger_authority)
    ):
        differences.append(MatchupParityDifference(
            window, surface, None, "authority",
            ledger_authority, legacy_authority,
            CLASSIFICATION_AUTHORITY_MISMATCH,
        ))
    if ledger.producer == PRODUCER_LEDGER and (
        not ledger.publication_id or not ledger.payload_checksum
    ):
        differences.append(MatchupParityDifference(
            window, surface, None, "ledger_publication",
            ledger.publication_id, ledger.payload_checksum,
            CLASSIFICATION_AUTHORITY_MISMATCH,
        ))

    legacy_facts = _facts_for_surface(legacy.facts, surface=surface)
    ledger_facts = _facts_for_surface(ledger.facts, surface=surface)
    for team_id, stat_key in sorted(
        _duplicate_fact_keys(legacy.facts, surface=surface)
    ):
        differences.append(MatchupParityDifference(
            window, surface, team_id, stat_key, "duplicate", "duplicate",
            CLASSIFICATION_DUPLICATE_METRIC,
        ))
    for team_id, stat_key in sorted(
        _duplicate_fact_keys(ledger.facts, surface=surface)
    ):
        differences.append(MatchupParityDifference(
            window, surface, team_id, stat_key, "duplicate", "duplicate",
            CLASSIFICATION_DUPLICATE_METRIC,
        ))
    for fact in (*legacy.facts, *ledger.facts):
        if fact.base == surface and fact.slice_key != fact.stat_key:
            differences.append(MatchupParityDifference(
                window, surface, int(fact.team_id),
                f"{fact.slice_key}.{fact.stat_key}", "present", None,
                CLASSIFICATION_EXTRA_METRIC,
            ))
    legacy_teams = {team_id for team_id, _ in legacy_facts}
    ledger_teams = {team_id for team_id, _ in ledger_facts}

    # League-Complete coverage: exactly the governed 30 teams, no extras.
    league_complete = legacy_teams == expected and ledger_teams == expected
    team_identities_exact = legacy_teams == ledger_teams
    if not league_complete:
        differences.append(MatchupParityDifference(
            window, surface, None, "team_coverage",
            sorted(int(team_id) for team_id in ledger_teams),
            sorted(int(team_id) for team_id in legacy_teams),
            CLASSIFICATION_LEAGUE_INCOMPLETE,
        ))
    for team_id in sorted(legacy_teams - ledger_teams):
        differences.append(MatchupParityDifference(
            window, surface, team_id, "identity", None, "present",
            CLASSIFICATION_MISSING_LEDGER_TEAM,
        ))
    for team_id in sorted(ledger_teams - legacy_teams):
        differences.append(MatchupParityDifference(
            window, surface, team_id, "identity", "present", None,
            CLASSIFICATION_MISSING_LEGACY_TEAM,
        ))
    for team_id in sorted(legacy_teams - expected):
        differences.append(MatchupParityDifference(
            window, surface, team_id, "identity", None, "present",
            CLASSIFICATION_EXTRA_TEAM,
        ))
    for team_id in sorted(ledger_teams - expected):
        differences.append(MatchupParityDifference(
            window, surface, team_id, "identity", "present", None,
            CLASSIFICATION_EXTRA_TEAM,
        ))

    # Surface presence: both sides must carry facts for the surface.
    if not legacy_facts or not ledger_facts:
        differences.append(MatchupParityDifference(
            window, surface, None, "surface",
            "present" if ledger_facts else None,
            "present" if legacy_facts else None,
            CLASSIFICATION_MISSING_SURFACE,
        ))

    # Exact governed game sets: no missing or extra teams or game keys, and L15
    # exactly 15 games per team.  Byte-identical checksums prove equality.
    governed_game_ids = {
        int(team_id): frozenset(str(game_id) for game_id in game_ids)
        for team_id, game_ids in expected_game_ids_by_team.items()
    }
    game_sets_exact = True
    if set(governed_game_ids) != expected:
        game_sets_exact = False
        differences.append(MatchupParityDifference(
            window, surface, None, "game_set_keys",
            sorted(governed_game_ids), sorted(expected),
            CLASSIFICATION_GAME_SET_MISMATCH,
        ))
    for team_id in sorted(expected):
        legacy_ids = legacy.game_ids_by_team.get(team_id, frozenset())
        ledger_ids = ledger.game_ids_by_team.get(team_id, frozenset())
        governed_ids = governed_game_ids.get(team_id, frozenset())
        if window == "l15" and (
            len(governed_ids) != 15 or len(legacy_ids) != 15 or len(ledger_ids) != 15
        ):
            game_sets_exact = False
            differences.append(MatchupParityDifference(
                window, surface, team_id, "l15_game_count",
                (len(ledger_ids), len(ledger_ids)), (len(governed_ids), len(legacy_ids)),
                CLASSIFICATION_L15_GAME_COUNT_MISMATCH,
            ))
        if not (legacy_ids == ledger_ids == governed_ids):
            game_sets_exact = False
            differences.append(MatchupParityDifference(
                window, surface, team_id, "game_ids",
                sorted(ledger_ids), sorted(legacy_ids),
                CLASSIFICATION_GAME_SET_MISMATCH,
            ))
    for team_id in sorted(set(legacy.game_ids_by_team) - expected):
        differences.append(MatchupParityDifference(
            window, surface, team_id, "game_ids", None, "present",
            CLASSIFICATION_EXTRA_TEAM,
        ))
    for team_id in sorted(set(ledger.game_ids_by_team) - expected):
        differences.append(MatchupParityDifference(
            window, surface, team_id, "game_ids", "present", None,
            CLASSIFICATION_EXTRA_TEAM,
        ))
    legacy_checksum = ledger_checksum = None
    if legacy.game_ids_by_team and ledger.game_ids_by_team:
        legacy_checksum = LedgerLineage.for_game_ids(
            game_id for ids in legacy.game_ids_by_team.values() for game_id in ids
        )
        ledger_checksum = LedgerLineage.for_game_ids(
            game_id for ids in ledger.game_ids_by_team.values() for game_id in ids
        )
        if legacy_checksum != ledger_checksum:
            game_sets_exact = False
            differences.append(MatchupParityDifference(
                window, surface, None, "game_set_checksum",
                ledger_checksum, legacy_checksum,
                CLASSIFICATION_GAME_SET_MISMATCH,
            ))

    # Per-team metrics: every contracted metric must be present on both sides,
    # integer counts exactly equal, denominators and derived rates tolerant.
    stat_to_metric = _SURFACE_COUNT_KEYS[surface]
    compared = 0
    rates_by_side: dict[str, dict[str, dict[int, float]]] = {
        "ledger": {},
        "legacy": {},
    }
    shared_teams = sorted(ledger_teams & legacy_teams)
    for stat_key in stat_to_metric:
        for team_id in shared_teams:
            key = (team_id, stat_key)
            ledger_fact = ledger_facts.get(key)
            legacy_fact = legacy_facts.get(key)
            if ledger_fact is None or legacy_fact is None:
                differences.append(MatchupParityDifference(
                    window, surface, team_id, stat_key,
                    "present" if ledger_fact is not None else None,
                    "present" if legacy_fact is not None else None,
                    CLASSIFICATION_MISSING_METRIC,
                ))
                continue
            compared += 1
            ledger_count = _integral_or_none(ledger_fact.raw_value)
            legacy_count = _integral_or_none(legacy_fact.raw_value)
            if ledger_count is None or legacy_count is None:
                differences.append(MatchupParityDifference(
                    window, surface, team_id, stat_key,
                    ledger_fact.raw_value, legacy_fact.raw_value,
                    CLASSIFICATION_NON_INTEGER_COUNT,
                ))
            elif ledger_count != legacy_count:
                differences.append(MatchupParityDifference(
                    window, surface, team_id, stat_key,
                    ledger_count, legacy_count,
                    CLASSIFICATION_INTEGER_COUNT_DIFFERENCE,
                ))
            ledger_minutes = _minutes(
                ledger_fact.denominator_value, ledger_fact.denominator_unit
            )
            legacy_minutes = _minutes(
                legacy_fact.denominator_value, legacy_fact.denominator_unit
            )
            if legacy_minutes is not None and legacy.producer != PRODUCER_LEDGER:
                # The legacy PBP aggregate reports its window minutes from
                # summed player seconds at a tenth-of-a-second grain; the
                # contract denominator is the nominal game length, so read the
                # legacy value as the nominal length it establishes.
                nominal = nominal_window_minutes(
                    legacy_minutes,
                    len(legacy.game_ids_by_team.get(team_id, ())),
                )
                if nominal is not None:
                    legacy_minutes = nominal
            if ledger_minutes is None or legacy_minutes is None:
                differences.append(MatchupParityDifference(
                    window, surface, team_id, "denominator_minutes",
                    ledger_minutes, legacy_minutes,
                    CLASSIFICATION_INVALID_DENOMINATOR,
                ))
                ledger_rate = legacy_rate = None
            else:
                denominator_matches = math.isclose(
                    ledger_minutes, legacy_minutes,
                    rel_tol=tolerance, abs_tol=tolerance,
                )
                if not denominator_matches:
                    differences.append(MatchupParityDifference(
                        window, surface, team_id, "denominator_minutes",
                        ledger_minutes, legacy_minutes,
                        CLASSIFICATION_DENOMINATOR_TOLERANCE_EXCEEDED,
                    ))
                calculated_ledger_rate = _per48(
                    ledger_fact.raw_value, ledger_minutes
                )
                legacy_rate = _per48(legacy_fact.raw_value, legacy_minutes)
                if ledger.producer == PRODUCER_LEDGER:
                    served_ledger_rate = ledger.served_per48.get(key)
                    if served_ledger_rate is None or (
                        calculated_ledger_rate is None
                        or not math.isclose(
                            served_ledger_rate,
                            calculated_ledger_rate,
                            rel_tol=tolerance,
                            abs_tol=tolerance,
                        )
                    ):
                        differences.append(MatchupParityDifference(
                            window,
                            surface,
                            team_id,
                            f"served_per48.{stat_key}",
                            served_ledger_rate,
                            calculated_ledger_rate,
                            CLASSIFICATION_SERVED_RATE_MISMATCH,
                        ))
                    ledger_rate = served_ledger_rate
                else:
                    ledger_rate = calculated_ledger_rate
            if ledger_rate is not None and legacy_rate is not None and not math.isclose(
                ledger_rate, legacy_rate, rel_tol=tolerance, abs_tol=tolerance
            ):
                differences.append(MatchupParityDifference(
                    window, surface, team_id, f"per48.{stat_key}",
                    ledger_rate, legacy_rate,
                    CLASSIFICATION_DERIVED_RATE_DIFFERENCE,
                ))
            if ledger_rate is not None:
                rates_by_side["ledger"].setdefault(stat_key, {})[team_id] = ledger_rate
            if legacy_rate is not None:
                rates_by_side["legacy"].setdefault(stat_key, {})[team_id] = legacy_rate

    # Extra metrics: a fact whose stat key is not a contracted metric.
    for (team_id, stat_key) in sorted(ledger_facts):
        if stat_key not in stat_to_metric:
            differences.append(MatchupParityDifference(
                window, surface, team_id, stat_key, "present", None,
                CLASSIFICATION_EXTRA_METRIC,
            ))
    for (team_id, stat_key) in sorted(legacy_facts):
        if stat_key not in stat_to_metric:
            differences.append(MatchupParityDifference(
                window, surface, team_id, stat_key, None, "present",
                CLASSIFICATION_EXTRA_METRIC,
            ))

    # Deterministic rankings per metric (ascending, ties 1, 1, 3).
    rankings_deterministic = True
    for stat_key in stat_to_metric:
        ledger_metric = rates_by_side["ledger"].get(stat_key, {})
        legacy_metric = rates_by_side["legacy"].get(stat_key, {})
        if not ledger_metric and not legacy_metric:
            continue
        ledger_ranks = competition_ranks(ledger_metric, descending=False)
        legacy_ranks = competition_ranks(legacy_metric, descending=False)
        if ledger.producer == PRODUCER_LEDGER:
            served_ledger_ranks = {
                team_id: ledger.served_ranks.get((team_id, stat_key))
                for team_id in ledger_metric
            }
            if any(rank is None for rank in served_ledger_ranks.values()) or (
                served_ledger_ranks != ledger_ranks
            ):
                rankings_deterministic = False
                differences.append(MatchupParityDifference(
                    window,
                    surface,
                    None,
                    f"served_rank.{stat_key}",
                    served_ledger_ranks,
                    ledger_ranks,
                    CLASSIFICATION_SERVED_RANK_MISMATCH,
                ))
        if ledger_ranks != legacy_ranks:
            rankings_deterministic = False
            differences.append(MatchupParityDifference(
                window, surface, None, f"competition_rank.{stat_key}",
                ledger_ranks, legacy_ranks,
                CLASSIFICATION_RANKING_DIFFERENCE,
            ))

    # Independent per-surface availability. Same-authority retained legacy
    # facts remain valid while their latest failed attempt stays observable.
    legacy_observation = _observation_for_surface(legacy.observations, surface=surface)
    ledger_observation = _observation_for_surface(ledger.observations, surface=surface)
    legacy_available = (
        legacy_observation is not None
        and (
            legacy_observation.status == "available"
            or (legacy.retained_last_good and bool(legacy_facts))
        )
    )
    ledger_available = (
        ledger_observation is not None and ledger_observation.status == "available"
    )
    if not legacy_available or not ledger_available:
        differences.append(MatchupParityDifference(
            window, surface, None, "availability",
            ledger_observation.status if ledger_observation else None,
            legacy_observation.status if legacy_observation else None,
            CLASSIFICATION_AVAILABILITY_DIFFERENCE,
        ))

    return MatchupParityReport(
        season=legacy.season,
        window=window,
        surface=surface,
        cutoff=legacy.cutoff if cutoffs_aligned else ledger.cutoff or legacy.cutoff,
        tolerance=tolerance,
        expected_team_ids=expected,
        league_complete=league_complete,
        team_identities_exact=team_identities_exact,
        game_sets_exact=game_sets_exact,
        cutoffs_aligned=cutoffs_aligned,
        rankings_deterministic=rankings_deterministic,
        compared_count=compared,
        ledger_publication_id=ledger.publication_id,
        ledger_payload_checksum=ledger.payload_checksum,
        differences=tuple(differences),
        legacy_game_ids_by_team={
            int(team_id): tuple(sorted(str(game_id) for game_id in game_ids))
            for team_id, game_ids in legacy.game_ids_by_team.items()
        },
        ledger_game_ids_by_team={
            int(team_id): tuple(sorted(str(game_id) for game_id in game_ids))
            for team_id, game_ids in ledger.game_ids_by_team.items()
        },
        legacy_game_set_checksum=legacy_checksum,
        ledger_game_set_checksum=ledger_checksum,
        legacy_manifest_id=legacy.manifest_id,
        legacy_event_catalog_publication_id=legacy.event_catalog_publication_id,
        legacy_event_catalog_checksum=legacy.event_catalog_checksum,
        ledger_manifest_id=ledger.manifest_id,
        ledger_event_catalog_publication_id=ledger.event_catalog_publication_id,
        ledger_event_catalog_checksum=ledger.event_catalog_checksum,
        semantic_rule=semantic_rule,
        semantic_rule_reason=semantic_rule_reason,
    )


class MatchupParityRunner:
    """Run one bounded dual-run against the immutable governed authority.

    The runner resolves the exact governed roster and game sets from the
    injected governance reader, produces the legacy side from the injected
    legacy materializer seam, derives the ledger side from the verified
    candidate publications, and records per-stream artifacts bound to their
    exact publication and payload checksum.  It never reads or advances a
    ``PublicationPointer``; hard failures are retained as blocking evidence.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        governance,
        legacy_source: LegacyMatchupSource | None = None,
        parity_repository=None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.engine = engine
        self.governance = governance
        self.legacy_source = legacy_source
        from app.services.ledger_parity import (
            LedgerParityArtifactRepository,
            LegacyMatchupDiagnosticCaptureRepository,
        )

        self._parity_repository = parity_repository or LedgerParityArtifactRepository(
            engine, clock=clock
        )
        self._legacy_capture_repository = LegacyMatchupDiagnosticCaptureRepository(
            engine, clock=clock
        )
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def run(
        self,
        season: str,
        window: str,
        *,
        cutoff: datetime,
        legacy: MatchupMaterialization | None = None,
        publications: Mapping[str, str] | None = None,
        session: Session | None = None,
        semantic_rule: str | None = None,
        semantic_rule_reason: str | None = None,
    ) -> tuple[MatchupParityReport, ...]:
        """Compare every ledger-owned surface and optionally record artifacts.

        ``publications`` maps a stream key to its candidate publication ID.
        The legacy side must be produced by the injected materializer source;
        caller-supplied materialization documents are intentionally rejected.
        """

        if cutoff is None or cutoff.tzinfo is None:
            raise MatchupParityError("matchup parity requires an aware immutable cutoff")
        if window not in {"season", "l15"}:
            raise MatchupParityError("matchup parity window is invalid")
        if legacy is not None:
            raise MatchupParityError("legacy materializer source is required")
        governance = self.governance.read_for_composition(season, cutoff)
        if self.legacy_source is None:
            raise MatchupParityError("legacy materializer source is required")
        stored_legacy = isinstance(self.legacy_source, StoredLegacyMatchupSource)
        legacy_materialization = None
        if not stored_legacy:
            try:
                legacy_materialization = self.legacy_source.produce(
                    season=season, window=window, cutoff=cutoff,
                    governance=governance, session=session,
                )
            except TypeError as error:
                if "session" not in str(error):
                    raise
                legacy_materialization = self.legacy_source.produce(
                    season=season, window=window, cutoff=cutoff,
                    governance=governance,
                )
            self._validate_legacy_provenance(
                legacy_materialization, season, window, cutoff, governance
            )
        expected_team_ids = frozenset(int(team_id) for team_id in governance.team_ids)
        if expected_team_ids != frozenset(NBA_TEAM_ID_TO_TRICODE):
            raise MatchupParityError("governed team roster is not the canonical 30-team set")
        expected_game_ids_by_team = (
            governance.expected_l15_game_ids
            if window == "l15"
            else governance.expected_season_game_ids
        )
        expected_streams = {
            matchup_stream_key(surface, window) for surface in LEDGER_OWNED_SURFACES
        }
        supplied_streams = set(publications or {})
        if supplied_streams != expected_streams:
            raise MatchupParityError(
                "both traditional and assist matchup publications are required"
            )
        reports: list[MatchupParityReport] = []
        if session is None:
            owned_session = self._session_factory()
            session_scope = owned_session
            transaction_scope = owned_session.begin()
        else:
            session_scope = nullcontext(session)
            transaction_scope = nullcontext()
        with session_scope as active_session, transaction_scope:
            for surface in LEDGER_OWNED_SURFACES:
                if stored_legacy:
                    legacy_materialization = self.legacy_source.produce(
                        season=season, window=window, cutoff=cutoff,
                        governance=governance, session=active_session,
                        surface=surface,
                    )
                    self._validate_legacy_provenance(
                        legacy_materialization, season, window, cutoff, governance
                    )
                assert legacy_materialization is not None
                stream_key = matchup_stream_key(surface, window)
                publication_id = (publications or {}).get(stream_key)
                if publication_id is None:
                    continue
                publication = resolve_matchup_publication(
                    active_session,
                    publication_id=publication_id,
                    stream_key=stream_key,
                    season=season,
                    cutoff=cutoff,
                    manifest_id=governance.manifest_id,
                    event_catalog_publication_id=(
                        governance.event_catalog_publication_id
                    ),
                    event_catalog_checksum=governance.event_catalog_checksum,
                )
                ledger = materialization_from_publication(publication, surface=surface)
                legacy_capture = self._legacy_capture_repository.record(
                    legacy_materialization, surface=surface,
                    publication_id=publication.publication_id,
                    session=active_session,
                )
                report = compare_matchup_materializations(
                    legacy_materialization,
                    ledger,
                    surface=surface,
                    expected_team_ids=expected_team_ids,
                    expected_game_ids_by_team=expected_game_ids_by_team,
                    semantic_rule=semantic_rule,
                    semantic_rule_reason=semantic_rule_reason,
                )
                reports.append(report)
                self._parity_repository.record_matchup_parity(
                    stream_key,
                    cutoff=cutoff,
                    report=report,
                    publication_id=publication.publication_id,
                    payload_checksum=publication.payload_checksum,
                    session=active_session,
                    legacy_capture=legacy_capture,
                )
        return tuple(reports)

    @staticmethod
    def _validate_legacy_provenance(
        legacy: MatchupMaterialization,
        season: str,
        window: str,
        cutoff: datetime,
        governance,
    ) -> None:
        if legacy.producer != PRODUCER_LEGACY:
            raise MatchupParityError("legacy materialization producer mismatch")
        try:
            cutoff_matches = _aware(legacy.cutoff) == _aware(cutoff)
        except MatchupParityError:
            cutoff_matches = False
        if legacy.season != season or legacy.window != window or not cutoff_matches:
            raise MatchupParityError("legacy materialization scope mismatch")
        if (
            legacy.manifest_id != governance.manifest_id
            or legacy.event_catalog_publication_id
            != governance.event_catalog_publication_id
            or legacy.event_catalog_checksum != governance.event_catalog_checksum
        ):
            raise MatchupParityError("legacy materialization authority mismatch")


def _per48(raw_value: float | None, minutes: float | None) -> float | None:
    if raw_value is None or minutes is None:
        return None
    if minutes <= 0 or not math.isfinite(float(raw_value)) or not math.isfinite(float(minutes)):
        return None
    return float(raw_value) * _PER48 / minutes


__all__ = [
    "CLASSIFICATION_AUTHORITY_MISMATCH",
    "CLASSIFICATION_AVAILABILITY_DIFFERENCE",
    "CLASSIFICATION_CUTOFF_MISMATCH",
    "CLASSIFICATION_DENOMINATOR_TOLERANCE_EXCEEDED",
    "CLASSIFICATION_DERIVED_RATE_DIFFERENCE",
    "CLASSIFICATION_DUPLICATE_METRIC",
    "CLASSIFICATION_EXTRA_METRIC",
    "CLASSIFICATION_EXTRA_TEAM",
    "CLASSIFICATION_GAME_SET_MISMATCH",
    "CLASSIFICATION_INTEGER_COUNT_DIFFERENCE",
    "CLASSIFICATION_INVALID_DENOMINATOR",
    "CLASSIFICATION_L15_GAME_COUNT_MISMATCH",
    "CLASSIFICATION_LEAGUE_INCOMPLETE",
    "CLASSIFICATION_MISSING_LEGACY_TEAM",
    "CLASSIFICATION_MISSING_LEDGER_TEAM",
    "CLASSIFICATION_MISSING_METRIC",
    "CLASSIFICATION_MISSING_SURFACE",
    "CLASSIFICATION_NON_INTEGER_COUNT",
    "CLASSIFICATION_RANKING_DIFFERENCE",
    "CLASSIFICATION_SCOPE_MISMATCH",
    "CLASSIFICATION_SERVED_RATE_MISMATCH",
    "CLASSIFICATION_SERVED_RANK_MISMATCH",
    "HARD_CLASSIFICATIONS",
    "LEDGER_OWNED_SURFACES",
    "LegacyMatchupSource",
    "LedgerGovernanceReader",
    "LedgerTeamWindowRow",
    "MATCHUP_PARITY_TOLERANCE",
    "MatchupMaterialization",
    "MatchupParityDifference",
    "MatchupParityError",
    "MatchupParityReport",
    "MatchupParityRunner",
    "MatchupPublication",
    "PRODUCER_LEDGER",
    "PRODUCER_LEGACY",
    "SOFT_CLASSIFICATIONS",
    "StoredLegacyMatchupSource",
    "compare_matchup_materializations",
    "materialization_from_publication",
    "matchup_surface_stream_keys",
    "matchup_stream_key",
    "resolve_matchup_publication",
]
