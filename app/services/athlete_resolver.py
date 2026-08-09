"""Conservative resolution of provider athlete evidence to the season catalog."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.providers.dfs import (
    AthleteEvidence,
    PlayerProjectionMarket,
    TeamEvidence,
)
from app.providers.nba_stats import validate_canonical_season
from app.services.athlete_mapping_errors import (
    DEFAULT_MAPPING_FAILURE_SUMMARY,
    AthleteMappingPersistenceError,
)


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


#: Latin letters that Unicode decomposition cannot reduce to ASCII because the
#: stroke, bar, or ligature is part of the letter rather than a combining mark.
#: Only these documented substitutions are applied; nothing here is an alias,
#: nickname, or fuzzy rule.
_NON_DECOMPOSING_LATIN_LETTERS = {
    "ø": "o",
    "œ": "oe",
    "æ": "ae",
    "ł": "l",
    "đ": "d",
    "ð": "d",
    "þ": "th",
    "ħ": "h",
    "ŧ": "t",
    "ı": "i",
}


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
    def canonical_player_id(self) -> int | None:
        return self.canonical_athlete.player_id if self.canonical_athlete else None

    @property
    def is_auto_qualifying(self) -> bool:
        return self.state is MappingResolutionState.AUTO and self.canonical_athlete is not None


def normalize_athlete_name(value: str | None) -> str:
    """Normalize accents and presentation punctuation for exact comparisons.

    Unicode decomposition removes combining marks, and the documented
    ``_NON_DECOMPOSING_LATIN_LETTERS`` table folds the Latin letters whose
    stroke or ligature is not a combining mark (``ø``, ``ł``, ``œ``, and the
    rest of that table) to their ASCII equivalents.  This deliberately does
    not apply aliases, initials, nicknames, or fuzzy similarity.  A normalized
    comparison is still exact after the reviewed Unicode/case/spacing
    normalization.
    """

    if not isinstance(value, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    folded = "".join(
        _NON_DECOMPOSING_LATIN_LETTERS.get(char, char) for char in without_marks
    )
    # Punctuation and underscores are presentation, not identity.  Removing
    # them (rather than turning them into spaces) keeps D'Angelo/DAngelo and
    # Nikola_Jokic/Nikola Jokic exact matches while retaining no fuzzy logic.
    return "".join(char for char in folded if char.isalnum())


def _provider_name(value: str | None) -> str:
    name = value.strip().casefold() if isinstance(value, str) else ""
    if not name:
        raise ValueError("mapping provider must be a non-empty string")
    return name


class AthleteResolver:
    """Resolve provider athlete evidence against one requested catalog season."""

    def __init__(
        self,
        catalog: Any,
        *,
        mapping_repository: Any | None = None,
    ) -> None:
        if not callable(getattr(catalog, "get_catalog", None)):
            raise TypeError("athlete catalog must expose get_catalog")
        self.catalog = catalog
        self.mapping_repository = mapping_repository

    def resolve(
        self,
        provider: str,
        evidence: AthleteEvidence,
        season: str,
    ) -> AthleteResolution:
        """Resolve one provider's typed athlete evidence for one season."""

        if not isinstance(evidence, AthleteEvidence):
            raise TypeError("evidence must be AthleteEvidence")
        canonical_season = validate_canonical_season(season)
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
            try:
                state = MappingResolutionState(str(existing.get("mapping_state", "")))
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
            retained = self._retained_identity(existing, rows, canonical_season)
            if retained is not None:
                if self._team_conflicts(evidence.team, retained):
                    # Preserving the canonical ID across a label change never
                    # bypasses team validation: provider team evidence that
                    # disagrees with the requested season's canonical row is a
                    # conflict for an operator, not an automatic mapping.
                    return self._result(
                        normalized_provider,
                        evidence,
                        canonical_season,
                        MappingResolutionState.MAPPING_CONFLICT,
                        canonical=retained,
                        candidates=(retained,),
                        reason="mapping_conflict",
                    )
                return self._result(
                    normalized_provider,
                    evidence,
                    canonical_season,
                    MappingResolutionState.AUTO,
                    canonical=retained,
                    candidates=(retained,),
                    reason="retained_canonical_identity",
                )
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

    def resolve_market(
        self, market: PlayerProjectionMarket, season: str
    ) -> AthleteResolution:
        """Resolve the athlete evidence carried by one normalized market."""

        if not isinstance(market, PlayerProjectionMarket):
            raise TypeError("market must be PlayerProjectionMarket")
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
        return self.resolve(market.provider, evidence, season)

    def _catalog_rows(self, season: str) -> Sequence[Mapping[str, Any]]:
        try:
            rows = self.catalog.get_catalog(season, active_only=False)
        except SQLAlchemyError as exc:
            raise AthleteMappingPersistenceError(
                DEFAULT_MAPPING_FAILURE_SUMMARY
            ) from exc
        return tuple(rows or ())

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
        """Report whether an established mapping disagrees with this candidate.

        Team evidence is judged by ``_team_conflicts`` against the candidate
        for the *requested* season, never against the team a mapping recorded
        for an earlier season, so a player who legitimately changes teams
        between seasons is not read as conflicting evidence.
        """

        if not existing or not bool(existing.get("is_active", False)):
            return False
        if str(existing.get("mapping_state", "")) != MappingResolutionState.AUTO.value:
            return False
        previous_player_id = existing.get("canonical_player_id")
        if previous_player_id is not None:
            # The canonical player ID is the identity.  When it still agrees,
            # a changed official display name is a relabeling of the same NBA
            # player rather than disagreeing evidence.
            return int(previous_player_id) != candidate.player_id
        previous_name = existing.get("provider_name")
        return bool(
            previous_name
            and evidence.name
            and normalize_athlete_name(previous_name)
            != normalize_athlete_name(evidence.name)
        )

    @staticmethod
    def _retained_identity(
        existing: Mapping[str, Any] | None,
        rows: Sequence[Mapping[str, Any]],
        season: str,
    ) -> CanonicalAthlete | None:
        """Keep an established identity whose catalog row was only relabeled.

        Official display names change (suffixes, legal names, spellings) on
        both the catalog and the provider side.  An identity already mapped to
        a canonical player ID that is still active for the season stays mapped
        to that player, so a label change alone never turns it into an
        unmatched or conflicting identity.  The canonical facts are re-read
        from the catalog row so the current official name is retained.
        """

        if not existing or not bool(existing.get("is_active", False)):
            return None
        if str(existing.get("mapping_state", "")) != MappingResolutionState.AUTO.value:
            return None
        player_id = existing.get("canonical_player_id")
        if player_id is None:
            return None
        for row in rows:
            if int(row["player_id"]) != int(player_id):
                continue
            athlete = CanonicalAthlete.from_row({**row, "season": season})
            return athlete if athlete.is_active_for_season else None
        return None

    @staticmethod
    def _existing_name_conflicts(
        existing: Mapping[str, Any] | None,
        evidence: AthleteEvidence,
    ) -> bool:
        if not existing or not bool(existing.get("is_active", False)):
            return False
        if str(existing.get("mapping_state", "")) != MappingResolutionState.AUTO.value:
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


__all__ = [
    "AthleteResolution",
    "AthleteResolver",
    "CanonicalAthlete",
    "MappingResolutionState",
    "normalize_athlete_name",
]
