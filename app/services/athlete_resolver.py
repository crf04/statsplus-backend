"""Conservative resolution of provider athlete evidence to the season catalog."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.providers.dfs import (
    AthleteEvidence,
    PlayerProjectionMarket,
    TeamEvidence,
)
from app.providers.nba_stats import validate_canonical_season


class MappingResolutionState(str, Enum):
    """Closed outcomes of one provider-evidence resolution attempt."""

    AUTO = "auto"
    MANUAL_APPROVED = "manual_approved"
    MANUAL_OVERRIDE = "manual_override"
    REJECTED = "rejected"
    MAPPING_CONFLICT = "mapping_conflict"
    AMBIGUOUS = "ambiguous"
    INACTIVE_ONLY = "inactive_only"
    TEAM_CONFLICT = "team_conflict"
    UNMATCHED = "unmatched"
    MISSING_IDENTITY = "missing_identity"
    REJECTION_CLEARED = "rejection_cleared"



@dataclass(frozen=True, slots=True)
class CanonicalAthlete:
    """Typed view of one season-scoped canonical athlete row."""

    season: str
    player_id: int
    display_name: str
    roster_status: str
    is_active: bool
    is_active_for_season: bool
    team_id: int | None = None
    team_name: str | None = None
    team_abbreviation: str | None = None

    @property
    def canonical_player_id(self) -> int:
        return self.player_id

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CanonicalAthlete":
        return cls(
            season=str(row.get("season") or ""),
            player_id=int(row["player_id"]),
            display_name=str(row.get("display_name") or ""),
            roster_status=str(row.get("roster_status") or ""),
            is_active=bool(row.get("is_active")),
            is_active_for_season=bool(
                row.get("is_active_for_season", row.get("is_active"))
            ),
            team_id=(None if row.get("team_id") is None else int(row["team_id"])),
            team_name=(None if row.get("team_name") is None else str(row["team_name"])),
            team_abbreviation=(
                None
                if row.get("team_abbreviation") is None
                else str(row["team_abbreviation"]).strip().upper()
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderAthleteEvidence:
    """Provider-qualified wrapper around the shared typed athlete evidence."""

    provider: str
    athlete: AthleteEvidence

    def __post_init__(self) -> None:
        provider = _provider_name(self.provider)
        if not isinstance(self.athlete, AthleteEvidence):
            raise ValueError("athlete must be AthleteEvidence")
        object.__setattr__(self, "provider", provider)

    @property
    def provider_id(self) -> str | None:
        return self.athlete.provider_id

    @property
    def name(self) -> str | None:
        return self.athlete.name

    @property
    def team(self) -> TeamEvidence | None:
        return self.athlete.team


@dataclass(frozen=True, slots=True)
class AthleteResolution:
    """Typed result retained by the board and mapping repository seams."""

    provider: str
    provider_evidence: AthleteEvidence
    season: str
    state: MappingResolutionState
    canonical_athlete: CanonicalAthlete | None = None
    candidates: tuple[CanonicalAthlete, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        provider = self.provider.strip().casefold() if isinstance(self.provider, str) else ""
        if not provider:
            raise ValueError("mapping provider must be a non-empty string")
        if not isinstance(self.provider_evidence, AthleteEvidence):
            raise ValueError("provider_evidence must be AthleteEvidence")
        season = validate_canonical_season(self.season)
        state = (
            self.state
            if isinstance(self.state, MappingResolutionState)
            else MappingResolutionState(self.state)
        )
        candidates = tuple(self.candidates)
        if any(not isinstance(candidate, CanonicalAthlete) for candidate in candidates):
            raise ValueError("mapping candidates must be CanonicalAthlete values")
        if state is MappingResolutionState.AUTO and self.canonical_athlete is None:
            raise ValueError("auto resolution requires a canonical athlete")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "season", season)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "candidates", candidates)

    @property
    def provider_athlete_id(self) -> str | None:
        return self.provider_evidence.provider_id

    @property
    def provider_id(self) -> str | None:
        return self.provider_athlete_id

    @property
    def canonical_player_id(self) -> int | None:
        return self.canonical_athlete.player_id if self.canonical_athlete else None

    @property
    def canonical_athlete_id(self) -> int | None:
        return self.canonical_player_id

    @property
    def qualified(self) -> bool:
        return self.is_auto_qualifying

    @property
    def status(self) -> MappingResolutionState:
        """Compatibility spelling for callers that use ``status``."""

        return self.state

    @property
    def is_auto_qualifying(self) -> bool:
        return self.state is MappingResolutionState.AUTO and self.canonical_athlete is not None

    @property
    def mapped(self) -> bool:
        return self.canonical_athlete is not None and self.state in {
            MappingResolutionState.AUTO,
            MappingResolutionState.MANUAL_APPROVED,
            MappingResolutionState.MANUAL_OVERRIDE,
        }


def normalize_athlete_name(value: str | None) -> str:
    """Normalize accents and presentation punctuation for exact comparisons.

    This deliberately does not apply aliases, initials, nicknames, or fuzzy
    similarity.  A normalized comparison is still exact after the reviewed
    Unicode/case/spacing normalization.
    """

    if not isinstance(value, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    # Punctuation and underscores are presentation, not identity.  Removing
    # them (rather than turning them into spaces) keeps D'Angelo/DAngelo and
    # Nikola_Jokic/Nikola Jokic exact matches while retaining no fuzzy logic.
    return "".join(char for char in without_marks if char.isalnum())


def _provider_name(value: str | None) -> str:
    name = value.strip().casefold() if isinstance(value, str) else ""
    if not name:
        raise ValueError("mapping provider must be a non-empty string")
    return name


def _evidence_from_value(value: AthleteEvidence | Mapping[str, Any]) -> AthleteEvidence:
    if isinstance(value, AthleteEvidence):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("athlete evidence must be AthleteEvidence or a mapping")
    raw_team = value.get("team")
    team = raw_team
    if isinstance(raw_team, Mapping):
        team = TeamEvidence(
            provider_id=raw_team.get("provider_id"),
            canonical_id=raw_team.get("canonical_id"),
            name=raw_team.get("name"),
            abbreviation=raw_team.get("abbreviation"),
        )
    return AthleteEvidence(
        provider_id=value.get("provider_id"),
        canonical_id=value.get("canonical_id"),
        name=value.get("name"),
        team=team,
    )


class AthleteResolver:
    """Resolve provider athlete evidence against one requested catalog season."""

    def __init__(
        self,
        catalog: Any | None = None,
        *,
        athlete_catalog: Any | None = None,
        catalog_service: Any | None = None,
        mapping_repository: Any | None = None,
        repository: Any | None = None,
    ) -> None:
        catalog = catalog or athlete_catalog or catalog_service
        mapping_repository = mapping_repository or repository
        if not callable(getattr(catalog, "get_catalog", None)):
            raise TypeError("athlete catalog must expose get_catalog")
        self.catalog = catalog
        self.mapping_repository = mapping_repository

    def resolve(
        self,
        provider_or_market: str | PlayerProjectionMarket | ProviderAthleteEvidence | AthleteEvidence | Mapping[str, Any] | None = None,
        evidence_or_season: AthleteEvidence | Mapping[str, Any] | str | None = None,
        season: str | None = None,
        *,
        provider: str | None = None,
        evidence: AthleteEvidence | Mapping[str, Any] | None = None,
        provider_evidence: ProviderAthleteEvidence | AthleteEvidence | Mapping[str, Any] | None = None,
        requested_season: str | None = None,
    ) -> AthleteResolution:
        """Resolve ``(provider, evidence, season)`` or ``(market, season)``.

        The market form keeps board integration typed while the explicit
        provider/evidence form is convenient for operators and tests.
        """

        if requested_season is not None and season is None:
            season = requested_season

        if provider_evidence is not None:
            if isinstance(provider_evidence, ProviderAthleteEvidence):
                provider = provider_evidence.provider
                evidence = provider_evidence.athlete
            else:
                evidence = provider_evidence
        if provider is not None:
            requested_provider = provider
            requested_evidence = evidence
            if requested_evidence is None and not isinstance(provider_or_market, str):
                requested_evidence = provider_or_market
            if requested_evidence is None and isinstance(evidence_or_season, (AthleteEvidence, Mapping)):
                requested_evidence = evidence_or_season
            if requested_evidence is None:
                raise ValueError("provider athlete evidence is required")
            provider = requested_provider
            evidence = _evidence_from_value(requested_evidence)
            requested_season = requested_season or season or (
                evidence_or_season if isinstance(evidence_or_season, str) else None
            )
        elif isinstance(provider_or_market, ProviderAthleteEvidence):
            provider = provider_or_market.provider
            evidence = provider_or_market.athlete
            requested_season = season or (
                evidence_or_season if isinstance(evidence_or_season, str) else None
            )
        elif isinstance(provider_or_market, PlayerProjectionMarket):
            market = provider_or_market
            provider = market.provider
            requested_season = season or (
                evidence_or_season if isinstance(evidence_or_season, str) else None
            )
            if market.athlete is None:
                raise ValueError("provider market has no athlete evidence")
            evidence = market.athlete
            if evidence.team is None and market.team is not None:
                evidence = AthleteEvidence(
                    provider_id=evidence.provider_id,
                    canonical_id=evidence.canonical_id,
                    name=evidence.name,
                    team=market.team,
                )
        elif isinstance(provider_or_market, str):
            provider = provider_or_market
            evidence = _evidence_from_value(evidence_or_season)  # type: ignore[arg-type]
            requested_season = season
        else:
            if provider is None:
                raise ValueError("provider is required for athlete evidence")
            evidence = _evidence_from_value(provider_or_market)  # type: ignore[arg-type]
            requested_season = requested_season or season or (
                evidence_or_season if isinstance(evidence_or_season, str) else None
            )

        if requested_season is None:
            raise ValueError("an explicit requested NBA season is required")
        canonical_season = validate_canonical_season(requested_season)
        normalized_provider = _provider_name(provider)

        if not evidence.provider_id:
            return self._result(
                normalized_provider,
                evidence,
                canonical_season,
                MappingResolutionState.MISSING_IDENTITY,
                reason="missing_identity",
            )

        existing = self._get_mapping(normalized_provider, evidence.provider_id)
        if existing is not None:
            existing_state = str(
                existing.get("mapping_state", existing.get("state", ""))
            )
            try:
                state = MappingResolutionState(existing_state)
            except ValueError:
                state = None
            if state is MappingResolutionState.MAPPING_CONFLICT:
                return self._result(
                    normalized_provider,
                    evidence,
                    canonical_season,
                    MappingResolutionState.MAPPING_CONFLICT,
                    reason="mapping_conflict",
                )
            if state in {
                MappingResolutionState.MANUAL_APPROVED,
                MappingResolutionState.MANUAL_OVERRIDE,
            } and bool(existing.get("is_active", True)):
                canonical = self._canonical_from_mapping(existing, canonical_season)
                return self._result(
                    normalized_provider,
                    evidence,
                    canonical_season,
                    state,
                    canonical=canonical,
                    reason=state.value,
                )

        rejection = self._get_rejection(normalized_provider, evidence.provider_id)
        if rejection is not None and bool(rejection.get("is_active", True)):
            return self._result(
                normalized_provider,
                evidence,
                canonical_season,
                MappingResolutionState.REJECTED,
                reason="rejected",
            )

        if not evidence.name:
            return self._result(
                normalized_provider,
                evidence,
                canonical_season,
                MappingResolutionState.UNMATCHED,
                reason="missing_name",
            )

        rows = self._catalog_rows(canonical_season)
        all_matches = tuple(
            CanonicalAthlete.from_row(row)
            for row in rows
            if normalize_athlete_name(row.get("display_name"))
            == normalize_athlete_name(evidence.name)
        )
        active_matches = tuple(
            athlete for athlete in all_matches if athlete.is_active_for_season
        )
        if not active_matches:
            if self._existing_name_conflicts(existing, evidence):
                return self._result(
                    normalized_provider,
                    evidence,
                    canonical_season,
                    MappingResolutionState.MAPPING_CONFLICT,
                    reason="mapping_conflict",
                )
            state = (
                MappingResolutionState.INACTIVE_ONLY
                if all_matches
                else MappingResolutionState.UNMATCHED
            )
            return self._result(
                normalized_provider,
                evidence,
                canonical_season,
                state,
                candidates=all_matches,
                reason=state.value,
            )
        if len(active_matches) != 1:
            return self._result(
                normalized_provider,
                evidence,
                canonical_season,
                MappingResolutionState.AMBIGUOUS,
                candidates=active_matches,
                reason="duplicate_name",
            )

        candidate = active_matches[0]
        if self._team_conflicts(evidence.team, candidate):
            state = MappingResolutionState.TEAM_CONFLICT
            if self._existing_auto_conflicts(existing, evidence, candidate):
                state = MappingResolutionState.MAPPING_CONFLICT
            return self._result(
                normalized_provider,
                evidence,
                canonical_season,
                state,
                canonical=candidate,
                candidates=active_matches,
                reason=state.value,
            )

        if self._existing_auto_conflicts(existing, evidence, candidate):
            return self._result(
                normalized_provider,
                evidence,
                canonical_season,
                MappingResolutionState.MAPPING_CONFLICT,
                canonical=candidate,
                candidates=active_matches,
                reason="mapping_conflict",
            )

        return self._result(
            normalized_provider,
            evidence,
            canonical_season,
            MappingResolutionState.AUTO,
            canonical=candidate,
            candidates=active_matches,
            reason="exact_normalized_name",
        )

    def resolve_evidence(
        self, provider: str, evidence: AthleteEvidence, season: str
    ) -> AthleteResolution:
        return self.resolve(provider, evidence, season)

    resolve_provider_athlete = resolve_evidence

    def resolve_market(
        self, market: PlayerProjectionMarket, season: str
    ) -> AthleteResolution:
        return self.resolve(market, season)

    def _catalog_rows(self, season: str) -> Sequence[Mapping[str, Any]]:
        try:
            rows = self.catalog.get_catalog(season, active_only=False)
        except TypeError:
            rows = self.catalog.get_catalog(season)
        return tuple(rows or ())

    def _get_mapping(self, provider: str, provider_id: str) -> Mapping[str, Any] | None:
        repository = self.mapping_repository
        if repository is None:
            return None
        value = repository.get_mapping(provider, provider_id)
        return _as_mapping(value)

    def _get_rejection(self, provider: str, provider_id: str) -> Mapping[str, Any] | None:
        repository = self.mapping_repository
        if repository is None:
            return None
        value = repository.get_rejection(provider, provider_id)
        return _as_mapping(value)

    @staticmethod
    def _team_conflicts(
        provider_team: TeamEvidence | None,
        canonical: CanonicalAthlete,
    ) -> bool:
        if provider_team is None:
            return False
        # An agreed canonical ID is the strongest typed team evidence.  Text
        # labels may differ by provider, so only compare them when no IDs are
        # available to establish identity.
        if provider_team.canonical_id is not None and canonical.team_id is not None:
            return provider_team.canonical_id != canonical.team_id
        if provider_team.name and canonical.team_name:
            if normalize_athlete_name(provider_team.name) != normalize_athlete_name(
                canonical.team_name
            ):
                return True
        if provider_team.abbreviation and canonical.team_abbreviation:
            return provider_team.abbreviation.casefold() != canonical.team_abbreviation.casefold()
        return False

    @staticmethod
    def _existing_auto_conflicts(
        existing: Mapping[str, Any] | None,
        evidence: AthleteEvidence,
        candidate: CanonicalAthlete,
    ) -> bool:
        if not existing or not bool(existing.get("is_active", False)):
            return False
        state = str(existing.get("mapping_state", existing.get("state", "")))
        if state not in {MappingResolutionState.AUTO.value, "auto_mapped"}:
            return False
        previous_player_id = existing.get("canonical_player_id")
        if previous_player_id is not None and int(previous_player_id) != candidate.player_id:
            return True
        previous_name = existing.get("provider_name")
        if previous_name and evidence.name and normalize_athlete_name(previous_name) != normalize_athlete_name(evidence.name):
            return True
        previous_team_id = existing.get("canonical_team_id")
        current_team_id = evidence.team.canonical_id if evidence.team else None
        return bool(
            previous_team_id is not None
            and current_team_id is not None
            and int(previous_team_id) != int(current_team_id)
        )

    @staticmethod
    def _existing_name_conflicts(
        existing: Mapping[str, Any] | None,
        evidence: AthleteEvidence,
    ) -> bool:
        if not existing or not bool(existing.get("is_active", False)):
            return False
        state = str(existing.get("mapping_state", existing.get("state", "")))
        if state not in {MappingResolutionState.AUTO.value, "auto_mapped"}:
            return False
        previous_name = existing.get("provider_name")
        return bool(
            previous_name
            and evidence.name
            and normalize_athlete_name(previous_name)
            != normalize_athlete_name(evidence.name)
        )

    @staticmethod
    def _canonical_from_mapping(
        mapping: Mapping[str, Any], season: str
    ) -> CanonicalAthlete | None:
        player_id = mapping.get("canonical_player_id")
        if player_id is None:
            return None
        return CanonicalAthlete(
            season=str(mapping.get("season") or season),
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
    def _result(
        provider: str,
        evidence: AthleteEvidence,
        season: str,
        state: MappingResolutionState,
        *,
        canonical: CanonicalAthlete | None = None,
        candidates: Sequence[CanonicalAthlete] = (),
        reason: str | None = None,
    ) -> AthleteResolution:
        return AthleteResolution(
            provider=provider,
            provider_evidence=evidence,
            season=season,
            state=state,
            canonical_athlete=canonical,
            candidates=tuple(candidates),
            reason=reason,
        )


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "to_dict"):
        candidate = value.to_dict()
        if isinstance(candidate, Mapping):
            return candidate
    try:
        return {key: getattr(value, key) for key in vars(value)}
    except (TypeError, AttributeError):
        return None


__all__ = [
    "AthleteResolution",
    "AthleteResolver",
    "CanonicalAthlete",
    "MappingResolutionState",
    "ProviderAthleteEvidence",
    "normalize_athlete_name",
]
