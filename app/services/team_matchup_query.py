"""Derived matchup metrics over the stored raw 30-team facts."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import fmean, pstdev

from app.services.team_matchup_repository import (
    StoredTeamMatchupObservation,
    TeamMatchupRepository,
    TeamMatchupSnapshotScope,
)


@dataclass(frozen=True, slots=True)
class LeagueMatchupMetric:
    base: str
    slice_key: str
    stat_key: str
    average_allowed_per_48: float
    sigma: float
    team_count: int


@dataclass(frozen=True, slots=True)
class TeamMatchupMetric:
    base: str
    slice_key: str
    stat_key: str
    allowed_per_48: float
    percent_vs_league_average: float
    sigma_deviation: float
    rank: int


@dataclass(frozen=True, slots=True)
class TeamMatchupWindow:
    scope: TeamMatchupSnapshotScope
    league_metrics: tuple[LeagueMatchupMetric, ...]
    team_metrics: dict[int, tuple[TeamMatchupMetric, ...]]
    observations: tuple[StoredTeamMatchupObservation, ...]


class TeamMatchupQueryService:
    """Calculate league denominators and team comparisons from raw facts."""

    def __init__(self, repository: TeamMatchupRepository) -> None:
        self.repository = repository

    def get_window(self, scope: TeamMatchupSnapshotScope) -> TeamMatchupWindow:
        snapshot = self.repository.get_snapshot(scope)
        grouped = defaultdict(list)
        for fact in snapshot.facts:
            if fact.status != "available":
                continue
            value = self._allowed_per_48(
                fact.raw_value, fact.denominator_value, fact.denominator_unit
            )
            grouped[(fact.base, fact.slice_key, fact.stat_key)].append(
                (fact.team_id, value)
            )

        league_metrics = []
        team_metrics = defaultdict(list)
        for key in sorted(grouped):
            team_values = grouped[key]
            if (
                len(team_values) != 30
                or len({team_id for team_id, _ in team_values}) != 30
            ):
                raise ValueError(
                    "team matchup league metrics require exactly 30 distinct teams"
                )
            values = [value for _, value in team_values]
            average = fmean(values)
            sigma = pstdev(values)
            league_metrics.append(
                LeagueMatchupMetric(*key, average, sigma, len(team_values))
            )
            ranks = {}
            for index, value in enumerate(sorted(values)):
                ranks.setdefault(value, index + 1)
            for team_id, value in team_values:
                team_metrics[team_id].append(
                    TeamMatchupMetric(
                        *key,
                        allowed_per_48=value,
                        percent_vs_league_average=(value / average - 1) * 100,
                        sigma_deviation=(value - average) / sigma if sigma else 0.0,
                        rank=ranks[value],
                    )
                )
        return TeamMatchupWindow(
            scope=scope,
            league_metrics=tuple(league_metrics),
            team_metrics={
                team_id: tuple(metrics)
                for team_id, metrics in sorted(team_metrics.items())
            },
            observations=snapshot.observations,
        )

    @staticmethod
    def _allowed_per_48(raw, denominator, denominator_unit) -> float:
        if raw is None or denominator is None or denominator <= 0:
            raise ValueError(
                "an available team matchup fact needs a positive denominator"
            )
        if denominator_unit == "minutes":
            return float(raw) * 48 / float(denominator)
        if denominator_unit == "seconds":
            return float(raw) * 48 * 60 / float(denominator)
        if denominator_unit == "games":
            return float(raw) / float(denominator)
        raise ValueError(
            "an available team matchup fact has an unknown denominator unit"
        )


__all__ = [
    "LeagueMatchupMetric",
    "TeamMatchupMetric",
    "TeamMatchupQueryService",
    "TeamMatchupWindow",
]
