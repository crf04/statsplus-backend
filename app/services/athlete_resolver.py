"""Conservative resolution of provider athlete evidence to the season catalog."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.providers.dfs import (
    AthleteEvidence,
    PlayerProjectionMarket,
    TeamEvidence,
    normalize_timestamp,
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


#: Mapping states that withdrew an automatic claim from board comparisons
#: without retracting it.  The identity still belongs to the canonical athlete
#: the row names.
WITHDRAWN_CLAIM_STATES = frozenset(
    {
        MappingResolutionState.INACTIVE_ONLY.value,
        MappingResolutionState.AMBIGUOUS.value,
        MappingResolutionState.UNMATCHED.value,
    }
)


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
    #: When the provider observation behind this result was retrieved, in UTC.
    #: It is the provider's own instant, never a persistence time, so a slow
    #: read that lands after a later decision can still be recognized as older
    #: than it.  ``None`` when the caller observed no provider read.
    observed_at: datetime | str | None = None

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
        object.__setattr__(self, "observed_at", normalize_timestamp(self.observed_at))

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
        *,
        observed_at: datetime | str | None = None,
    ) -> AthleteResolution:
        """Resolve one provider's typed athlete evidence for one season.

        ``observed_at`` is when the provider was read.  Resolution itself is a
        pure function of the evidence and the catalog, so the instant is only
        carried onto the result for the persistence seam to order it against
        decisions taken while the read was in flight.
        """

        resolution = self._resolve(provider, evidence, season)
        if observed_at is None:
            return resolution
        return replace(resolution, observed_at=observed_at)

    def _resolve(
        self,
        provider: str,
        evidence: AthleteEvidence,
        season: str,
    ) -> AthleteResolution:
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
                if self._manual_evidence_conflicts(existing, evidence):
                    # Operator precedence protects a governed mapping from
                    # automatic overwrite; it does not assert the identity for
                    # evidence the operator never reviewed.
                    return self._result(
                        normalized_provider,
                        evidence,
                        canonical_season,
                        MappingResolutionState.MAPPING_CONFLICT,
                        reason="manual_mapping_conflict",
                    )
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
            # An established claim is resolved by its canonical player ID, not
            # by the label either side currently prints, so the order is: an
            # exact label naming a different athlete, then a provider name that
            # no longer agrees with the observed one, then the claimed row --
            # retained while it is active for the season, withdrawn once the
            # catalog only lists it as inactive.
            claimed_player_id = self._claimed_player_id(existing)
            if claimed_player_id is not None:
                mismatched = tuple(
                    athlete
                    for athlete in all_matches
                    if athlete.player_id != claimed_player_id
                )
                if mismatched or self._provider_name_changed(existing, evidence):
                    return self._result(
                        normalized_provider,
                        evidence,
                        canonical_season,
                        MappingResolutionState.MAPPING_CONFLICT,
                        # One exactly matching athlete is the candidate the
                        # operator has to weigh against the claim; several
                        # equally exact ones name none of them.
                        canonical=(mismatched[0] if len(mismatched) == 1 else None),
                        candidates=mismatched,
                        reason="mapping_conflict",
                    )
                claimed = self._catalog_athlete(rows, claimed_player_id, canonical_season)
                if claimed is not None and claimed.is_active_for_season:
                    if self._team_conflicts(evidence.team, claimed):
                        # Preserving the canonical ID across a label change never
                        # bypasses team validation: provider team evidence that
                        # disagrees with the requested season's canonical row is a
                        # conflict for an operator, not an automatic mapping.
                        return self._result(
                            normalized_provider,
                            evidence,
                            canonical_season,
                            MappingResolutionState.MAPPING_CONFLICT,
                            canonical=claimed,
                            candidates=(claimed,),
                            reason="mapping_conflict",
                        )
                    return self._result(
                        normalized_provider,
                        evidence,
                        canonical_season,
                        MappingResolutionState.AUTO,
                        canonical=claimed,
                        candidates=(claimed,),
                        reason="retained_canonical_identity",
                    )
                if claimed is not None:
                    # The catalog still lists the claimed athlete, only as
                    # inactive for this season, whatever it now calls the row.
                    return self._result(
                        normalized_provider,
                        evidence,
                        canonical_season,
                        MappingResolutionState.INACTIVE_ONLY,
                        candidates=(claimed,),
                        reason="inactive_only",
                    )
                # The catalog no longer lists the claimed athlete at all for
                # this season, while the provider still reports the name the
                # claim was established on.  Nothing supports comparing the
                # identity any more, so the claim is withdrawn as unmatched
                # rather than retained -- and the athlete that disappeared is
                # named as the observation's candidate, which is what tells an
                # operator this unmatched evidence withdrew a claim instead of
                # simply matching nothing.
                return self._result(
                    normalized_provider,
                    evidence,
                    canonical_season,
                    MappingResolutionState.UNMATCHED,
                    candidates=self._absent_claim_candidates(existing, canonical_season),
                    reason="claimed_athlete_absent",
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
        self,
        market: PlayerProjectionMarket,
        season: str,
        *,
        observed_at: datetime | str | None = None,
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
        return self.resolve(
            market.provider, evidence, season, observed_at=observed_at
        )

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
    def _asserts_automatic_claim(existing: Mapping[str, Any] | None) -> bool:
        """Report whether a mapping row still asserts an automatic claim.

        An active automatic mapping does.  So does one withdrawn from
        comparisons because the catalog only lists its athlete as inactive for
        the requested season, because the catalog named two equally exact
        athletes and the board could not choose, or because the catalog stopped
        listing the claimed athlete at all: every withdrawal suspends the
        claim rather than retracting it, so the identity still belongs to the
        canonical athlete it was mapped to, and a later observation naming a
        different athlete under the same provider ID is still a conflict rather
        than a silent remap.
        """

        if not existing:
            return False
        state = str(existing.get("mapping_state", ""))
        if state in WITHDRAWN_CLAIM_STATES:
            return True
        return state == MappingResolutionState.AUTO.value and bool(
            existing.get("is_active", False)
        )

    @classmethod
    def _existing_auto_conflicts(
        cls,
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

        if not cls._asserts_automatic_claim(existing):
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
    def _manual_evidence_conflicts(
        existing: Mapping[str, Any],
        evidence: AthleteEvidence,
    ) -> bool:
        """Report whether the provider identity changed under a manual mapping.

        An operator approves *this provider identity* as *this canonical
        athlete* on the evidence they reviewed.  When the provider later
        reports a different athlete under the same ID -- a reused or reassigned
        identity such as Nikola/DEN becoming LeBron/LAL -- the approval no
        longer covers the evidence, so the identity fails closed rather than
        lending the operator's decision to an athlete nobody reviewed.

        Only provider-side facts are compared, and only when both sides carry
        one, so an approval recorded without evidence is never second-guessed
        and a deliberate override to a differently named canonical athlete is
        not read as a conflict.

        Team evidence is judged at the strongest tier both sides recorded:
        identities first -- the canonical team ID and the provider's own team
        ID -- then the abbreviation, then the team name.  A disagreement at any
        comparable identity is a conflict, and agreement there makes a differing
        label a presentation difference rather than a changed athlete.
        """

        previous_name = existing.get("provider_name")
        if (
            previous_name
            and evidence.name
            and normalize_athlete_name(previous_name)
            != normalize_athlete_name(evidence.name)
        ):
            return True
        team = evidence.team
        if team is None:
            return False
        team_identities = [
            (existing.get("provider_team_canonical_id"), team.canonical_id),
            (existing.get("provider_team_id"), team.provider_id),
        ]
        comparable = [
            (str(previous).strip(), str(current).strip())
            for previous, current in team_identities
            if previous is not None and current is not None
        ]
        if comparable:
            return any(previous != current for previous, current in comparable)
        previous_abbreviation = existing.get("provider_team_abbreviation")
        if previous_abbreviation and team.abbreviation:
            return previous_abbreviation.casefold() != team.abbreviation.casefold()
        previous_team_name = existing.get("provider_team_name")
        if previous_team_name and team.name:
            return normalize_athlete_name(previous_team_name) != normalize_athlete_name(
                team.name
            )
        return False

    @classmethod
    def _claimed_player_id(cls, existing: Mapping[str, Any] | None) -> int | None:
        """Return the canonical player an established claim still names.

        Official display names change (suffixes, legal names, spellings) on
        both the catalog and the provider side, so the claim is read from the
        stable canonical player ID rather than from either label.
        """

        if not cls._asserts_automatic_claim(existing):
            return None
        player_id = existing.get("canonical_player_id")
        return None if player_id is None else int(player_id)

    @staticmethod
    def _absent_claim_candidates(
        existing: Mapping[str, Any] | None,
        season: str,
    ) -> tuple["CanonicalAthlete", ...]:
        """Name the claimed athlete the requested season no longer lists.

        The facts come from the mapping row rather than the catalog, because
        the catalog is exactly what stopped carrying them.  The candidate is
        recorded as not active for the season: it is not a row an operator can
        approve as it stands, it is the claim this observation withdrew.
        """

        existing = existing or {}
        player_id = existing.get("canonical_player_id")
        if player_id is None:
            return ()
        return (
            CanonicalAthlete(
                season=str(existing.get("season") or season),
                player_id=int(player_id),
                display_name=str(existing.get("canonical_name") or ""),
                roster_status="absent",
                is_active=False,
                is_active_for_season=False,
                team_id=(
                    None
                    if existing.get("canonical_team_id") is None
                    else int(existing["canonical_team_id"])
                ),
                team_name=existing.get("canonical_team_name"),
                team_abbreviation=existing.get("canonical_team_abbreviation"),
            ),
        )

    @staticmethod
    def _catalog_athlete(
        rows: Sequence[Mapping[str, Any]],
        player_id: int,
        season: str,
    ) -> CanonicalAthlete | None:
        """Read one canonical athlete out of the requested season's catalog.

        The facts come from the catalog row, so the current official name and
        the row's activity for this season are both what the catalog says now.
        """

        for row in rows:
            if int(row["player_id"]) == player_id:
                return CanonicalAthlete.from_row({**row, "season": season})
        return None

    @staticmethod
    def _provider_name_changed(
        existing: Mapping[str, Any] | None,
        evidence: AthleteEvidence,
    ) -> bool:
        """Report whether the provider stopped reporting the observed name.

        Retaining a canonical identity through a catalog rename is only safe
        while the provider still names the athlete we observed under that ID.
        Once the provider reports a different name that matches nothing active,
        the identity may have been reused, so it fails closed instead.
        """

        previous_name = (existing or {}).get("provider_name")
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
    "WITHDRAWN_CLAIM_STATES",
    "AthleteResolution",
    "AthleteResolver",
    "CanonicalAthlete",
    "MappingResolutionState",
    "normalize_athlete_name",
]
