"""Conservative resolution of provider event evidence to canonical NBA games.

Provider event evidence is typed and incomplete: a market may name its event by
a provider ID, a matchup label, a start time, and home/away team facts, in any
combination.  Resolution therefore asks one narrow question -- which single
scheduled NBA game does this evidence identify? -- and answers it only from
canonical home/away teams plus schedule proximity.  Labels are retained as
evidence and never parsed into teams, and no provider identity is ever
fabricated.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.providers.dfs import (
    EventEvidence,
    PlayerProjectionMarket,
    TeamEvidence,
    normalize_timestamp,
)
from app.providers.nba_stats import validate_canonical_season
from app.services.event_mapping_errors import (
    DEFAULT_EVENT_MAPPING_FAILURE_SUMMARY,
    EventMappingPersistenceError,
)

#: Reviewed default distance between a provider's reported start time and a
#: scheduled NBA game.  Operators change it with
#: ``EVENT_MAPPING_MATCH_WINDOW_HOURS``; the boundary itself is inside it.
DEFAULT_EVENT_MATCH_WINDOW = timedelta(hours=6)


def event_match_window(
    match_window: timedelta | float | None = None,
    settings: Any | None = None,
) -> timedelta:
    """Resolve the configured distance one seam compares start times with.

    Every seam that judges whether provider evidence still describes the game
    it was mapped to has to use the same window, so the policy is read once
    here -- from an explicit value, from settings, or from the reviewed default
    -- rather than being re-derived wherever a comparison happens.
    """

    if match_window is None and settings is not None:
        configured = getattr(
            getattr(settings, "catalog", None), "event_match_window_hours", None
        )
        match_window = None if configured is None else float(configured)
    if match_window is None:
        return DEFAULT_EVENT_MATCH_WINDOW
    window = (
        match_window
        if isinstance(match_window, timedelta)
        else timedelta(hours=float(match_window))
    )
    if window.total_seconds() <= 0:
        raise ValueError("the event match window must be greater than zero")
    return window


class EventResolutionState(str, Enum):
    """Closed outcomes of one provider-event resolution attempt."""

    AUTO = "auto"
    MANUAL_APPROVED = "manual_approved"
    MANUAL_OVERRIDE = "manual_override"
    REJECTED = "rejected"
    MAPPING_CONFLICT = "mapping_conflict"
    AMBIGUOUS = "ambiguous"
    UNMATCHED = "unmatched"
    #: The canonical game a claim names is no longer scheduled.  A replacement
    #: NBA game ID never inherits the mapping automatically, however clearly
    #: the new evidence points at one, so the identity waits for an operator.
    REPLACEMENT_PENDING = "replacement_pending"
    #: The Event Catalog is missing or older than allowed for the season, so
    #: there is no comparison identity to establish.  Normalized markets stay
    #: visible; nothing is recorded.
    EVENT_CATALOG_UNAVAILABLE = "event_catalog_unavailable"
    REJECTION_CLEARED = "rejection_cleared"


#: Mapping states that withdrew an automatic claim from board comparisons
#: without retracting it.  The identity still belongs to the canonical game the
#: row names.
WITHDRAWN_EVENT_CLAIM_STATES = frozenset(
    {
        EventResolutionState.AMBIGUOUS.value,
        EventResolutionState.UNMATCHED.value,
        EventResolutionState.REPLACEMENT_PENDING.value,
    }
)


#: Resolution states that still assert a canonical claim for the identity.
#: Only these may hand a stored mapping to a caller that compares events; every
#: other governed state means the identity is suppressed, disputed, or
#: withdrawn and must fail closed with no canonical game at all.
CLAIMING_EVENT_MAPPING_STATES = frozenset(
    {
        EventResolutionState.AUTO,
        EventResolutionState.MANUAL_APPROVED,
        EventResolutionState.MANUAL_OVERRIDE,
    }
)


def normalize_team_label(value: str | None) -> str:
    """Fold a team label to its comparable form.

    Team tricodes and names are presentation, so case, spacing, and punctuation
    are ignored while the letters themselves are compared exactly.  Nothing
    here is an alias, a nickname, or a fuzzy rule.
    """

    if not isinstance(value, str):
        return ""
    return "".join(char for char in value.casefold() if char.isalnum())


def _provider_name(value: str | None) -> str:
    name = value.strip().casefold() if isinstance(value, str) else ""
    if not name:
        raise ValueError("mapping provider must be a non-empty string")
    return name


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def stored_timestamp(value: datetime | str | None) -> datetime | None:
    """Read one persisted instant as UTC.

    Provider evidence is normalized when it is constructed, but a stored row is
    read back as the database returned it: SQLite has no time zone, and a
    serialized record carries an ISO string.  Both are UTC instants that were
    written as such, so they are read as such rather than rejected.
    """

    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("a stored timestamp must be a datetime or ISO 8601 string")
    return _utc(value)


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    """Typed view of one canonical NBA game from the Event Catalog."""

    nba_game_id: str
    season: str
    scheduled_at: datetime | None
    home_team_id: int | None = None
    home_team_name: str | None = None
    home_team_tricode: str | None = None
    away_team_id: int | None = None
    away_team_name: str | None = None
    away_team_tricode: str | None = None
    status_text: str | None = None
    classification: str | None = None
    is_postponed: bool = False
    #: Signed distance from the provider's reported start time, in seconds, for
    #: a candidate the window admitted.  ``None`` outside a comparison.
    start_offset_seconds: int | None = None
    #: Whether the requested season's catalog still lists this game.  A claim
    #: whose canonical game was replaced keeps its facts while saying so.
    is_scheduled: bool = True

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CanonicalEvent":
        scheduled_at = stored_timestamp(row.get("scheduled_at"))
        return cls(
            nba_game_id=str(row["nba_game_id"]),
            season=str(row.get("season") or ""),
            scheduled_at=scheduled_at,
            home_team_id=(
                None if row.get("home_team_id") is None else int(row["home_team_id"])
            ),
            home_team_name=(
                None if row.get("home_team_name") is None else str(row["home_team_name"])
            ),
            home_team_tricode=(
                None
                if row.get("home_team_tricode") is None
                else str(row["home_team_tricode"]).strip().upper()
            ),
            away_team_id=(
                None if row.get("away_team_id") is None else int(row["away_team_id"])
            ),
            away_team_name=(
                None if row.get("away_team_name") is None else str(row["away_team_name"])
            ),
            away_team_tricode=(
                None
                if row.get("away_team_tricode") is None
                else str(row["away_team_tricode"]).strip().upper()
            ),
            status_text=(
                None if row.get("status_text") is None else str(row["status_text"])
            ),
            classification=(
                None if row.get("classification") is None else str(row["classification"])
            ),
            is_postponed=bool(row.get("is_postponed") or row.get("postponed_status")),
        )


@dataclass(frozen=True, slots=True)
class EventResolution:
    """Typed result retained by the board and event mapping repository seams."""

    provider: str
    provider_evidence: EventEvidence
    season: str
    state: EventResolutionState
    canonical_event: CanonicalEvent | None = None
    candidates: tuple[CanonicalEvent, ...] = ()
    reason: str | None = None
    #: Every distinct provider evidence one observation of this identity
    #: carried, when the observation contradicts itself.  A single board read
    #: can report the same provider event ID with two different matchups, and
    #: the contradiction is the whole set rather than any one of them.  Empty
    #: for every observation that asserts one event.
    contradictory_evidence: tuple[EventEvidence, ...] = ()
    #: When the provider observation behind this result was retrieved, in UTC.
    #: It is the provider's own instant, never a persistence time, so a slow
    #: read that lands after a later decision is still recognized as older.
    observed_at: datetime | str | None = None

    def __post_init__(self) -> None:
        provider = _provider_name(self.provider)
        if not isinstance(self.provider_evidence, EventEvidence):
            raise ValueError("provider_evidence must be EventEvidence")
        season = validate_canonical_season(self.season)
        state = (
            self.state
            if isinstance(self.state, EventResolutionState)
            else EventResolutionState(self.state)
        )
        candidates = tuple(self.candidates)
        if any(not isinstance(candidate, CanonicalEvent) for candidate in candidates):
            raise ValueError("event candidates must be CanonicalEvent values")
        contradictory = tuple(self.contradictory_evidence)
        if any(not isinstance(item, EventEvidence) for item in contradictory):
            raise ValueError("contradictory evidence must be EventEvidence values")
        if state is EventResolutionState.AUTO and self.canonical_event is None:
            raise ValueError("auto resolution requires a canonical event")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "season", season)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "contradictory_evidence", contradictory)
        object.__setattr__(self, "observed_at", normalize_timestamp(self.observed_at))

    @property
    def provider_event_id(self) -> str | None:
        return self.provider_evidence.provider_id

    @property
    def canonical_event_id(self) -> str | None:
        return self.canonical_event.nba_game_id if self.canonical_event else None

    @property
    def is_durable(self) -> bool:
        """Whether anything about this observation may be stored.

        A market with teams and a start time but no stable provider event ID
        can be compared on the current board, and is reevaluated on the next
        read; it never receives a fabricated durable identity, so there is
        nothing to key a mapping, a decision, or a rejection on.
        """

        return bool(self.provider_event_id)

    @property
    def is_auto_qualifying(self) -> bool:
        return (
            self.state is EventResolutionState.AUTO and self.canonical_event is not None
        )

    @property
    def asserted_evidence(self) -> tuple[EventEvidence, ...]:
        """Every provider evidence this observation asserts about the identity.

        One evidence for an ordinary observation, and the whole contradictory
        set for a fail-closed one, so a governance check reads what the board
        actually observed rather than the representative it recorded.
        """

        return self.contradictory_evidence or (self.provider_evidence,)


class EventResolver:
    """Resolve provider event evidence against one requested catalog season."""

    def __init__(
        self,
        event_catalog: Any,
        *,
        mapping_repository: Any | None = None,
        match_window: timedelta | float | None = None,
        settings: Any | None = None,
    ) -> None:
        if not callable(getattr(event_catalog, "get_events", None)):
            raise TypeError("event catalog must expose get_events")
        if not callable(getattr(event_catalog, "get_freshness", None)):
            raise TypeError("event catalog must expose get_freshness")
        self.catalog = event_catalog
        self.mapping_repository = mapping_repository
        self.match_window = event_match_window(match_window, settings)

    # -- resolution --------------------------------------------------------

    def resolve(
        self,
        provider: str,
        evidence: EventEvidence,
        season: str,
        *,
        observed_at: datetime | str | None = None,
    ) -> EventResolution:
        """Resolve one provider's typed event evidence for one season.

        ``observed_at`` is when the provider was read.  Resolution is a pure
        function of the evidence, the catalog, and the governed mapping state,
        so the instant is only carried onto the result for the persistence seam
        to order it against decisions taken while the read was in flight.
        """

        resolution = self._resolve(provider, evidence, season)
        if observed_at is None:
            return resolution
        return replace(resolution, observed_at=observed_at)

    def resolve_market(
        self,
        market: PlayerProjectionMarket,
        season: str,
        *,
        observed_at: datetime | str | None = None,
    ) -> EventResolution:
        """Resolve the event evidence carried by one normalized market."""

        if not isinstance(market, PlayerProjectionMarket):
            raise TypeError("market must be PlayerProjectionMarket")
        if market.event is None:
            raise ValueError("provider market has no event evidence")
        return self.resolve(
            market.provider, market.event, season, observed_at=observed_at
        )

    def _resolve(
        self,
        provider: str,
        evidence: EventEvidence,
        season: str,
    ) -> EventResolution:
        if not isinstance(evidence, EventEvidence):
            raise TypeError("evidence must be EventEvidence")
        canonical_season = validate_canonical_season(season)
        normalized_provider = _provider_name(provider)
        provider_event_id = evidence.provider_id

        existing = (
            None
            if not provider_event_id
            else self._get_mapping(normalized_provider, provider_event_id)
        )
        if existing is not None:
            governed = self._governed_result(
                normalized_provider, evidence, canonical_season, existing
            )
            if governed is not None:
                # Freshness gates a governed identity exactly as it gates a
                # match.  Without usable catalog data there is no schedule to
                # place the operator's decision against, so the identity is
                # neither handed to the board nor recorded against; the
                # decision itself is untouched and returns with the next
                # refresh.
                unavailable = self._catalog_unavailable_reason(canonical_season)
                if unavailable is None:
                    return governed
                return self._unavailable_result(
                    normalized_provider, evidence, canonical_season, unavailable
                )
        if provider_event_id:
            rejection = self._get_rejection(normalized_provider, provider_event_id)
            if rejection is not None and bool(rejection.get("is_active", True)):
                return self._result(
                    normalized_provider,
                    evidence,
                    canonical_season,
                    EventResolutionState.REJECTED,
                    reason="rejected",
                )

        unavailable = self._catalog_unavailable_reason(canonical_season)
        if unavailable is not None:
            return self._unavailable_result(
                normalized_provider, evidence, canonical_season, unavailable
            )
        events = self._catalog_events(canonical_season)
        return self._match(
            normalized_provider, evidence, canonical_season, events, existing
        )

    def _governed_result(
        self,
        provider: str,
        evidence: EventEvidence,
        season: str,
        existing: Mapping[str, Any],
    ) -> EventResolution | None:
        """Return the state a governed mapping row already decides, if any."""

        try:
            state = EventResolutionState(str(existing.get("mapping_state", "")))
        except ValueError:
            return None
        if state is EventResolutionState.MAPPING_CONFLICT:
            return self._result(
                provider,
                evidence,
                season,
                EventResolutionState.MAPPING_CONFLICT,
                reason="mapping_conflict",
            )
        manual = state in {
            EventResolutionState.MANUAL_APPROVED,
            EventResolutionState.MANUAL_OVERRIDE,
        }
        if not manual or not bool(existing.get("is_active", True)):
            return None
        if self._manual_evidence_conflicts(existing, evidence, self.match_window):
            # Operator precedence protects a governed mapping from automatic
            # overwrite; it does not assert the identity for evidence the
            # operator never reviewed.
            return self._result(
                provider,
                evidence,
                season,
                EventResolutionState.MAPPING_CONFLICT,
                reason="manual_mapping_conflict",
            )
        return self._result(
            provider,
            evidence,
            season,
            state,
            canonical=self._canonical_from_mapping(existing, season),
            reason=state.value,
        )

    def _match(
        self,
        provider: str,
        evidence: EventEvidence,
        season: str,
        events: tuple[CanonicalEvent, ...],
        existing: Mapping[str, Any] | None,
    ) -> EventResolution:
        """Resolve the evidence against the season's scheduled games."""

        claimed_id = self._claimed_event_id(existing)
        home_id = self._canonical_team_id(evidence.home_team, events)
        away_id = self._canonical_team_id(evidence.away_team, events)
        if home_id is None or away_id is None:
            return self._withdrawing_result(
                provider,
                evidence,
                season,
                events,
                existing,
                state=EventResolutionState.UNMATCHED,
                reason="missing_team_evidence",
            )
        if evidence.starts_at is None:
            return self._withdrawing_result(
                provider,
                evidence,
                season,
                events,
                existing,
                state=EventResolutionState.UNMATCHED,
                reason="missing_start_time",
            )
        if home_id == away_id:
            return self._withdrawing_result(
                provider,
                evidence,
                season,
                events,
                existing,
                state=EventResolutionState.UNMATCHED,
                reason="single_team_matchup",
            )

        starts_at = _utc(evidence.starts_at)
        nearby = self._nearby(events, starts_at, home_id, away_id)
        if claimed_id is not None and not self._is_scheduled(events, claimed_id):
            # The canonical game this identity was mapped to is gone from the
            # season's schedule.  A replacement NBA game ID never inherits the
            # mapping, however unambiguous the new evidence is, so the identity
            # stays unresolved until an operator approves it.
            reason = (
                "replacement_event_identity"
                if len(nearby) == 1
                else "ambiguous_replacement_event"
                if nearby
                else "replaced_event_absent"
            )
            return self._result(
                provider,
                evidence,
                season,
                EventResolutionState.REPLACEMENT_PENDING,
                candidates=nearby + self._absent_claim_candidates(existing, season),
                reason=reason,
            )
        if not nearby:
            reversed_matchup = self._nearby(events, starts_at, away_id, home_id)
            return self._withdrawing_result(
                provider,
                evidence,
                season,
                events,
                existing,
                state=EventResolutionState.UNMATCHED,
                reason=(
                    "home_away_mismatch" if reversed_matchup else "no_nearby_event"
                ),
                candidates=reversed_matchup,
            )
        if len(nearby) > 1:
            return self._withdrawing_result(
                provider,
                evidence,
                season,
                events,
                existing,
                state=EventResolutionState.AMBIGUOUS,
                reason="ambiguous_nearby_events",
                candidates=nearby,
            )

        candidate = nearby[0]
        if claimed_id is not None and claimed_id != candidate.nba_game_id:
            # A claimed identity that now resolves to a different scheduled
            # game is exactly the silent transfer this seam must refuse.
            return self._result(
                provider,
                evidence,
                season,
                EventResolutionState.MAPPING_CONFLICT,
                canonical=candidate,
                candidates=nearby,
                reason="mapping_conflict",
            )
        return self._result(
            provider,
            evidence,
            season,
            EventResolutionState.AUTO,
            canonical=candidate,
            candidates=nearby,
            reason="canonical_matchup_within_window",
        )

    def _withdrawing_result(
        self,
        provider: str,
        evidence: EventEvidence,
        season: str,
        events: tuple[CanonicalEvent, ...],
        existing: Mapping[str, Any] | None,
        *,
        state: EventResolutionState,
        reason: str,
        candidates: tuple[CanonicalEvent, ...] = (),
    ) -> EventResolution:
        """One non-matching outcome, naming the claim it withdraws if any.

        An identity that was mapped and can no longer be placed keeps its claim
        as a candidate, so an operator's queue distinguishes a withdrawn claim
        from an identity that simply matched nothing.
        """

        claimed_id = self._claimed_event_id(existing)
        if claimed_id is not None:
            claimed = self._catalog_event(events, claimed_id)
            candidates = candidates + (
                (claimed,)
                if claimed is not None
                else self._absent_claim_candidates(existing, season)
            )
        return self._result(
            provider, evidence, season, state, candidates=candidates, reason=reason
        )

    def _nearby(
        self,
        events: tuple[CanonicalEvent, ...],
        starts_at: datetime,
        home_id: int,
        away_id: int,
    ) -> tuple[CanonicalEvent, ...]:
        """Scheduled games with this exact matchup inside the match window.

        Home and away are compared in the orientation the provider reported: a
        reversed matchup is different evidence, not the same game described
        another way.  The boundary is inside the window, so a game exactly the
        configured distance away still matches.
        """

        matches: list[CanonicalEvent] = []
        for event in events:
            if event.home_team_id != home_id or event.away_team_id != away_id:
                continue
            if event.scheduled_at is None:
                continue
            offset = _utc(event.scheduled_at) - starts_at
            if abs(offset) > self.match_window:
                continue
            matches.append(
                replace(event, start_offset_seconds=int(offset.total_seconds()))
            )
        matches.sort(
            key=lambda event: (abs(event.start_offset_seconds or 0), event.nba_game_id)
        )
        return tuple(matches)

    @staticmethod
    def _canonical_team_id(
        team: TeamEvidence | None,
        events: Sequence[CanonicalEvent],
    ) -> int | None:
        """Resolve one provider team evidence to a canonical NBA team ID.

        The canonical ID is the strongest evidence.  Otherwise the tricode and
        then the team name are compared with the teams the season's schedule
        actually names, and only an unambiguous match counts.  Nothing is
        inferred from a matchup label, so a provider that reports no team
        evidence resolves no team rather than a guessed one.
        """

        if team is None:
            return None
        if team.canonical_id is not None:
            return int(team.canonical_id)
        for label, fields in (
            (team.abbreviation, ("home_team_tricode", "away_team_tricode")),
            (team.name, ("home_team_name", "away_team_name")),
        ):
            normalized = normalize_team_label(label)
            if not normalized:
                continue
            found: set[int] = set()
            for event in events:
                for field, team_id_field in zip(
                    fields, ("home_team_id", "away_team_id")
                ):
                    if normalize_team_label(getattr(event, field)) == normalized:
                        team_id = getattr(event, team_id_field)
                        if team_id is not None:
                            found.add(int(team_id))
            if len(found) == 1:
                return found.pop()
            if found:
                # Two canonical teams answer to one label, so the label is not
                # identifying evidence and nothing may be assumed from it.
                return None
        return None

    @staticmethod
    def _is_scheduled(events: Sequence[CanonicalEvent], game_id: str) -> bool:
        return any(event.nba_game_id == game_id for event in events)

    @staticmethod
    def _catalog_event(
        events: Sequence[CanonicalEvent],
        game_id: str,
    ) -> CanonicalEvent | None:
        for event in events:
            if event.nba_game_id == game_id:
                return event
        return None

    @classmethod
    def _claimed_event_id(cls, existing: Mapping[str, Any] | None) -> str | None:
        """Return the canonical game an established claim still names.

        An active automatic mapping asserts one.  So does one withdrawn from
        comparisons because the evidence became ambiguous, matched nothing, or
        named a replaced game: every withdrawal suspends the claim rather than
        retracting it, so a later observation naming a *different* game under
        the same provider ID is still a conflict rather than a silent remap.
        """

        if not existing:
            return None
        state = str(existing.get("mapping_state", ""))
        claimed = state in WITHDRAWN_EVENT_CLAIM_STATES or (
            state == EventResolutionState.AUTO.value
            and bool(existing.get("is_active", False))
        )
        if not claimed:
            return None
        game_id = existing.get("canonical_event_id")
        return None if game_id is None else str(game_id)

    @staticmethod
    def _absent_claim_candidates(
        existing: Mapping[str, Any] | None,
        season: str,
    ) -> tuple[CanonicalEvent, ...]:
        """Name the claimed game the requested season no longer schedules.

        The facts come from the mapping row rather than the catalog, because
        the catalog is exactly what stopped carrying them.  The candidate is
        recorded as no longer scheduled: it is not a game an operator can
        approve as it stands, it is the claim this observation withdrew.
        """

        existing = existing or {}
        game_id = existing.get("canonical_event_id")
        if game_id is None:
            return ()
        return (
            CanonicalEvent(
                nba_game_id=str(game_id),
                season=str(existing.get("season") or season),
                scheduled_at=stored_timestamp(existing.get("canonical_scheduled_at")),
                home_team_id=existing.get("canonical_home_team_id"),
                home_team_name=existing.get("canonical_home_team_name"),
                home_team_tricode=existing.get("canonical_home_team_tricode"),
                away_team_id=existing.get("canonical_away_team_id"),
                away_team_name=existing.get("canonical_away_team_name"),
                away_team_tricode=existing.get("canonical_away_team_tricode"),
                is_scheduled=False,
            ),
        )

    @staticmethod
    def _canonical_from_mapping(
        mapping: Mapping[str, Any],
        season: str,
    ) -> CanonicalEvent | None:
        game_id = mapping.get("canonical_event_id")
        if game_id is None:
            return None
        return CanonicalEvent(
            nba_game_id=str(game_id),
            season=str(mapping.get("season") or season),
            scheduled_at=stored_timestamp(mapping.get("canonical_scheduled_at")),
            home_team_id=mapping.get("canonical_home_team_id"),
            home_team_name=mapping.get("canonical_home_team_name"),
            home_team_tricode=mapping.get("canonical_home_team_tricode"),
            away_team_id=mapping.get("canonical_away_team_id"),
            away_team_name=mapping.get("canonical_away_team_name"),
            away_team_tricode=mapping.get("canonical_away_team_tricode"),
        )

    @staticmethod
    def _manual_evidence_conflicts(
        existing: Mapping[str, Any],
        evidence: EventEvidence,
        window: timedelta,
    ) -> bool:
        """Report whether the provider identity changed under a manual mapping.

        An operator approves *this provider identity* as *this NBA game* on the
        evidence they reviewed.  When the provider later reports a different
        matchup or a start time no longer near the approved one -- a reused or
        reassigned fixture ID -- the approval no longer covers the evidence, so
        the identity fails closed rather than lending the operator's decision
        to a game nobody reviewed.

        Only provider-side facts are compared, and only when both sides carry
        one, so an approval recorded without evidence is never second-guessed
        and a deliberate override to a differently scheduled game is not read
        as a conflict.  The matchup label is presentation and is never compared;
        a start time inside the reviewed window is a reschedule of the same
        game rather than a different one.

        The provider's own canonical event claim is such a fact -- it is what
        the provider says this fixture *is*, and two markets claiming different
        ones already contradict each other -- so an observation that renames
        the claim is evidence the operator never reviewed, whatever else about
        the matchup still matches.
        """

        previous_claim = existing.get("provider_canonical_event_id")
        if previous_claim is not None and evidence.canonical_id is not None:
            if str(previous_claim).strip() != str(evidence.canonical_id).strip():
                return True
        for side in ("home", "away"):
            team = getattr(evidence, f"{side}_team")
            if team is None:
                continue
            identities = [
                (existing.get(f"provider_{side}_team_canonical_id"), team.canonical_id),
                (existing.get(f"provider_{side}_team_id"), team.provider_id),
            ]
            comparable = [
                (str(previous).strip(), str(current).strip())
                for previous, current in identities
                if previous is not None and current is not None
            ]
            if comparable:
                if any(previous != current for previous, current in comparable):
                    return True
                continue
            previous_abbreviation = existing.get(f"provider_{side}_team_abbreviation")
            if previous_abbreviation and team.abbreviation:
                if previous_abbreviation.casefold() != team.abbreviation.casefold():
                    return True
                continue
            previous_name = existing.get(f"provider_{side}_team_name")
            if previous_name and team.name:
                if normalize_team_label(previous_name) != normalize_team_label(
                    team.name
                ):
                    return True
        previous_start = stored_timestamp(existing.get("provider_starts_at"))
        if previous_start is not None and evidence.starts_at is not None:
            if abs(_utc(evidence.starts_at) - _utc(previous_start)) > window:
                return True
        return False

    # -- collaborator reads ------------------------------------------------

    def _catalog_unavailable_reason(self, season: str) -> str | None:
        """Report why the season has no usable comparison identity, if so.

        Missing and over-age catalog data are both unusable, and neither is a
        provider defect: the normalized markets stay visible while event
        comparison identity is simply unavailable until a refresh.
        """

        freshness = self._call_catalog("get_freshness", season)
        if not isinstance(freshness, Mapping):
            return "event_catalog_missing"
        if not freshness.get("last_success_at"):
            return "event_catalog_missing"
        if not freshness.get("fresh"):
            return "event_catalog_stale"
        return None

    def _catalog_events(self, season: str) -> tuple[CanonicalEvent, ...]:
        rows = self._call_catalog("get_events", season) or ()
        return tuple(CanonicalEvent.from_row(row) for row in rows)

    def _call_catalog(self, method: str, season: str) -> Any:
        try:
            return getattr(self.catalog, method)(season)
        except SQLAlchemyError as exc:
            raise EventMappingPersistenceError(
                DEFAULT_EVENT_MAPPING_FAILURE_SUMMARY
            ) from exc

    def _get_mapping(self, provider: str, provider_id: str) -> Mapping[str, Any] | None:
        if self.mapping_repository is None:
            return None
        record = self.mapping_repository.get_mapping(provider, provider_id)
        return None if record is None else record.to_dict()

    def _get_rejection(self, provider: str, provider_id: str) -> Mapping[str, Any] | None:
        if self.mapping_repository is None:
            return None
        record = self.mapping_repository.get_rejection(provider, provider_id)
        return None if record is None else record.to_dict()

    @classmethod
    def _unavailable_result(
        cls,
        provider: str,
        evidence: EventEvidence,
        season: str,
        reason: str,
    ) -> EventResolution:
        """One outcome for a season with no usable comparison identity."""

        return cls._result(
            provider,
            evidence,
            season,
            EventResolutionState.EVENT_CATALOG_UNAVAILABLE,
            reason=reason,
        )

    @staticmethod
    def _result(
        provider: str,
        evidence: EventEvidence,
        season: str,
        state: EventResolutionState,
        *,
        canonical: CanonicalEvent | None = None,
        candidates: Sequence[CanonicalEvent] = (),
        reason: str | None = None,
    ) -> EventResolution:
        return EventResolution(
            provider=provider,
            provider_evidence=evidence,
            season=season,
            state=state,
            canonical_event=canonical,
            candidates=tuple(candidates),
            reason=reason,
        )


__all__ = [
    "CLAIMING_EVENT_MAPPING_STATES",
    "DEFAULT_EVENT_MATCH_WINDOW",
    "WITHDRAWN_EVENT_CLAIM_STATES",
    "CanonicalEvent",
    "EventResolution",
    "EventResolutionState",
    "EventResolver",
    "event_match_window",
    "normalize_team_label",
    "stored_timestamp",
]
