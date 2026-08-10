"""Internal, deadline-bounded collection of NBA DFS provider snapshots.

The board collector is deliberately smaller than a public board API.  It owns
provider selection, bounded fan-out, expected provider-failure translation,
and deterministic ordering.  Adapters remain the owners of wire formats and
return one immutable :class:`ProviderSnapshot` per observation.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable

import requests

from app.config.settings import ConfigurationError, RuntimeSettings
from app.dfs_catalog import DFS_PROVIDER_NAMES
from app.errors import ProviderUnavailableError
from app.services.athlete_mapping_errors import AthleteMappingPersistenceError
from app.services.athlete_resolver import (
    MappingResolutionState,
    normalize_athlete_name,
)
from app.services.event_mapping_errors import EventMappingPersistenceError
from app.services.event_resolver import EventResolutionState, normalize_team_label
from app.providers.dfs import (
    AthleteEvidence,
    CoverageCode,
    DeadlineExceededError,
    EventEvidence,
    MalformedProviderResponseError,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    ProviderSnapshotProvider,
    RetrievalContext,
    SnapshotStatus,
    StatisticEvidence,
    TeamEvidence,
)
from app.domain.statistics import MatchReason, MatchState, StatisticMatch
from app.services.statistic_catalog import StatisticCatalog, StatisticResolver
from app.utils.telemetry import ProviderResponseError
from app.utils.telemetry import (
    BoardTelemetryEvent,
    BoardTelemetryRecorder,
    BoundedBoardTelemetryRecorder,
)

logger = logging.getLogger(__name__)

DEFAULT_BOARD_DEADLINE_SECONDS = 15.0
DEFAULT_BOARD_MAX_CONCURRENCY = 3

__all__ = [
    "DFSBoard",
    "DFSBoardService",
    "ProviderFailureReason",
    "ProviderOutcome",
    "ProviderOutcomeStatus",
]

# CoverageCode is a closed shared vocabulary. Keep the board's semantic
# classification closed too, so newly reviewed malformed/pagination codes
# cannot silently fall through to ``upstream_error``.
_MALFORMED_COVERAGE_CODES = frozenset(
    {
        CoverageCode.CONFLICTING_SOURCE_IDENTITY,
        CoverageCode.FIXTURE_MALFORMED,
        CoverageCode.MISSING_COMPETITION_ID,
        CoverageCode.MALFORMED_RECORD,
        CoverageCode.PAGE_MALFORMED,
        CoverageCode.PAGE_METADATA_MISMATCH,
        CoverageCode.PAGINATION_EXPECTED_TOTAL_CHANGED,
        CoverageCode.PAGINATION_METADATA_MALFORMED,
        CoverageCode.PAGINATION_PAGE_MISMATCH,
        CoverageCode.PAGINATION_TOTAL_PAGES_CHANGED,
    }
)
_UPSTREAM_COVERAGE_CODES = frozenset(
    {
        CoverageCode.FIXTURE_FAILED,
        CoverageCode.FIXTURE_LIST_FAILED,
        CoverageCode.PAGE_FETCH_FAILED,
    }
)


class ProviderOutcomeStatus(str, Enum):
    """Stable status for one enabled provider attempt."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class ProviderFailureReason(str, Enum):
    """Safe, provider-independent reasons exposed by the collector."""

    TIMEOUT = "timeout"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    RATE_LIMITED = "rate_limited"
    ACCESS_DENIED = "access_denied"
    UPSTREAM_ERROR = "upstream_error"
    MALFORMED_RESPONSE = "malformed_response"


def _provider_name(value: Any) -> str:
    """Normalize one provider name without exposing arbitrary source labels."""

    name = value.strip().casefold() if isinstance(value, str) else ""
    if not name:
        raise ValueError("DFS provider name must be a non-empty string")
    return name


def _normalize_names(values: Iterable[str]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = _provider_name(value)
        if name not in seen:
            names.append(name)
            seen.add(name)
    return tuple(names)


def _identity_key(resolution: Any) -> tuple[str, str] | None:
    """The provider identity one resolution describes, if it names one."""

    provider_athlete_id = getattr(resolution, "provider_athlete_id", None)
    if not isinstance(provider_athlete_id, str) or not provider_athlete_id.strip():
        return None
    return resolution.provider, provider_athlete_id.strip()


#: The athlete facts two observations of one identity must agree on.
_ATHLETE_FACTS = ("name", "canonical_id")

#: The team facts, strongest identifying tier first.  Only the strongest tier
#: both observations carry decides, exactly as the resolver judges provider
#: team evidence: agreeing identities make a differing label a presentation
#: difference rather than a different team.
_TEAM_FACT_TIERS = (
    ("team_canonical_id", "team_provider_id"),
    ("team_abbreviation",),
    ("team_name",),
)


def _evidence_facts(resolution: Any) -> dict[str, Any]:
    """The identity facts one observation asserts, normalized for comparison.

    Only what could name a different athlete is read: the athlete's name and
    canonical ID, and the team evidence at every tier the provider reported.
    Presentation differences the resolver already ignores -- accents, case,
    punctuation -- are ignored here too, so equivalent spellings of one athlete
    are one assertion rather than a contradiction.  A fact the provider did not
    report is ``None``, which asserts nothing at all about the identity.
    """

    evidence = resolution.provider_evidence
    team = evidence.team
    return {
        "name": normalize_athlete_name(evidence.name) or None,
        "canonical_id": evidence.canonical_id,
        "team_canonical_id": None if team is None else team.canonical_id,
        "team_provider_id": (
            None if team is None else (team.provider_id or "").strip() or None
        ),
        "team_abbreviation": (
            None if team is None else (team.abbreviation or "").strip().casefold() or None
        ),
        "team_name": (None if team is None else normalize_athlete_name(team.name) or None),
    }


def _evidence_order(resolution: Any) -> tuple[str, ...]:
    """A total order over observations that depends only on their evidence."""

    facts = _evidence_facts(resolution)
    return tuple("" if facts[fact] is None else str(facts[fact]) for fact in sorted(facts))


def _contradicts(
    facts: Mapping[str, Any],
    other: Mapping[str, Any],
    *,
    direct: tuple[str, ...],
    tier_chains: tuple[tuple[tuple[str, ...], ...], ...],
) -> bool:
    """Report whether two observations of one identity name different things.

    Only facts both sides actually assert are comparable.  Evidence that adds a
    canonical ID, a team, or a stronger team identifier to what a sparser
    market reported is the same subject described more completely, so it agrees
    rather than contradicting; a fact whose value changed names something else.

    ``direct`` facts are compared one by one.  Each chain in ``tier_chains``
    describes one subject named at several strengths -- an athlete's team, or an
    event's home and away sides -- and only the strongest tier both sides carry
    decides for that subject, so agreeing identities make a differing label a
    presentation difference.
    """

    for fact in direct:
        known, current = facts[fact], other[fact]
        if known is not None and current is not None and known != current:
            return True
    for chain in tier_chains:
        for tier in chain:
            comparable = [
                (facts[fact], other[fact])
                for fact in tier
                if facts[fact] is not None and other[fact] is not None
            ]
            if comparable:
                if any(known != current for known, current in comparable):
                    return True
                break
    return False


def _athlete_contradicts(facts: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    return _contradicts(
        facts, other, direct=_ATHLETE_FACTS, tier_chains=(_TEAM_FACT_TIERS,)
    )


def _contradictory_identities(
    observed: Iterable[tuple[tuple[str, str] | None, Any]],
    *,
    facts_of: Callable[[Any], dict[str, Any]] = _evidence_facts,
    contradicts: Callable[[Mapping[str, Any], Mapping[str, Any]], bool] = (
        _athlete_contradicts
    ),
) -> set[tuple[str, str]]:
    """Identities one board read observed asserting more than one subject.

    Every observation of an identity is compared with every other one, rather
    than with a running merge of the facts observed so far.  A merge answers
    for evidence no single market reported -- a team named by canonical ID in
    one market and by an abbreviation in another becomes one team that a third
    market is then judged against -- and a weaker fact overwrites a stronger
    one, so which disagreements stay visible depends on the order the markets
    arrived in.  Comparing the observations themselves makes the answer a
    property of the whole set: an identity is contradictory exactly when two of
    its markets disagree, in every order the provider could list them in.
    """

    observations: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for key, resolution in observed:
        if key is None:
            continue
        facts = facts_of(resolution)
        asserted = observations.setdefault(key, [])
        # Markets that assert exactly the same facts are one assertion, and
        # comparing a repeat with itself can never find a disagreement.
        if facts not in asserted:
            asserted.append(facts)
    return {
        key
        for key, asserted in observations.items()
        if any(
            contradicts(facts, other)
            for position, facts in enumerate(asserted)
            for other in asserted[position + 1 :]
        )
    }


def _fail_closed_conflict(resolutions: Iterable[Any]) -> Any:
    """One governed conflict for an identity its own board read contradicts.

    Every snapshot is temporally coherent, so one provider identity carrying
    two different athletes across its markets is not a sequence of observations
    that supersede each other -- it is one observation that cannot say who the
    identity is.  Resolving the markets in turn would let their arrival order
    decide, because each market is judged against whatever the previous one
    stored: an unmatched name followed by an exact one maps the identity
    automatically, while the reverse promotes a conflict.  The identity
    therefore fails closed on the contradiction itself, whatever the order, and
    never enters a canonical comparison on evidence that disagrees with itself.

    The contradiction is the whole set of evidence, so the result carries all
    of it: governance is rechecked against every athlete the markets named, and
    an operator keeps each one as a candidate to review.  What the conflict is
    recorded *on* -- the representative evidence, the candidate order -- is
    chosen by sorting the observations rather than by taking the first, so the
    same markets in any order produce the same durable observation and a
    reordered repeat of one snapshot recognizes itself instead of appending a
    second conflict.
    """

    resolutions = sorted(resolutions, key=_evidence_order)
    candidates: list[Any] = []
    evidence: list[Any] = []
    seen_evidence: set[tuple[str, ...]] = set()
    for resolution in resolutions:
        canonical = resolution.canonical_athlete
        if canonical is not None and canonical not in candidates:
            candidates.append(canonical)
        key = _evidence_order(resolution)
        if key not in seen_evidence:
            seen_evidence.add(key)
            evidence.append(resolution.provider_evidence)
    candidates.sort(key=lambda candidate: candidate.player_id)
    return replace(
        resolutions[0],
        state=MappingResolutionState.MAPPING_CONFLICT,
        # One athlete the markets disagreed over is the candidate an operator
        # weighs; several equally named ones name none of them.
        canonical_athlete=(candidates[0] if len(candidates) == 1 else None),
        candidates=tuple(candidates),
        contradictory_evidence=tuple(evidence),
        reason="contradictory_provider_evidence",
    )


def _identity_groups(
    observed: Iterable[tuple[tuple[str, str] | None, Any]],
) -> tuple[tuple[tuple[str, str] | None, tuple[Any, ...]], ...]:
    """Each identity's observations, grouped where the identity first appears.

    A snapshot can carry several markets for the same provider athlete, and
    together they are one observation of it rather than a sequence that
    supersedes itself.  Grouping them keeps every write for one identity
    together and leaves one outcome to report, at the position the board first
    named it.  Evidence that names no provider ID names no identity to group,
    and stands alone where it was observed.
    """

    groups: list[tuple[tuple[str, str] | None, list[Any]]] = []
    positions: dict[tuple[str, str], int] = {}
    for key, resolution in observed:
        if key is None:
            groups.append((None, [resolution]))
            continue
        position = positions.get(key)
        if position is None:
            positions[key] = len(groups)
            groups.append((key, [resolution]))
        else:
            groups[position][1].append(resolution)
    return tuple((key, tuple(group)) for key, group in groups)


def _first_reported(values: Iterable[Any]) -> Any:
    """The first value a market actually reported, or ``None`` if none did."""

    for value in values:
        if value is not None:
            return value
    return None


def _merged_evidence(resolutions: Iterable[Any]) -> AthleteEvidence:
    """Everything one identity's compatible markets reported about it.

    The markets agree about who the identity is, so a fact one of them omits
    is a fact about the same athlete rather than about a different one: one
    market may name the team only by provider ID and another only by
    abbreviation.  Combining them keeps every typed fact the read observed,
    where writing the markets one at a time would leave the durable row
    reporting whichever of them happened to be written last.  Facts are taken
    in evidence order, so the group yields the same combined evidence -- and
    therefore the same idempotency fingerprint -- however the provider listed
    the markets.
    """

    evidence = [
        resolution.provider_evidence
        for resolution in sorted(resolutions, key=_evidence_order)
    ]
    teams = [item.team for item in evidence if item.team is not None]
    return AthleteEvidence(
        provider_id=_first_reported(item.provider_id for item in evidence),
        canonical_id=_first_reported(item.canonical_id for item in evidence),
        name=_first_reported(item.name for item in evidence),
        team=(
            None
            if not teams
            else TeamEvidence(
                provider_id=_first_reported(team.provider_id for team in teams),
                canonical_id=_first_reported(team.canonical_id for team in teams),
                name=_first_reported(team.name for team in teams),
                abbreviation=_first_reported(team.abbreviation for team in teams),
            )
        ),
    )


def _merged_observation(resolutions: Iterable[Any], resolve: Any) -> Any:
    """One identity's compatible markets as a single durable observation.

    A snapshot's markets for one provider athlete are one observation of it,
    not a sequence that supersedes itself, so they are recorded once with the
    combined evidence they reported.  Persisting them one at a time made the
    provider's listing order the decider -- an objection only promoted a claim
    that was already stored, while a claim made after an objection simply
    mapped the identity -- and appended one durable observation per market, so
    an unchanged repeat of the same snapshot kept growing the audit.

    The combined evidence is then resolved as a whole rather than being carried
    onto the least-claiming market's own result.  A market that reported no
    name did not object to the athlete its sibling named, it simply said less,
    and reusing its result left the identity unmapped while the durable row
    recorded the name that would have mapped it.  Resolving the merged evidence
    asks the catalog what the group actually observed, so an objection an
    operator or the catalog genuinely raises -- a rejection, a manual decision,
    a team the catalog contradicts, a conflicting established claim -- is raised
    again against evidence that only grew, while evidence that merely completes
    a sparse market is allowed to claim the identity.  The answer depends on
    the combined evidence alone, so it is the same in every order the provider
    could have listed the markets in.
    """

    ordered = sorted(resolutions, key=_evidence_order)
    merged = _merged_evidence(ordered)
    return resolve(
        ordered[0].provider,
        merged,
        ordered[0].season,
        observed_at=ordered[0].observed_at,
    )


def _event_identity_key(resolution: Any) -> tuple[str, str] | None:
    """The provider event identity one resolution describes, if it names one."""

    provider_event_id = getattr(resolution, "provider_event_id", None)
    if not isinstance(provider_event_id, str) or not provider_event_id.strip():
        return None
    return resolution.provider, provider_event_id.strip()


#: The event facts two observations of one fixture must agree on.  The matchup
#: label is presentation and is deliberately absent: providers spell one game
#: several ways, and the teams and start time are what identify it.
_EVENT_FACTS = ("canonical_id", "starts_at")

#: The home and away sides, each named at every strength the provider may use,
#: strongest identifying tier first.  The two sides are independent: agreeing
#: home identities say nothing about a disagreement over the away team.
_EVENT_TEAM_FACT_TIERS = tuple(
    (
        (f"{side}_canonical_id", f"{side}_provider_id"),
        (f"{side}_abbreviation",),
        (f"{side}_name",),
    )
    for side in ("home_team", "away_team")
)


def _event_evidence_facts(resolution: Any) -> dict[str, Any]:
    """The identity facts one event observation asserts, normalized.

    Only what could name a different NBA game is read: the provider's canonical
    claim, the start time, and the home/away teams at every tier it reported.
    A fact the provider did not report is ``None``, which asserts nothing.
    """

    evidence = resolution.provider_evidence
    facts: dict[str, Any] = {
        "canonical_id": evidence.canonical_id,
        "starts_at": (
            None if evidence.starts_at is None else evidence.starts_at.isoformat()
        ),
    }
    for side in ("home_team", "away_team"):
        team = getattr(evidence, side)
        facts[f"{side}_canonical_id"] = None if team is None else team.canonical_id
        facts[f"{side}_provider_id"] = (
            None if team is None else (team.provider_id or "").strip() or None
        )
        facts[f"{side}_abbreviation"] = (
            None if team is None else (team.abbreviation or "").strip().casefold() or None
        )
        facts[f"{side}_name"] = (
            None if team is None else normalize_team_label(team.name) or None
        )
    return facts


def _event_contradicts(facts: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    return _contradicts(
        facts, other, direct=_EVENT_FACTS, tier_chains=_EVENT_TEAM_FACT_TIERS
    )


#: Every field of one event evidence the board retains, in the order they are
#: compared.  Identity facts alone cannot order two observations: markets that
#: name the same fixture may still spell its matchup label, status, end time, or
#: update instant differently, and those are exactly the facts a merge carries
#: onto the durable row.
_EVENT_EVIDENCE_FIELDS = (
    "provider_id",
    "canonical_id",
    "label",
    "starts_at",
    "ends_at",
    "updated_at",
    "status_label",
)
_EVENT_TEAM_EVIDENCE_FIELDS = ("provider_id", "canonical_id", "name", "abbreviation")


def _event_evidence_order(resolution: Any) -> tuple[tuple[bool, str], ...]:
    """A total order over event observations depending only on their evidence.

    Every retained field takes part, so two observations compare equal only
    when they report exactly the same evidence.  Ordering on the identity facts
    alone left presentation-only differences tied, and a stable sort then let
    the provider's listing order decide which label, status, end time, and
    update instant the merged observation carried -- so a reordered read of one
    snapshot rewrote the durable row and appended a second observation.  A fact
    nothing reported is ordered after every reported one rather than compared as
    an empty string, which no provider can then collide with.
    """

    evidence = resolution.provider_evidence
    values = [getattr(evidence, field) for field in _EVENT_EVIDENCE_FIELDS]
    for side in ("home_team", "away_team"):
        team = getattr(evidence, side)
        values.extend(
            None if team is None else getattr(team, field)
            for field in _EVENT_TEAM_EVIDENCE_FIELDS
        )
    return tuple(
        (value is None, "" if value is None else str(value)) for value in values
    )


def _fail_closed_event_conflict(resolutions: Iterable[Any]) -> Any:
    """One governed conflict for a fixture its own board read contradicts.

    Every snapshot is temporally coherent, so one provider event ID carrying two
    different matchups or start times across its markets is not a sequence of
    observations that supersede each other -- it is one observation that cannot
    say which game the fixture is.  Resolving the markets in turn would let
    their arrival order decide, so the identity fails closed on the
    contradiction itself and never enters a canonical comparison on evidence
    that disagrees with itself.  What the conflict is recorded *on* comes from
    sorting the observations rather than from the provider's listing order, so a
    reordered repeat of one snapshot recognizes itself.
    """

    resolutions = sorted(resolutions, key=_event_evidence_order)
    candidates: list[Any] = []
    evidence: list[Any] = []
    seen_evidence: set[tuple[tuple[bool, str], ...]] = set()
    for resolution in resolutions:
        canonical = resolution.canonical_event
        if canonical is not None and canonical not in candidates:
            candidates.append(canonical)
        key = _event_evidence_order(resolution)
        if key not in seen_evidence:
            seen_evidence.add(key)
            evidence.append(resolution.provider_evidence)
    candidates.sort(key=lambda candidate: candidate.nba_game_id)
    return replace(
        resolutions[0],
        state=EventResolutionState.MAPPING_CONFLICT,
        # One game the markets disagreed over is the candidate an operator
        # weighs; several equally named ones name none of them.
        canonical_event=(candidates[0] if len(candidates) == 1 else None),
        candidates=tuple(candidates),
        contradictory_evidence=tuple(evidence),
        reason="contradictory_provider_evidence",
    )


def _event_catalog_unusable(resolutions: Iterable[Any]) -> bool:
    """Whether the season's catalog left this group nothing to be placed against."""

    return any(
        resolution.state is EventResolutionState.EVENT_CATALOG_UNAVAILABLE
        for resolution in resolutions
    )


def _withheld_event_observation(resolutions: Iterable[Any]) -> Any:
    """One withheld outcome for a fixture no schedule can place.

    A missing or over-age Event Catalog withdraws the comparison identity
    itself, so nothing the markets say about the fixture -- including a
    disagreement between them -- is an observation of a canonical game.
    Promoting the group to a conflict or merging it into one claim would record
    a decision about an identity that could not be checked, and would queue that
    decision against an operator's standing one.  The group therefore stays
    unavailable as a whole, and the representative is chosen by sorting the
    observations rather than by taking the first, so the markets in any order
    leave the board reporting the same single outcome.

    Freshness is read once per market, so a refresh aging out part way through
    one board read leaves a group where some markets were placed and others
    were not.  Only what the catalog could not place can represent such a
    group: sorting all of them let a placed resolution stand for the fixture
    whenever its evidence sorted first, which reported -- and persisted -- a
    canonical or governed identity for a read that could not place the fixture
    as a whole.
    """

    return sorted(
        (
            resolution
            for resolution in resolutions
            if resolution.state is EventResolutionState.EVENT_CATALOG_UNAVAILABLE
        ),
        key=_event_evidence_order,
    )[0]


def _merged_event_evidence(resolutions: Iterable[Any]) -> EventEvidence:
    """Everything one fixture's compatible markets reported about it.

    The markets agree about which game the fixture is, so a fact one of them
    omits describes the same game more sparsely: one market may name the home
    team only by provider ID and another only by tricode.  Combining them keeps
    every typed fact the read observed, where writing the markets one at a time
    would leave the durable row reporting whichever was written last.  Facts are
    taken in evidence order, so the group yields the same combined evidence --
    and the same idempotency fingerprint -- however the provider listed them.
    """

    evidence = [
        resolution.provider_evidence
        for resolution in sorted(resolutions, key=_event_evidence_order)
    ]

    def merged_team(side: str) -> TeamEvidence | None:
        teams = [
            team
            for team in (getattr(item, side) for item in evidence)
            if team is not None
        ]
        if not teams:
            return None
        return TeamEvidence(
            provider_id=_first_reported(team.provider_id for team in teams),
            canonical_id=_first_reported(team.canonical_id for team in teams),
            name=_first_reported(team.name for team in teams),
            abbreviation=_first_reported(team.abbreviation for team in teams),
        )

    return EventEvidence(
        provider_id=_first_reported(item.provider_id for item in evidence),
        canonical_id=_first_reported(item.canonical_id for item in evidence),
        label=_first_reported(item.label for item in evidence),
        starts_at=_first_reported(item.starts_at for item in evidence),
        ends_at=_first_reported(item.ends_at for item in evidence),
        updated_at=_first_reported(item.updated_at for item in evidence),
        home_team=merged_team("home_team"),
        away_team=merged_team("away_team"),
        status_label=_first_reported(item.status_label for item in evidence),
    )


def _merged_event_observation(resolutions: Iterable[Any], resolve: Any) -> Any:
    """One fixture's compatible markets as a single durable observation.

    The combined evidence is resolved as a whole rather than inheriting any one
    market's own pre-merge result: a market that reported no start time did not
    object to the game its sibling identified, it simply said less.  Resolving
    the merged evidence re-raises every objection the catalog or an operator
    genuinely holds against evidence that only grew, so the answer depends on
    the combined evidence alone and is the same in every order the provider
    could have listed the markets in.
    """

    ordered = sorted(resolutions, key=_event_evidence_order)
    return resolve(
        ordered[0].provider,
        _merged_event_evidence(ordered),
        ordered[0].season,
        observed_at=ordered[0].observed_at,
    )


@dataclass(frozen=True, slots=True)
class ProviderOutcome:
    """One provider's complete, partial, or failed retrieval observation."""

    provider: str
    status: ProviderOutcomeStatus | str
    snapshot: ProviderSnapshot | None = None
    reason: ProviderFailureReason | str | None = None
    cache_status: str | None = None
    cache_retrieved_at: datetime | str | None = None
    cache_age_seconds: Decimal | float | None = None
    cache_failure_reason: str | None = None
    cache_failure_at: datetime | str | None = None

    def __post_init__(self) -> None:
        provider = _provider_name(self.provider)
        try:
            status = (
                self.status
                if isinstance(self.status, ProviderOutcomeStatus)
                else ProviderOutcomeStatus(self.status)
            )
        except (TypeError, ValueError) as error:
            raise ValueError("provider outcome status is invalid") from error
        if self.snapshot is not None and not isinstance(self.snapshot, ProviderSnapshot):
            raise ValueError("provider outcome snapshot must be ProviderSnapshot or None")
        if self.snapshot is not None and self.snapshot.provider != provider:
            raise ValueError("provider outcome snapshot provider must match outcome provider")
        if status in {ProviderOutcomeStatus.COMPLETE, ProviderOutcomeStatus.PARTIAL}:
            if self.snapshot is None:
                raise ValueError("usable provider outcomes require a snapshot")
            expected = (
                SnapshotStatus.COMPLETE
                if status is ProviderOutcomeStatus.COMPLETE
                else SnapshotStatus.PARTIAL
            )
            if self.snapshot.status is not expected:
                raise ValueError("provider outcome status must match snapshot status")
        elif self.snapshot is not None:
            raise ValueError("failed provider outcomes cannot carry a snapshot")

        reason = self.reason
        if reason is not None:
            try:
                reason = (
                    reason
                    if isinstance(reason, ProviderFailureReason)
                    else ProviderFailureReason(reason)
                )
            except (TypeError, ValueError) as error:
                raise ValueError("provider outcome reason is invalid") from error
        if status is ProviderOutcomeStatus.FAILED and reason is None:
            raise ValueError("failed provider outcomes require a reason")

        cache_status = self.cache_status
        if cache_status is not None and cache_status not in {
            "hit",
            "miss",
            "disabled",
            "stale",
            "error",
        }:
            raise ValueError("provider outcome cache_status is invalid")
        cache_failure_reason = self.cache_failure_reason
        if cache_failure_reason is not None and (
            not isinstance(cache_failure_reason, str) or not cache_failure_reason.strip()
        ):
            raise ValueError("provider outcome cache_failure_reason is invalid")
        cache_age = self.cache_age_seconds
        if cache_age is not None and (
            isinstance(cache_age, bool)
            or not isinstance(cache_age, (int, float, Decimal))
            or cache_age < 0
        ):
            raise ValueError("provider outcome cache_age_seconds must be non-negative")
        cache_retrieved_at = self.cache_retrieved_at
        if cache_retrieved_at is not None:
            if isinstance(cache_retrieved_at, str):
                try:
                    cache_retrieved_at = datetime.fromisoformat(
                        cache_retrieved_at.replace("Z", "+00:00")
                    )
                except ValueError as error:
                    raise ValueError("provider outcome cache_retrieved_at is invalid") from error
            if not isinstance(cache_retrieved_at, datetime) or cache_retrieved_at.tzinfo is None:
                raise ValueError("provider outcome cache_retrieved_at must be timezone-aware")
            cache_retrieved_at = cache_retrieved_at.astimezone(timezone.utc)
        cache_failure_at = self.cache_failure_at
        if cache_failure_at is not None:
            if isinstance(cache_failure_at, str):
                try:
                    cache_failure_at = datetime.fromisoformat(
                        cache_failure_at.replace("Z", "+00:00")
                    )
                except ValueError as error:
                    raise ValueError("provider outcome cache_failure_at is invalid") from error
            if not isinstance(cache_failure_at, datetime) or cache_failure_at.tzinfo is None:
                raise ValueError("provider outcome cache_failure_at must be timezone-aware")
            cache_failure_at = cache_failure_at.astimezone(timezone.utc)

        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "cache_status", cache_status)
        object.__setattr__(self, "cache_failure_reason", cache_failure_reason)
        object.__setattr__(self, "cache_retrieved_at", cache_retrieved_at)
        object.__setattr__(self, "cache_age_seconds", cache_age)
        object.__setattr__(self, "cache_failure_at", cache_failure_at)

    @property
    def usable(self) -> bool:
        """Whether this outcome contributes one coherent provider snapshot."""

        return self.snapshot is not None and self.status in {
            ProviderOutcomeStatus.COMPLETE,
            ProviderOutcomeStatus.PARTIAL,
        }

    @property
    def coverage(self):
        """Expose adapter coverage without copying or flattening its evidence."""

        return self.snapshot.coverage if self.snapshot is not None else None


@dataclass(frozen=True, slots=True)
class DFSBoard:
    """Deterministic collection result for one board retrieval attempt."""

    query: NBAMarketQuery
    provider_outcomes: tuple[ProviderOutcome, ...]
    disabled_providers: tuple[str, ...] = ()
    generated_at: datetime | str = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    contract_version: str = "1"
    mapping_outcomes: tuple[Any, ...] = ()
    event_mapping_outcomes: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.query, NBAMarketQuery):
            raise ValueError("DFS board query must be NBAMarketQuery")
        outcomes = tuple(self.provider_outcomes)
        if any(not isinstance(outcome, ProviderOutcome) for outcome in outcomes):
            raise ValueError("DFS board outcomes must be ProviderOutcome values")
        if tuple(sorted(outcome.provider for outcome in outcomes)) != tuple(
            outcome.provider for outcome in outcomes
        ):
            raise ValueError("DFS board provider outcomes must be deterministic")
        disabled = _normalize_names(self.disabled_providers)
        if tuple(sorted(disabled)) != disabled:
            raise ValueError("DFS board disabled providers must be deterministic")
        if set(disabled) & {outcome.provider for outcome in outcomes}:
            raise ValueError("a provider cannot be enabled and disabled")
        generated = self.generated_at
        if isinstance(generated, str):
            generated = datetime.fromisoformat(generated.replace("Z", "+00:00"))
        if not isinstance(generated, datetime) or generated.tzinfo is None:
            raise ValueError("DFS board generated_at must be timezone-aware")
        if not isinstance(self.contract_version, str) or not self.contract_version.strip():
            raise ValueError("DFS board contract_version must be non-empty")
        mapping_outcomes = tuple(self.mapping_outcomes)
        event_mapping_outcomes = tuple(self.event_mapping_outcomes)
        object.__setattr__(self, "provider_outcomes", outcomes)
        object.__setattr__(self, "disabled_providers", disabled)
        object.__setattr__(self, "generated_at", generated.astimezone(timezone.utc))
        object.__setattr__(self, "contract_version", self.contract_version.strip())
        object.__setattr__(self, "mapping_outcomes", mapping_outcomes)
        object.__setattr__(self, "event_mapping_outcomes", event_mapping_outcomes)

    @property
    def snapshots(self) -> tuple[ProviderSnapshot, ...]:
        """Usable snapshots, retaining each provider's coherent observation."""

        return tuple(
            outcome.snapshot
            for outcome in self.provider_outcomes
            if outcome.usable and outcome.snapshot is not None
        )

    @property
    def usable(self) -> bool:
        """Whether at least one provider produced a usable snapshot."""

        return bool(self.snapshots)

    @property
    def resolved_markets(self) -> tuple[PlayerProjectionMarket, ...]:
        """Flatten usable snapshots after statistic resolution."""

        return tuple(
            market for snapshot in self.snapshots for market in snapshot.markets
        )

    @property
    def canonical_markets(self) -> tuple[PlayerProjectionMarket, ...]:
        """Markets with a reviewed canonical statistic identity."""

        return tuple(
            market
            for market in self.resolved_markets
            if market.statistic_match is not None
            and market.statistic_match.is_comparable
        )

    @property
    def unmapped_markets(self) -> tuple[PlayerProjectionMarket, ...]:
        """Markets retained outside comparisons because their statistic is unmapped."""

        return tuple(
            market
            for market in self.resolved_markets
            if market.statistic_match_state is MatchState.UNMAPPED
        )

    @property
    def non_comparable_markets(self) -> tuple[PlayerProjectionMarket, ...]:
        """Canonical/provider-specific facts excluded from comparisons."""

        return tuple(
            market
            for market in self.resolved_markets
            if market.statistic_match is not None
            and market.statistic_match.state is MatchState.CANONICAL
            and not market.statistic_match.is_comparable
        )

    @property
    def statistic_matches(self) -> tuple[StatisticMatch, ...]:
        """Typed statistic matches retained alongside each resolved market."""

        return tuple(
            market.statistic_match
            for market in self.resolved_markets
            if market.statistic_match is not None
        )


class DFSBoardService:
    """Collect enabled DFS providers under one absolute retrieval deadline."""

    def __init__(
        self,
        provider_registry: Mapping[str, ProviderSnapshotProvider],
        *,
        known_providers: Iterable[str] = DFS_PROVIDER_NAMES,
        max_concurrency: int = DEFAULT_BOARD_MAX_CONCURRENCY,
        deadline_seconds: float = DEFAULT_BOARD_DEADLINE_SECONDS,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        settings: RuntimeSettings | None = None,
        telemetry_recorder: BoardTelemetryRecorder | None = None,
        snapshot_cache: Callable[
            [str, ProviderSnapshotProvider], ProviderSnapshotProvider
        ]
        | None = None,
        statistic_catalog: StatisticCatalog | None = None,
        statistic_resolver: StatisticResolver | None = None,
        athlete_resolver: Any | None = None,
        athlete_mapping_repository: Any | None = None,
        event_resolver: Any | None = None,
        event_mapping_repository: Any | None = None,
    ) -> None:
        registry = self._build_registry(provider_registry)
        decorator = snapshot_cache
        if decorator is not None:
            decorate = getattr(decorator, "decorate", None)
            if callable(decorate):
                decorator = decorate
            if not callable(decorator):
                raise TypeError("DFS snapshot cache decorator must be callable")
            registry = {
                name: decorator(name, provider)
                for name, provider in registry.items()
            }
            registry = self._build_registry(registry)
        enabled = tuple(registry)
        if settings is not None and settings.environment == "production" and not enabled:
            raise ConfigurationError(
                "DFS_ENABLED_PROVIDERS must explicitly configure at least one provider in production"
            )

        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError("DFS board max_concurrency must be a positive integer")
        if not isinstance(deadline_seconds, (int, float)) or isinstance(deadline_seconds, bool):
            raise ValueError("DFS board deadline_seconds must be positive")
        if deadline_seconds <= 0:
            raise ValueError("DFS board deadline_seconds must be positive")

        known = _normalize_names(known_providers)
        self.provider_registry = MappingProxyType(
            {name: registry[name] for name in sorted(registry)}
        )
        self.enabled_providers = tuple(sorted(enabled))
        self.disabled_providers = tuple(sorted(set(known) - set(self.enabled_providers)))
        self.max_concurrency = min(max_concurrency, max(1, len(self.enabled_providers)))
        self.deadline_seconds = min(float(deadline_seconds), DEFAULT_BOARD_DEADLINE_SECONDS)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.telemetry_recorder = telemetry_recorder or BoundedBoardTelemetryRecorder()
        if statistic_resolver is not None and not isinstance(
            statistic_resolver, StatisticResolver
        ):
            raise TypeError("statistic_resolver must be a StatisticResolver")
        if statistic_catalog is not None and not isinstance(
            statistic_catalog, StatisticCatalog
        ):
            raise TypeError("statistic_catalog must be a StatisticCatalog")
        if statistic_resolver is not None and statistic_catalog is not None and (
            statistic_resolver.catalog is not statistic_catalog
        ):
            raise ValueError("statistic_resolver and statistic_catalog must agree")
        catalog = statistic_catalog or (
            statistic_resolver.catalog if statistic_resolver is not None else StatisticCatalog.load_default()
        )
        self.statistic_catalog = catalog
        self.statistic_resolver = statistic_resolver or StatisticResolver(catalog)
        self.athlete_resolver = athlete_resolver
        self.athlete_mapping_repository = athlete_mapping_repository
        self.event_resolver = event_resolver
        self.event_mapping_repository = event_mapping_repository

    @staticmethod
    def _build_registry(
        values: Mapping[str, ProviderSnapshotProvider],
    ) -> dict[str, ProviderSnapshotProvider]:
        if isinstance(values, Mapping):
            registry: dict[str, ProviderSnapshotProvider] = {}
            for raw_name, provider in values.items():
                name = _provider_name(raw_name)
                if not callable(getattr(provider, "get_snapshot", None)):
                    raise TypeError(f"DFS provider {name} must implement get_snapshot")
                if name in registry:
                    raise ValueError(f"duplicate DFS provider {name}")
                registry[name] = provider
            return registry

        raise TypeError("DFS provider registry must be a mapping")

    def get_board(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext | None = None,
        *,
        providers: Iterable[str] | None = None,
    ) -> DFSBoard:
        """Retrieve enabled snapshots with one shared absolute deadline.

        Expected upstream failures become failed outcomes.  A provider that
        raises an implementation defect is deliberately not caught.  Running
        provider threads are daemonized and their late results are isolated
        from the returned board.

        ``providers`` narrows this read to a subset of the enabled registry.
        It is answered before any retrieval starts, so an excluded provider is
        not called at all and is reported as disabled for this board.
        """

        if not isinstance(query, NBAMarketQuery):
            raise TypeError("query must be NBAMarketQuery")
        generated_at = self._clock_utc()
        if context is None:
            context = RetrievalContext(
                deadline=generated_at + timedelta(seconds=self.deadline_seconds)
            )
        elif not isinstance(context, RetrievalContext):
            raise TypeError("context must be RetrievalContext")
        # Adapters receive a child context so a caller cannot accidentally
        # extend this board's absolute fifteen-second ceiling.
        context = RetrievalContext(
            deadline=min(
                context.deadline,
                generated_at + timedelta(seconds=self.deadline_seconds),
            ),
            request_id=context.request_id,
        )
        start = self.monotonic()
        remaining = context.remaining_seconds(now=generated_at)
        board_deadline = start + remaining
        if providers is None:
            names = list(self.enabled_providers)
            disabled = self.disabled_providers
        else:
            requested = set(_normalize_names(providers))
            names = [name for name in self.enabled_providers if name in requested]
            disabled = tuple(
                sorted(set(self.disabled_providers) | (set(self.enabled_providers) - set(names)))
            )
        outcomes: dict[str, ProviderOutcome] = {}
        pending = list(names)
        active: dict[str, tuple[threading.Thread, dict[str, Any]]] = {}

        while pending or active:
            while pending and len(active) < self.max_concurrency:
                if self._deadline_reached(context, board_deadline):
                    break
                name = pending.pop(0)
                holder: dict[str, Any] = {}
                thread = threading.Thread(
                    target=self._retrieve_one,
                    args=(name, query, context, board_deadline, holder),
                    daemon=True,
                    name=f"statsplus-dfs-{name}",
                )
                active[name] = (thread, holder)
                thread.start()

            completed = [
                name for name, (thread, _holder) in active.items() if not thread.is_alive()
            ]
            if completed:
                for name in completed:
                    thread, holder = active.pop(name)
                    thread.join(timeout=0)
                    self._harvest(name, holder, outcomes, board_deadline)
                continue

            if self._deadline_reached(context, board_deadline):
                break
            wait_for = min(0.01, max(0.0, board_deadline - self.monotonic()))
            if not wait_for:
                break
            # A short join gives completed workers priority while preserving
            # the hard caller-side deadline when a provider ignores it.
            next_thread = next(iter(active.values()))[0]
            next_thread.join(timeout=wait_for)

        # A worker may publish a result in the same instant that the caller
        # observes the deadline. Drain holders whose completion timestamp is
        # at or before the boundary before assigning deadline failures.
        for name in list(active):
            thread, holder = active[name]
            completed_at = holder.get("completed_at")
            if completed_at is not None and completed_at <= board_deadline:
                active.pop(name)
                thread.join(timeout=0)
                self._harvest(name, holder, outcomes, board_deadline)

        if pending or active or self._deadline_reached(context, board_deadline):
            for name in pending:
                outcomes[name] = ProviderOutcome(
                    provider=name,
                    status=ProviderOutcomeStatus.FAILED,
                    reason=ProviderFailureReason.DEADLINE_EXCEEDED,
                )
            for name in active:
                outcomes[name] = ProviderOutcome(
                    provider=name,
                    status=ProviderOutcomeStatus.FAILED,
                    reason=ProviderFailureReason.DEADLINE_EXCEEDED,
                )

        ordered = tuple(
            self._resolve_outcome(outcomes[name]) for name in names if name in outcomes
        )
        board = DFSBoard(
            query=query,
            provider_outcomes=ordered,
            disabled_providers=disabled,
            generated_at=generated_at,
        )
        board = self._resolve_athlete_mappings(board)
        board = self._resolve_event_mappings(board)
        self._record_telemetry(
            self.monotonic() - start,
            board,
            started_at=generated_at.isoformat(),
            request_id=context.request_id,
        )
        return board

    def _resolve_outcome(self, outcome: ProviderOutcome) -> ProviderOutcome:
        if not outcome.usable or outcome.snapshot is None:
            return outcome
        return replace(outcome, snapshot=self._resolve_snapshot(outcome.snapshot))

    def _resolve_snapshot(self, snapshot: ProviderSnapshot) -> ProviderSnapshot:
        resolved_markets: list[PlayerProjectionMarket] = []
        for market in snapshot.markets:
            if market.statistic is None:
                # A provider that omitted statistic evidence stays visible as
                # an explicit unmapped match on the snapshot itself.
                resolved_markets.append(
                    replace(
                        market,
                        statistic_match=StatisticMatch(
                            state=MatchState.UNMAPPED,
                            evidence=StatisticEvidence(),
                            provider=market.provider,
                            scoring_period=market.scoring_period,
                            reason=MatchReason.MISSING_STATISTIC_EVIDENCE,
                        ),
                    )
                )
                continue
            # The provider's own evidence is the observation of record, so the
            # canonical interpretation is carried only by the attached match.
            resolved_markets.append(
                replace(
                    market,
                    statistic_match=self.statistic_resolver.resolve_market(market),
                )
            )
        if tuple(resolved_markets) == snapshot.markets:
            return snapshot
        return replace(snapshot, markets=tuple(resolved_markets))

    def _resolve_athlete_mappings(self, board: DFSBoard) -> DFSBoard:
        """Observe typed market evidence without making board reads fragile.

        Mapping persistence is opt-in through the injected resolver and
        repository.  Both the mapping-state read and the write translate their
        storage failures to ``AthleteMappingPersistenceError`` at the
        repository/service boundary, so this seam isolates exactly that type
        and never catches broad exceptions.
        """

        if (
            self.athlete_resolver is None
            or self.athlete_mapping_repository is None
            or board.query.season is None
        ):
            return board
        resolved: list[tuple[tuple[str, str] | None, Any]] = []
        for snapshot in board.snapshots:
            for market in snapshot.markets:
                if market.athlete is None:
                    continue
                try:
                    resolution = self.athlete_resolver.resolve_market(
                        market,
                        board.query.season,
                        # The snapshot is temporally coherent, so its retrieval
                        # instant is when every market in it was observed.  A
                        # slow provider read must be ordered by when it saw the
                        # provider, not by when the board got around to it.
                        observed_at=snapshot.retrieved_at,
                    )
                except AthleteMappingPersistenceError:
                    # Board retrieval is a normalized read; mapping storage is
                    # an audit side effect and must never remove a market.
                    logger.warning("Could not observe one athlete mapping")
                    continue
                resolved.append((_identity_key(resolution), resolution))
        # Every market is resolved before anything is written, so what one
        # identity's markets assert is known before any of them is acted on.
        contradictory = _contradictory_identities(resolved)
        groups: list[tuple[Any, ...]] = []
        for key, group in _identity_groups(resolved):
            if key in contradictory:
                # The contradiction is one observation of the identity, so the
                # markets it disagreed across are written once, together.
                group = (_fail_closed_conflict(group),)
            elif key is not None and len(group) > 1:
                # The markets agree about the identity, so together they are
                # one observation of it and are written once, resolved from
                # everything they reported about it.
                try:
                    group = (
                        _merged_observation(group, self.athlete_resolver.resolve),
                    )
                except AthleteMappingPersistenceError:
                    logger.warning("Could not observe one athlete mapping")
                    continue
            groups.append(group)
        observed: list[Any] = []
        for group in groups:
            governed = None
            for resolution in group:
                try:
                    result = self.athlete_mapping_repository.record_resolution(resolution)
                except AthleteMappingPersistenceError:
                    logger.warning("Could not observe one athlete mapping")
                    continue
                # The resolver read before the write's transaction opened, so
                # what it computed is only what this observation proposed.  The
                # write returns the state governance reached for the identity,
                # and the board reports that -- converted from the value in
                # hand, never re-read, so the report cannot race the write.
                governed = result.board_outcome(resolution)
            # The identity's last write is what is durable when the board is
            # returned, so it stands for every market that named the identity.
            if governed is not None:
                observed.append(governed)
        return DFSBoard(
            query=board.query,
            provider_outcomes=board.provider_outcomes,
            disabled_providers=board.disabled_providers,
            generated_at=board.generated_at,
            contract_version=board.contract_version,
            mapping_outcomes=tuple(observed),
            event_mapping_outcomes=board.event_mapping_outcomes,
        )

    def _resolve_event_mappings(self, board: DFSBoard) -> DFSBoard:
        """Observe typed event evidence without making board reads fragile.

        Event mapping persistence is opt-in through the injected resolver and
        repository.  Both the mapping-state read and the write translate their
        storage failures to ``EventMappingPersistenceError`` at the
        repository/service boundary, so this seam isolates exactly that type and
        never catches broad exceptions.  A market whose event cannot be placed --
        including every market of a season whose Event Catalog is missing or
        over-age -- stays on the board as a normalized market with no comparison
        identity.
        """

        if (
            self.event_resolver is None
            or self.event_mapping_repository is None
            or board.query.season is None
        ):
            return board
        resolved: list[tuple[tuple[str, str] | None, Any]] = []
        for snapshot in board.snapshots:
            for market in snapshot.markets:
                if market.event is None:
                    continue
                try:
                    resolution = self.event_resolver.resolve_market(
                        market,
                        board.query.season,
                        # The snapshot is temporally coherent, so its retrieval
                        # instant is when every market in it was observed.
                        observed_at=snapshot.retrieved_at,
                    )
                except EventMappingPersistenceError:
                    logger.warning("Could not observe one event mapping")
                    continue
                resolved.append((_event_identity_key(resolution), resolution))
        # Every market is resolved before anything is written, so what one
        # fixture's markets assert is known before any of them is acted on.
        contradictory = _contradictory_identities(
            resolved,
            facts_of=_event_evidence_facts,
            contradicts=_event_contradicts,
        )
        groups: list[tuple[Any, ...]] = []
        for key, group in _identity_groups(resolved):
            if _event_catalog_unusable(group):
                group = (_withheld_event_observation(group),)
            elif key in contradictory:
                # The contradiction is one observation of the fixture, so the
                # markets it disagreed across are written once, together.
                group = (_fail_closed_event_conflict(group),)
            elif key is not None and len(group) > 1:
                try:
                    group = (
                        _merged_event_observation(group, self.event_resolver.resolve),
                    )
                except EventMappingPersistenceError:
                    logger.warning("Could not observe one event mapping")
                    continue
            groups.append(group)
        observed: list[Any] = []
        for group in groups:
            governed = None
            for resolution in group:
                try:
                    result = self.event_mapping_repository.record_resolution(resolution)
                except EventMappingPersistenceError:
                    logger.warning("Could not observe one event mapping")
                    continue
                # The resolver read before the write's transaction opened, so
                # what it computed is only what this observation proposed.  The
                # write returns the state governance reached, converted from the
                # value in hand rather than re-read, so the report cannot race
                # the write.
                governed = result.board_outcome(resolution)
            if governed is not None:
                observed.append(governed)
        return DFSBoard(
            query=board.query,
            provider_outcomes=board.provider_outcomes,
            disabled_providers=board.disabled_providers,
            generated_at=board.generated_at,
            contract_version=board.contract_version,
            mapping_outcomes=board.mapping_outcomes,
            event_mapping_outcomes=tuple(observed),
        )

    def _record_telemetry(
        self,
        duration: float,
        board: DFSBoard,
        *,
        started_at: str,
        request_id: str | None,
    ) -> None:
        outcome_counts = Counter(outcome.status.value for outcome in board.provider_outcomes)
        reason_counts = Counter(
            outcome.reason.value
            for outcome in board.provider_outcomes
            if outcome.reason is not None
        )
        fetched = eligible = normalized = skipped = 0
        for outcome in board.provider_outcomes:
            if outcome.coverage is not None:
                fetched += outcome.coverage.fetched_count
                eligible += outcome.coverage.eligible_count
                normalized += outcome.coverage.normalized_count
                skipped += outcome.coverage.skipped_count
        self.telemetry_recorder.record(
            BoardTelemetryEvent(
                duration_ms=max(0.0, duration * 1000.0),
                outcome_counts=tuple(sorted(outcome_counts.items())),
                failure_reason_counts=tuple(sorted(reason_counts.items())),
                fetched_count=fetched,
                eligible_count=eligible,
                normalized_count=normalized,
                skipped_count=skipped,
                started_at=started_at,
                request_id=request_id,
            )
        )

    def _retrieve_one(
        self,
        name: str,
        query: NBAMarketQuery,
        context: RetrievalContext,
        board_deadline: float,
        holder: dict[str, Any],
    ) -> None:
        """Run one provider in an isolated daemon worker."""

        try:
            if self.monotonic() >= board_deadline:
                holder["outcome"] = ProviderOutcome(
                    provider=name,
                    status=ProviderOutcomeStatus.FAILED,
                    reason=ProviderFailureReason.DEADLINE_EXCEEDED,
                )
                return
            context.ensure_active(now=self._clock_utc())
            provider = self.provider_registry[name]
            snapshot = provider.get_snapshot(query, context)
            if self.monotonic() > board_deadline:
                holder["outcome"] = ProviderOutcome(
                    provider=name,
                    status=ProviderOutcomeStatus.FAILED,
                    reason=ProviderFailureReason.DEADLINE_EXCEEDED,
                )
                return
            cache_result = None
            get_last_result = getattr(provider, "get_last_result", None)
            if callable(get_last_result):
                cache_result = get_last_result()
            if cache_result is None:
                # Keep the narrow existing provider seam compatible with
                # injected test doubles that override this class method.
                holder["outcome"] = self._outcome_from_snapshot(name, snapshot)
            else:
                holder["outcome"] = self._outcome_from_snapshot(
                    name,
                    snapshot,
                    cache_result=cache_result,
                )
        except (
            DeadlineExceededError,
            ProviderUnavailableError,
            ProviderResponseError,
            MalformedProviderResponseError,
            TimeoutError,
            requests.exceptions.RequestException,
        ) as error:
            if self.monotonic() >= board_deadline or context.is_expired(
                now=self._clock_utc()
            ):
                reason = ProviderFailureReason.DEADLINE_EXCEEDED
            else:
                reason = self._failure_reason(error)
            holder["outcome"] = ProviderOutcome(
                provider=name,
                status=ProviderOutcomeStatus.FAILED,
                reason=reason,
            )
        except BaseException as error:
            # The main thread re-raises defects from workers as soon as their
            # worker completes.  A late defect is isolated just like a late
            # provider result because the board deadline has already passed.
            holder["exception"] = error
        finally:
            holder["completed_at"] = self.monotonic()

    @staticmethod
    def _harvest(
        name: str,
        holder: Mapping[str, Any],
        outcomes: dict[str, ProviderOutcome],
        board_deadline: float,
    ) -> None:
        completed_at = holder.get("completed_at")
        if not isinstance(completed_at, (int, float)) or completed_at > board_deadline:
            outcomes[name] = ProviderOutcome(
                provider=name,
                status=ProviderOutcomeStatus.FAILED,
                reason=ProviderFailureReason.DEADLINE_EXCEEDED,
            )
            return
        error = holder.get("exception")
        if error is not None:
            raise error
        outcome = holder.get("outcome")
        if not isinstance(outcome, ProviderOutcome):
            raise RuntimeError(f"DFS provider {name} did not produce an outcome")
        outcomes[name] = outcome
        logger.info(
            "dfs_board_provider provider=%s status=%s reason=%s fetched=%s "
            "eligible=%s normalized=%s skipped=%s",
            outcome.provider,
            outcome.status.value,
            outcome.reason.value if outcome.reason is not None else "none",
            outcome.coverage.fetched_count if outcome.coverage is not None else 0,
            outcome.coverage.eligible_count if outcome.coverage is not None else 0,
            outcome.coverage.normalized_count if outcome.coverage is not None else 0,
            outcome.coverage.skipped_count if outcome.coverage is not None else 0,
        )

    @classmethod
    def _outcome_from_snapshot(
        cls,
        name: str,
        snapshot: ProviderSnapshot,
        *,
        cache_result: Any | None = None,
    ) -> ProviderOutcome:
        if not isinstance(snapshot, ProviderSnapshot):
            raise TypeError("DFS provider get_snapshot must return ProviderSnapshot")
        if snapshot.provider != name:
            raise ValueError("DFS provider snapshot provider does not match registry name")
        if snapshot.status is SnapshotStatus.COMPLETE:
            return ProviderOutcome(
                provider=name,
                status=ProviderOutcomeStatus.COMPLETE,
                snapshot=snapshot,
                cache_status=getattr(cache_result, "cache_status", None),
                cache_retrieved_at=getattr(cache_result, "retrieved_at", None),
                cache_age_seconds=getattr(cache_result, "age_seconds", None),
                cache_failure_reason=getattr(
                    cache_result, "refresh_failure_reason", None
                ),
                cache_failure_at=getattr(cache_result, "refresh_failed_at", None),
            )
        return ProviderOutcome(
            provider=name,
            status=ProviderOutcomeStatus.PARTIAL,
            snapshot=snapshot,
            reason=cls._partial_reason(snapshot),
            cache_status=getattr(cache_result, "cache_status", None),
            cache_retrieved_at=getattr(cache_result, "retrieved_at", None),
            cache_age_seconds=getattr(cache_result, "age_seconds", None),
            cache_failure_reason=getattr(cache_result, "refresh_failure_reason", None),
            cache_failure_at=getattr(cache_result, "refresh_failed_at", None),
        )

    @staticmethod
    def _partial_reason(snapshot: ProviderSnapshot) -> ProviderFailureReason | None:
        codes = set(snapshot.coverage.warning_codes) | set(snapshot.coverage.skipped_reasons)
        if codes & _MALFORMED_COVERAGE_CODES:
            return ProviderFailureReason.MALFORMED_RESPONSE
        if codes & _UPSTREAM_COVERAGE_CODES:
            return ProviderFailureReason.UPSTREAM_ERROR
        return ProviderFailureReason.UPSTREAM_ERROR if not snapshot.coverage.is_complete else None

    @staticmethod
    def _failure_reason(error: BaseException) -> ProviderFailureReason:
        """Classify expected failures without retaining their diagnostic text."""

        chain: list[BaseException] = []
        current: BaseException | None = error
        while current is not None and len(chain) < 12:
            chain.append(current)
            current = current.__cause__ or current.__context__

        for candidate in chain:
            if isinstance(candidate, DeadlineExceededError):
                return ProviderFailureReason.DEADLINE_EXCEEDED
            if isinstance(candidate, (ProviderResponseError, MalformedProviderResponseError)):
                return ProviderFailureReason.MALFORMED_RESPONSE

        for candidate in chain:
            if isinstance(candidate, requests.exceptions.HTTPError):
                status = getattr(getattr(candidate, "response", None), "status_code", None)
                if status == 429:
                    return ProviderFailureReason.RATE_LIMITED
                if status in {401, 403}:
                    return ProviderFailureReason.ACCESS_DENIED
                return ProviderFailureReason.UPSTREAM_ERROR
            if isinstance(candidate, requests.exceptions.Timeout):
                return ProviderFailureReason.TIMEOUT
            if isinstance(candidate, TimeoutError):
                return ProviderFailureReason.TIMEOUT

        typed_reasons = {
            "deadline_exceeded": ProviderFailureReason.DEADLINE_EXCEEDED,
            "timeout": ProviderFailureReason.TIMEOUT,
            "rate_limited": ProviderFailureReason.RATE_LIMITED,
            "access_denied": ProviderFailureReason.ACCESS_DENIED,
            "malformed": ProviderFailureReason.MALFORMED_RESPONSE,
            "malformed_response": ProviderFailureReason.MALFORMED_RESPONSE,
            "upstream_error": ProviderFailureReason.UPSTREAM_ERROR,
        }
        for candidate in chain:
            typed_reason = typed_reasons.get(getattr(candidate, "provider_reason", None))
            if typed_reason is not None:
                return typed_reason
        return ProviderFailureReason.UPSTREAM_ERROR

    def _clock_utc(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("DFS board clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    def _deadline_reached(
        self,
        context: RetrievalContext,
        board_deadline: float,
    ) -> bool:
        """Observe both monotonic and injected wall clocks without extending work."""

        return self.monotonic() >= board_deadline or context.is_expired(
            now=self._clock_utc()
        )


__all__ = [
    "DEFAULT_BOARD_DEADLINE_SECONDS",
    "DFSBoard",
    "DFSBoardService",
    "ProviderFailureReason",
    "ProviderOutcome",
    "ProviderOutcomeStatus",
]
