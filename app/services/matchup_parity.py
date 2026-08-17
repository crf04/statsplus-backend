"""Bounded dual-run parity between legacy and ledger matchup materializers.

The ledger-owned Season and exact-L15 matchup materializer
(:class:`app.services.ledger_matchup_materialization.LedgerMatchupMaterializationService`)
and the legacy provider-aggregate writer
(:class:`app.services.team_matchup_refresh.TeamMatchupRefreshService`) both
populate the disposable ``team_matchup_facts`` read model at a shared season
and cutoff.  This module owns the comparison that lets an operator prove the
two materializers selected the same governed teams and games and produced the
same contracted facts before the legacy writer is fenced and the ledger stream
is activated.

The comparison is deliberately bounded to the two ledger-owned non-shot
surfaces -- traditional opponent counts and assist locations.  NBA-owned shot
zones, grouped shot types, and Synergy play types are composed from governed
publications, never from the ledger, so they have no legacy-vs-ledger dual-run
and are excluded here.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.services.ledger_derivations import (
    MATCHUP_ASSIST_KEYS,
    MATCHUP_TRADITIONAL_KEYS,
    competition_ranks,
)
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

#: Ledger-owned surfaces that participate in the dual-run.  Shot zones,
#: grouped shot types, and Synergy play types are NBA-owned and excluded.
LEDGER_OWNED_SURFACES = ("traditional", "assist_locations")

#: Legacy providers whose facts are the "legacy materializer" side.
_LEGACY_PROVIDERS = frozenset({"nba_stats", "pbp_stats"})

_LEDGER_PROVIDER = "ledger"

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
    """Durable evidence for one Season or L15 dual-run window."""

    season: str
    window: str
    as_of: date
    tolerance: float
    expected_team_ids: frozenset[int]
    league_complete: bool
    team_identities_exact: bool
    game_sets_exact: bool
    cutoffs_aligned: bool
    rankings_deterministic: bool
    surface_availability: Mapping[str, tuple[str | None, str | None]]
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
            "as_of": self.as_of.isoformat(),
            "tolerance": self.tolerance,
            "expected_team_ids": sorted(int(team_id) for team_id in self.expected_team_ids),
            "league_complete": self.league_complete,
            "team_identities_exact": self.team_identities_exact,
            "game_sets_exact": self.game_sets_exact,
            "cutoffs_aligned": self.cutoffs_aligned,
            "rankings_deterministic": self.rankings_deterministic,
            "surface_availability": {
                surface: list(statuses)
                for surface, statuses in self.surface_availability.items()
            },
            "compared_count": self.compared_count,
            "differences": [difference.to_dict() for difference in self.differences],
        }


def _json_value(value: object) -> object:
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, (tuple, frozenset, set, list)):
        return sorted(str(item) for item in value)
    return value


def _facts_by_surface(
    facts: Iterable[TeamMatchupFact],
    *,
    provider: str,
    providers: frozenset[str],
) -> dict[str, dict[tuple[int, str, str], TeamMatchupFact]]:
    """Index facts by surface and (team, slice, stat) for one side."""

    indexed: dict[str, dict[tuple[int, str, str], TeamMatchupFact]] = defaultdict(dict)
    for fact in facts:
        if provider is not None and fact.provider != provider:
            continue
        if provider is None and fact.provider not in providers:
            continue
        if fact.base not in LEDGER_OWNED_SURFACES:
            continue
        key = (int(fact.team_id), str(fact.slice_key), str(fact.stat_key))
        indexed[fact.base][key] = fact
    return dict(indexed)


def _observations_by_surface(
    observations: Iterable[TeamMatchupObservation],
) -> dict[str, TeamMatchupObservation]:
    return {
        str(observation.surface): observation
        for observation in observations
        if str(observation.surface) in LEDGER_OWNED_SURFACES
    }


def _team_ids_from_facts(facts_by_surface: Mapping[str, Mapping[tuple[int, str, str], TeamMatchupFact]]) -> set[int]:
    return {
        int(key[0])
        for surface_facts in facts_by_surface.values()
        for key in surface_facts
    }


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


def compare_matchup_materializations(
    legacy_facts: Iterable[TeamMatchupFact],
    legacy_observations: Iterable[TeamMatchupObservation],
    ledger_facts: Iterable[TeamMatchupFact],
    ledger_observations: Iterable[TeamMatchupObservation],
    *,
    season: str,
    window: str,
    as_of: date,
    legacy_as_of: date,
    expected_team_ids: Iterable[int],
    legacy_game_ids_by_team: Mapping[int, Iterable[str]],
    tolerance: float = MATCHUP_PARITY_TOLERANCE,
) -> MatchupParityReport:
    """Compare the two materializers at one shared season and cutoff.

    ``legacy_facts``/``legacy_observations`` are the provider-aggregate
    materializer output; ``ledger_facts``/``ledger_observations`` are the
    ledger materializer output.  ``legacy_game_ids_by_team`` is the legacy
    materializer's exact per-team selected game set, which provider-collected
    facts do not otherwise persist.  Integer counts must be exactly equal;
    floating denominators and derived per-48 rates use ``tolerance``.  Every
    difference is classified, and any difference makes the report require
    adjudication.
    """

    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be finite and non-negative")
    if window not in {"season", "l15"}:
        raise ValueError("window must be 'season' or 'l15'")

    expected = frozenset(int(team_id) for team_id in expected_team_ids)
    if len(expected) != 30:
        raise ValueError("expected_team_ids must name exactly 30 teams")

    legacy_by_surface = _facts_by_surface(legacy_facts, provider=None, providers=_LEGACY_PROVIDERS)
    ledger_by_surface = _facts_by_surface(ledger_facts, provider=_LEDGER_PROVIDER, providers=frozenset())
    legacy_obs = _observations_by_surface(legacy_observations)
    ledger_obs = _observations_by_surface(ledger_observations)

    differences: list[MatchupParityDifference] = []
    compared = 0

    # Team identity coverage: both materializers must cover the exact governed
    # 30-team roster (League Complete) with no asymmetric team.
    legacy_teams = _team_ids_from_facts(legacy_by_surface)
    ledger_teams = _team_ids_from_facts(ledger_by_surface)
    league_complete = legacy_teams == expected and ledger_teams == expected
    team_identities_exact = legacy_teams == ledger_teams
    if not league_complete:
        differences.append(MatchupParityDifference(
            window, None, None, "team_coverage",
            sorted(int(team_id) for team_id in ledger_teams),
            sorted(int(team_id) for team_id in legacy_teams),
            CLASSIFICATION_LEAGUE_INCOMPLETE,
        ))
    for team_id in sorted(legacy_teams - ledger_teams):
        differences.append(MatchupParityDifference(
            window, None, team_id, "identity", None, "present",
            CLASSIFICATION_MISSING_LEDGER_TEAM,
        ))
    for team_id in sorted(ledger_teams - legacy_teams):
        differences.append(MatchupParityDifference(
            window, None, team_id, "identity", "present", None,
            CLASSIFICATION_MISSING_LEGACY_TEAM,
        ))

    # Cutoff alignment: both materializers must describe the same Eastern
    # as-of date.
    cutoffs_aligned = legacy_as_of == as_of
    if not cutoffs_aligned:
        differences.append(MatchupParityDifference(
            window, None, None, "as_of", as_of, legacy_as_of,
            CLASSIFICATION_CUTOFF_MISMATCH,
        ))

    # Exact game sets: the ledger's persisted per-team game IDs must equal the
    # legacy resolver's exact selection for the same team.
    game_set_checks: dict[int, frozenset[str]] = {
        int(team_id): frozenset(str(game_id) for game_id in game_ids)
        for team_id, game_ids in legacy_game_ids_by_team.items()
    }
    ledger_game_ids_by_team: dict[int, frozenset[str]] = {}
    game_sets_exact = True
    for team_id in sorted(ledger_teams & set(game_set_checks)):
        stored_ids = _ledger_game_ids_for_team(
            team_id, ledger_by_surface, ledger_obs
        )
        ledger_game_ids_by_team[team_id] = stored_ids
        expected_ids = game_set_checks.get(team_id, frozenset())
        if stored_ids != expected_ids:
            game_sets_exact = False
            differences.append(MatchupParityDifference(
                window, None, team_id, "game_ids",
                sorted(stored_ids), sorted(expected_ids),
                CLASSIFICATION_GAME_SET_MISMATCH,
            ))
    for team_id in sorted(ledger_teams - set(game_set_checks)):
        game_sets_exact = False
        differences.append(MatchupParityDifference(
            window, None, team_id, "game_ids",
            sorted(ledger_game_ids_by_team.get(team_id, frozenset())),
            None,
            CLASSIFICATION_GAME_SET_MISMATCH,
        ))

    # Per-surface facts: exact integer counts, tolerant denominators, and
    # tolerant derived per-48 rates for every shared (team, slice, stat).
    count_keys: dict[str, frozenset[str]] = {
        "traditional": frozenset(MATCHUP_TRADITIONAL_KEYS),
        "assist_locations": frozenset(MATCHUP_ASSIST_KEYS),
    }
    rates_by_side: dict[str, dict[str, dict[int, float]]] = {
        "ledger": {"traditional": {}, "assist_locations": {}},
        "legacy": {"traditional": {}, "assist_locations": {}},
    }
    for surface in LEDGER_OWNED_SURFACES:
        legacy_surface = legacy_by_surface.get(surface, {})
        ledger_surface = ledger_by_surface.get(surface, {})
        if not legacy_surface and not ledger_surface:
            continue
        if not legacy_surface or not ledger_surface:
            differences.append(MatchupParityDifference(
                window, surface, None, "surface",
                "present" if ledger_surface else None,
                "present" if legacy_surface else None,
                CLASSIFICATION_MISSING_SURFACE,
            ))
            continue
        shared = set(ledger_surface) & set(legacy_surface)
        for key in sorted(ledger_surface.keys() - legacy_surface.keys()):
            differences.append(MatchupParityDifference(
                window, surface, key[0], key[2], key[1], None,
                CLASSIFICATION_MISSING_LEGACY_TEAM,
            ))
        for key in sorted(legacy_surface.keys() - ledger_surface.keys()):
            differences.append(MatchupParityDifference(
                window, surface, key[0], key[2], None, key[1],
                CLASSIFICATION_MISSING_LEDGER_TEAM,
            ))
        for key in sorted(shared):
            ledger_fact = ledger_surface[key]
            legacy_fact = legacy_surface[key]
            compared += 1
            if key[2] in count_keys[surface]:
                ledger_count = _integral_or_none(ledger_fact.raw_value)
                legacy_count = _integral_or_none(legacy_fact.raw_value)
                if ledger_count is None or legacy_count is None:
                    differences.append(MatchupParityDifference(
                        window, surface, key[0], key[2],
                        ledger_fact.raw_value, legacy_fact.raw_value,
                        CLASSIFICATION_NON_INTEGER_COUNT,
                    ))
                elif ledger_count != legacy_count:
                    differences.append(MatchupParityDifference(
                        window, surface, key[0], key[2],
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
                    window, surface, key[0], "denominator_minutes",
                    ledger_minutes, legacy_minutes,
                    CLASSIFICATION_DENOMINATOR_TOLERANCE_EXCEEDED,
                ))
            ledger_rate = _per48(ledger_fact.raw_value, ledger_minutes)
            legacy_rate = _per48(legacy_fact.raw_value, legacy_minutes)
            if ledger_rate is not None and legacy_rate is not None and not math.isclose(
                ledger_rate, legacy_rate, rel_tol=tolerance, abs_tol=tolerance
            ):
                differences.append(MatchupParityDifference(
                    window, surface, key[0], f"per48.{key[2]}",
                    ledger_rate, legacy_rate,
                    CLASSIFICATION_DERIVED_RATE_DIFFERENCE,
                ))
            if ledger_rate is not None:
                rates_by_side["ledger"][surface].setdefault(key[2], {})[key[0]] = ledger_rate
            if legacy_rate is not None:
                rates_by_side["legacy"][surface].setdefault(key[2], {})[key[0]] = legacy_rate

    # Deterministic rankings: both sides derive the same competition ranks
    # (ascending, ties 1, 1, 3) from their per-48 values.
    rankings_deterministic = True
    for surface in LEDGER_OWNED_SURFACES:
        ledger_metrics = rates_by_side["ledger"][surface]
        legacy_metrics = rates_by_side["legacy"][surface]
        for metric in sorted(set(ledger_metrics) & set(legacy_metrics)):
            ledger_ranks = competition_ranks(ledger_metrics[metric], descending=False)
            legacy_ranks = competition_ranks(legacy_metrics[metric], descending=False)
            if ledger_ranks != legacy_ranks:
                rankings_deterministic = False
                differences.append(MatchupParityDifference(
                    window, surface, None, f"rank.{metric}",
                    ledger_ranks, legacy_ranks,
                    CLASSIFICATION_RANKING_DIFFERENCE,
                ))

    # Per-surface availability: both materializers must report the same
    # availability status for each ledger-owned surface.
    availability: dict[str, tuple[str | None, str | None]] = {}
    for surface in LEDGER_OWNED_SURFACES:
        legacy_status = legacy_obs.get(surface).status if legacy_obs.get(surface) else None
        ledger_status = ledger_obs.get(surface).status if ledger_obs.get(surface) else None
        availability[surface] = (legacy_status, ledger_status)
        if legacy_status != ledger_status:
            differences.append(MatchupParityDifference(
                window, surface, None, "availability",
                ledger_status, legacy_status,
                CLASSIFICATION_AVAILABILITY_DIFFERENCE,
            ))

    return MatchupParityReport(
        season=season,
        window=window,
        as_of=as_of,
        tolerance=tolerance,
        expected_team_ids=expected,
        league_complete=league_complete,
        team_identities_exact=team_identities_exact,
        game_sets_exact=game_sets_exact,
        cutoffs_aligned=cutoffs_aligned,
        rankings_deterministic=rankings_deterministic,
        surface_availability=availability,
        compared_count=compared,
        differences=tuple(differences),
    )


def _ledger_game_ids_for_team(
    team_id: int,
    ledger_by_surface: Mapping[str, Mapping[tuple[int, str, str], TeamMatchupFact]],
    ledger_obs: Mapping[str, TeamMatchupObservation],
) -> frozenset[str]:
    """Recover the ledger's exact selected game IDs for one team."""

    for surface in ("traditional", "assist_locations"):
        surface_facts = ledger_by_surface.get(surface, {})
        for key, fact in surface_facts.items():
            if key[0] == team_id and fact.game_ids:
                return frozenset(fact.game_ids)
    for surface in LEDGER_OWNED_SURFACES:
        observation = ledger_obs.get(surface)
        if observation is not None and observation.game_ids:
            return frozenset(observation.game_ids)
    return frozenset()


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
    "CLASSIFICATION_MISSING_SURFACE",
    "CLASSIFICATION_NON_INTEGER_COUNT",
    "CLASSIFICATION_RANKING_DIFFERENCE",
    "LEDGER_OWNED_SURFACES",
    "MATCHUP_PARITY_TOLERANCE",
    "MatchupParityDifference",
    "MatchupParityReport",
    "compare_matchup_materializations",
]
