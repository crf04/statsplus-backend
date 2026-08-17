"""Bounded dual-run parity between legacy and ledger matchup materializers.

The ledger-owned Season and exact-L15 matchup materializer
(:class:`app.services.ledger_matchup_materialization.LedgerMatchupMaterializationService`)
and the legacy provider-aggregate writer
(:class:`app.services.team_matchup_refresh.TeamMatchupRefreshService`) both
produce the disposable ``team_matchup_facts`` read model for the same governed
season and cutoff.  The two materializers write the same surface rows, so their
outputs never coexist in one stored snapshot; each side must be produced into
its own isolated store or captured in memory before it is compared.  This
module owns the seam that accepts those two independently produced outputs,
compares them against the exact immutable governed authority, and records
durable activation evidence -- without ever advancing a publication pointer.

The comparison is bounded to the two ledger-owned non-shot surfaces --
traditional opponent counts and assist locations.  NBA-owned shot zones,
grouped shot types, and Synergy play types are composed from governed
publications, never from the ledger, so they have no legacy-vs-ledger dual-run
and are excluded here.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.domain.team_matchup_taxonomy import (
    LEDGER_OWNED_MATCHUP_SURFACES,
    matchup_stream_key,
)
from app.services.ledger_derivations import (
    MATCHUP_ASSIST_KEYS,
    MATCHUP_TRADITIONAL_KEYS,
    competition_ranks,
)
from app.services.ledger_lineage import LedgerLineage
from app.services.team_matchup_repository import (
    TeamMatchupFact,
    TeamMatchupObservation,
)

#: The single documented tolerance for floating denominators and derived
#: per-48 rates.  Integer counts compare exactly; only denominators (effective
#: team minutes) and the per-48 values recomputed from them admit this relative
#: tolerance, because the legacy provider reports total team minutes while the
#: ledger retains effective player-minute denominators (player minutes divided
#: by five).
MATCHUP_PARITY_TOLERANCE = 1e-6

#: Ledger-owned surfaces that participate in the dual-run (re-exported from the
#: canonical taxonomy so the comparator and the stream-key helper never drift).
LEDGER_OWNED_SURFACES = LEDGER_OWNED_MATCHUP_SURFACES

#: Per-surface contracted count metrics, keyed by the fact stat key.
_COUNT_KEYS = {
    "traditional": tuple(MATCHUP_TRADITIONAL_KEYS),
    "assist_locations": tuple(MATCHUP_ASSIST_KEYS),
}

#: Per-48 scaling shared by both materializers.
_PER48 = 48.0

# Closed difference classifications.  Every produced difference names exactly
# one of these reasons so adjudication can classify unexplained required
# differences without parsing free text.
CLASSIFICATION_LEAGUE_INCOMPLETE = "league_incomplete"
CLASSIFICATION_MISSING_LEGACY_TEAM = "missing_legacy_team"
CLASSIFICATION_MISSING_LEDGER_TEAM = "missing_ledger_team"
CLASSIFICATION_GAME_SET_MISMATCH = "game_set_mismatch"
CLASSIFICATION_INTEGER_COUNT_DIFFERENCE = "integer_count_difference"
CLASSIFICATION_NON_INTEGER_COUNT = "non_integer_count"
CLASSIFICATION_DENOMINATOR_TOLERANCE_EXCEEDED = "denominator_tolerance_exceeded"
CLASSIFICATION_DERIVED_RATE_DIFFERENCE = "derived_rate_difference"
CLASSIFICATION_RANKING_DIFFERENCE = "ranking_difference"
CLASSIFICATION_AVAILABILITY_DIFFERENCE = "availability_difference"
CLASSIFICATION_CUTOFF_MISMATCH = "cutoff_mismatch"
CLASSIFICATION_MISSING_SURFACE = "missing_surface"
CLASSIFICATION_MISSING_METRIC = "missing_metric"


def matchup_surface_stream_keys(window: str) -> tuple[str, ...]:
    """Return every ledger-owned stream key for one governed window."""

    return tuple(matchup_stream_key(surface, window) for surface in LEDGER_OWNED_SURFACES)


@dataclass(frozen=True, slots=True)
class MatchupMaterialization:
    """One independently produced materializer output at an exact cutoff."""

    season: str
    window: str
    cutoff: datetime
    facts: tuple[TeamMatchupFact, ...]
    observations: tuple[TeamMatchupObservation, ...]
    game_ids_by_team: Mapping[int, frozenset[str]]


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
        return {
            "window": self.window,
            "surface": self.surface,
            "team_id": self.team_id,
            "field": self.field,
            "ledger_value": _json_value(self.ledger_value),
            "legacy_value": _json_value(self.legacy_value),
            "classification": self.classification,
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
    differences: tuple[MatchupParityDifference, ...] = ()

    @property
    def adjudication_required(self) -> bool:
        return bool(self.differences)

    @property
    def exact(self) -> bool:
        """Whether every required comparison passed with no differences."""

        return (
            self.compared_count > 0
            and not self.adjudication_required
            and self.league_complete
            and self.team_identities_exact
            and self.game_sets_exact
            and self.cutoffs_aligned
            and self.rankings_deterministic
        )

    @property
    def status(self) -> str:
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
            "differences": [difference.to_dict() for difference in self.differences],
        }


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (tuple, frozenset, set, list)):
        return sorted(str(item) for item in value)
    return value


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
    if unit == "seconds":
        return float(value) / 60.0
    return float(value)


def _game_set_checksum(game_ids: Iterable[str]) -> str:
    return LedgerLineage.for_game_ids(game_ids)


def compare_matchup_materializations(
    legacy: MatchupMaterialization,
    ledger: MatchupMaterialization,
    *,
    surface: str,
    expected_team_ids: Iterable[int],
    expected_game_ids_by_team: Mapping[int, Iterable[str]],
    tolerance: float = MATCHUP_PARITY_TOLERANCE,
) -> MatchupParityReport:
    """Compare two independently produced materializations for one surface.

    Integer counts must be exactly equal; floating denominators and derived
    per-48 rates use ``tolerance``.  Team identities and both sides' exact game
    sets must equal the governed authority, every contracted metric must be
    present on both sides, and both sides must report the surface available.
    Any difference makes the report require adjudication.
    """

    if surface not in _COUNT_KEYS:
        raise ValueError(f"unsupported matchup surface {surface}")
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be finite and non-negative")

    expected = frozenset(int(team_id) for team_id in expected_team_ids)
    if len(expected) != 30:
        raise ValueError("expected_team_ids must name exactly 30 teams")

    differences: list[MatchupParityDifference] = []
    window = legacy.window

    # Cutoff alignment: both sides must describe the exact same aware cutoff.
    cutoffs_aligned = (
        legacy.cutoff is not None
        and ledger.cutoff is not None
        and legacy.cutoff.tzinfo is not None
        and ledger.cutoff.tzinfo is not None
        and legacy.cutoff == ledger.cutoff
    )
    if not cutoffs_aligned:
        differences.append(MatchupParityDifference(
            window, surface, None, "cutoff", ledger.cutoff, legacy.cutoff,
            CLASSIFICATION_CUTOFF_MISMATCH,
        ))
    if legacy.season != ledger.season:
        differences.append(MatchupParityDifference(
            window, surface, None, "season", ledger.season, legacy.season,
            CLASSIFICATION_CUTOFF_MISMATCH,
        ))
    if legacy.window != ledger.window:
        differences.append(MatchupParityDifference(
            window, surface, None, "window", ledger.window, legacy.window,
            CLASSIFICATION_CUTOFF_MISMATCH,
        ))

    legacy_facts = _facts_for_surface(legacy.facts, surface=surface)
    ledger_facts = _facts_for_surface(ledger.facts, surface=surface)
    legacy_teams = {team_id for team_id, _ in legacy_facts}
    ledger_teams = {team_id for team_id, _ in ledger_facts}

    # League-Complete team coverage: both sides must cover the exact governed
    # 30-team roster, and the identity sets must match.
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

    # Surface presence: both sides must carry facts for the surface.  A
    # both-missing surface, or a surface present on only one side, cannot pass.
    if not legacy_facts or not ledger_facts:
        differences.append(MatchupParityDifference(
            window, surface, None, "surface",
            "present" if ledger_facts else None,
            "present" if legacy_facts else None,
            CLASSIFICATION_MISSING_SURFACE,
        ))

    # Exact governed game sets: each side's per-team selection must equal the
    # governed authority and each other, proven by byte-identical checksums.
    governed_game_ids = {
        int(team_id): frozenset(str(game_id) for game_id in game_ids)
        for team_id, game_ids in expected_game_ids_by_team.items()
    }
    game_sets_exact = True
    for team_id in sorted(expected):
        legacy_ids = legacy.game_ids_by_team.get(team_id, frozenset())
        ledger_ids = ledger.game_ids_by_team.get(team_id, frozenset())
        governed_ids = governed_game_ids.get(team_id, frozenset())
        if not (legacy_ids == ledger_ids == governed_ids):
            game_sets_exact = False
            differences.append(MatchupParityDifference(
                window, surface, team_id, "game_ids",
                sorted(ledger_ids), sorted(legacy_ids),
                CLASSIFICATION_GAME_SET_MISMATCH,
            ))
    if legacy.game_ids_by_team and ledger.game_ids_by_team:
        legacy_checksum = _game_set_checksum(
            game_id for ids in legacy.game_ids_by_team.values() for game_id in ids
        )
        ledger_checksum = _game_set_checksum(
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
    compared = 0
    metric_keys = _COUNT_KEYS[surface]
    rates_by_side: dict[str, dict[str, dict[int, float]]] = {
        "ledger": {},
        "legacy": {},
    }
    shared_teams = sorted(ledger_teams & legacy_teams)
    for metric in metric_keys:
        for team_id in shared_teams:
            key = (team_id, metric)
            ledger_fact = ledger_facts.get(key)
            legacy_fact = legacy_facts.get(key)
            if ledger_fact is None or legacy_fact is None:
                differences.append(MatchupParityDifference(
                    window, surface, team_id, metric,
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
                    window, surface, team_id, metric,
                    ledger_fact.raw_value, legacy_fact.raw_value,
                    CLASSIFICATION_NON_INTEGER_COUNT,
                ))
            elif ledger_count != legacy_count:
                differences.append(MatchupParityDifference(
                    window, surface, team_id, metric,
                    ledger_count, legacy_count,
                    CLASSIFICATION_INTEGER_COUNT_DIFFERENCE,
                ))
            ledger_minutes = _minutes(
                ledger_fact.denominator_value, ledger_fact.denominator_unit
            )
            legacy_minutes = _minutes(
                legacy_fact.denominator_value, legacy_fact.denominator_unit
            )
            denominator_matches = (
                ledger_minutes is not None
                and legacy_minutes is not None
                and math.isclose(
                    ledger_minutes, legacy_minutes,
                    rel_tol=tolerance, abs_tol=tolerance,
                )
            )
            if not denominator_matches:
                differences.append(MatchupParityDifference(
                    window, surface, team_id, "denominator_minutes",
                    ledger_minutes, legacy_minutes,
                    CLASSIFICATION_DENOMINATOR_TOLERANCE_EXCEEDED,
                ))
            ledger_rate = _per48(ledger_fact.raw_value, ledger_minutes)
            legacy_rate = _per48(legacy_fact.raw_value, legacy_minutes)
            if ledger_rate is not None and legacy_rate is not None and not math.isclose(
                ledger_rate, legacy_rate, rel_tol=tolerance, abs_tol=tolerance
            ):
                differences.append(MatchupParityDifference(
                    window, surface, team_id, f"per48.{metric}",
                    ledger_rate, legacy_rate,
                    CLASSIFICATION_DERIVED_RATE_DIFFERENCE,
                ))
            if ledger_rate is not None:
                rates_by_side["ledger"].setdefault(metric, {})[team_id] = ledger_rate
            if legacy_rate is not None:
                rates_by_side["legacy"].setdefault(metric, {})[team_id] = legacy_rate

    # Deterministic rankings: for each metric both sides derive the same
    # competition ranks (ascending, ties 1, 1, 3) from their per-48 values.
    rankings_deterministic = True
    for metric in metric_keys:
        ledger_metric = rates_by_side["ledger"].get(metric, {})
        legacy_metric = rates_by_side["legacy"].get(metric, {})
        if not ledger_metric and not legacy_metric:
            continue
        ledger_ranks = competition_ranks(ledger_metric, descending=False)
        legacy_ranks = competition_ranks(legacy_metric, descending=False)
        if ledger_ranks != legacy_ranks:
            rankings_deterministic = False
            differences.append(MatchupParityDifference(
                window, surface, None, f"competition_rank.{metric}",
                ledger_ranks, legacy_ranks,
                CLASSIFICATION_RANKING_DIFFERENCE,
            ))

    # Independent per-surface availability: both sides must report the surface
    # available.  An unavailable or missing observation is a real difference.
    legacy_observation = _observation_for_surface(legacy.observations, surface=surface)
    ledger_observation = _observation_for_surface(ledger.observations, surface=surface)
    legacy_available = (
        legacy_observation is not None and legacy_observation.status == "available"
    )
    ledger_available = (
        ledger_observation is not None and ledger_observation.status == "available"
    )
    if legacy_available != ledger_available:
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
        differences=tuple(differences),
    )


class LedgerGovernanceReader(Protocol):
    """Resolve one exact immutable governed season/cutoff authority."""

    def read_for_composition(self, season: str, cutoff: datetime) -> Any: ...


class MatchupParityRunner:
    """Run one bounded dual-run against the immutable governed authority.

    The runner resolves the exact governed team roster and game sets from the
    injected governance reader -- the checksummed immutable Event Catalog
    publication bound to the active manifest, never the mutable stored event
    table -- and compares two independently produced materializations without
    reading or advancing a ``PublicationPointer``.  Optional ``publications``
    record per-stream parity artifacts bound to their exact publication and
    payload checksum.
    """

    def __init__(
        self,
        engine,
        *,
        governance: LedgerGovernanceReader,
        parity_repository=None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.engine = engine
        self.governance = governance
        from app.services.ledger_parity import LedgerParityArtifactRepository

        self._parity_repository = parity_repository or LedgerParityArtifactRepository(
            engine, clock=clock
        )

    def run(
        self,
        legacy: MatchupMaterialization,
        ledger: MatchupMaterialization,
        *,
        publications: Mapping[str, tuple[str, str]] | None = None,
    ) -> tuple[MatchupParityReport, ...]:
        """Compare every ledger-owned surface and optionally record artifacts.

        ``publications`` maps a stream key to its ``(publication_id,
        payload_checksum)``.  Only a stream key present in the mapping records
        an artifact, so a caller can run one surface independently.
        """

        if legacy.season != ledger.season:
            raise ValueError("materializations describe different seasons")
        if legacy.window != ledger.window:
            raise ValueError("materializations describe different windows")
        if legacy.cutoff != ledger.cutoff:
            raise ValueError("materializations describe different cutoffs")
        governance = self.governance.read_for_composition(legacy.season, legacy.cutoff)
        expected_team_ids = frozenset(int(team_id) for team_id in governance.team_ids)
        expected_game_ids_by_team = (
            governance.expected_l15_game_ids
            if legacy.window == "l15"
            else governance.expected_season_game_ids
        )
        reports = tuple(
            compare_matchup_materializations(
                legacy,
                ledger,
                surface=surface,
                expected_team_ids=expected_team_ids,
                expected_game_ids_by_team=expected_game_ids_by_team,
            )
            for surface in LEDGER_OWNED_SURFACES
        )
        if publications:
            for report in reports:
                stream_key = matchup_stream_key(report.surface, report.window)
                publication = publications.get(stream_key)
                if publication is None:
                    continue
                publication_id, payload_checksum = publication
                self._parity_repository.record_matchup_parity(
                    stream_key,
                    cutoff=report.cutoff,
                    report=report,
                    publication_id=publication_id,
                    payload_checksum=payload_checksum,
                )
        return reports


def _per48(raw_value: float | None, minutes: float | None) -> float | None:
    if raw_value is None or minutes is None:
        return None
    if minutes <= 0 or not math.isfinite(float(raw_value)) or not math.isfinite(float(minutes)):
        return None
    return float(raw_value) * _PER48 / minutes


__all__ = [
    "CLASSIFICATION_AVAILABILITY_DIFFERENCE",
    "CLASSIFICATION_CUTOFF_MISMATCH",
    "CLASSIFICATION_DENOMINATOR_TOLERANCE_EXCEEDED",
    "CLASSIFICATION_DERIVED_RATE_DIFFERENCE",
    "CLASSIFICATION_GAME_SET_MISMATCH",
    "CLASSIFICATION_INTEGER_COUNT_DIFFERENCE",
    "CLASSIFICATION_LEAGUE_INCOMPLETE",
    "CLASSIFICATION_MISSING_LEGACY_TEAM",
    "CLASSIFICATION_MISSING_LEDGER_TEAM",
    "CLASSIFICATION_MISSING_METRIC",
    "CLASSIFICATION_MISSING_SURFACE",
    "CLASSIFICATION_NON_INTEGER_COUNT",
    "CLASSIFICATION_RANKING_DIFFERENCE",
    "LEDGER_OWNED_SURFACES",
    "LedgerGovernanceReader",
    "MATCHUP_PARITY_TOLERANCE",
    "MatchupMaterialization",
    "MatchupParityDifference",
    "MatchupParityReport",
    "MatchupParityRunner",
    "compare_matchup_materializations",
    "matchup_stream_key",
    "matchup_surface_stream_keys",
]
