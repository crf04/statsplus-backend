"""Derived matchup metrics over the stored raw 30-team facts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from math import isfinite
from statistics import fmean, pstdev
from zoneinfo import ZoneInfo

from app.domain.utc import assume_utc
from app.services.team_matchup_repository import (
    StoredTeamMatchupFact,
    StoredTeamMatchupObservation,
    TeamMatchupRepository,
    TeamMatchupSnapshotScope,
)


EASTERN = ZoneInfo("America/New_York")


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
    percent_vs_league_average: float | None
    sigma_deviation: float
    rank: int


@dataclass(frozen=True, slots=True)
class TeamMatchupWindow:
    scope: TeamMatchupSnapshotScope
    fact_scopes: dict[str, TeamMatchupSnapshotScope]
    fact_retrieved_at: dict[str, datetime]
    league_metrics: tuple[LeagueMatchupMetric, ...]
    team_metrics: dict[int, tuple[TeamMatchupMetric, ...]]
    observations: tuple[StoredTeamMatchupObservation, ...]


class TeamMatchupQueryService:
    """Calculate league denominators and team comparisons from raw facts."""

    def __init__(
        self,
        repository: TeamMatchupRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def get_latest_window(
        self,
        season: str,
        *,
        window_games: int | None = None,
        as_of: date | None = None,
    ) -> TeamMatchupWindow | None:
        """Read the newest stored window on or before an optional slate date."""

        current_date = assume_utc(self._clock()).astimezone(EASTERN).date()
        if as_of is not None and as_of > current_date:
            raise ValueError("future as_of dates cannot be queried")
        cutoff = as_of or current_date
        observation_scope = self.repository.get_latest_scope(
            season, window_games=window_games, as_of=cutoff
        )
        if observation_scope is None:
            return None
        observation_snapshot = self.repository.get_snapshot(observation_scope)
        observations = observation_snapshot.observations
        fact_scopes = self.repository.get_latest_fact_scopes(
            season,
            window_games=window_games,
            as_of=cutoff,
        )
        surfaces_by_scope = defaultdict(set)
        for surface, fact_scope in fact_scopes.items():
            surfaces_by_scope[fact_scope].add(surface)
        snapshots_by_scope = {observation_scope: observation_snapshot}
        facts = []
        for fact_scope, surfaces in surfaces_by_scope.items():
            if fact_scope not in snapshots_by_scope:
                snapshots_by_scope[fact_scope] = self.repository.get_snapshot(
                    fact_scope
                )
            facts.extend(
                fact
                for fact in snapshots_by_scope[fact_scope].facts
                if fact.base in surfaces
            )
        return self._build_window(
            observation_scope,
            fact_scopes=fact_scopes,
            facts=facts,
            observations=observations,
        )

    def get_window(self, scope: TeamMatchupSnapshotScope) -> TeamMatchupWindow:
        snapshot = self.repository.get_snapshot(scope)
        return self._build_window(
            scope,
            fact_scopes={fact.base: scope for fact in snapshot.facts},
            facts=snapshot.facts,
            observations=snapshot.observations,
        )

    def _build_window(
        self,
        scope: TeamMatchupSnapshotScope,
        *,
        fact_scopes: dict[str, TeamMatchupSnapshotScope],
        facts: Iterable[StoredTeamMatchupFact],
        observations: Iterable[StoredTeamMatchupObservation],
    ) -> TeamMatchupWindow:
        grouped = defaultdict(list)
        invalid_surfaces: dict[str, str] = {}
        fact_rows = tuple(facts)
        for fact in fact_rows:
            try:
                value = self._allowed_per_48(
                    fact.raw_value, fact.denominator_value, fact.denominator_unit
                )
            except ValueError:
                invalid_surfaces[fact.base] = "provider_invalid_numeric"
                continue
            grouped[(fact.base, fact.slice_key, fact.stat_key)].append(
                (fact.team_id, value)
            )

        for key, team_values in grouped.items():
            if (
                len(team_values) != 30
                or len({team_id for team_id, _ in team_values}) != 30
            ):
                invalid_surfaces[key[0]] = "legacy_surface_incomplete"

        league_metrics = []
        team_metrics = defaultdict(list)
        for key in sorted(grouped):
            if key[0] in invalid_surfaces:
                continue
            team_values = grouped[key]
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
                        percent_vs_league_average=(
                            (value / average - 1) * 100 if average else None
                        ),
                        sigma_deviation=(value - average) / sigma if sigma else 0.0,
                        rank=ranks[value],
                    )
                )
        observations_by_surface = {
            observation.surface: observation for observation in observations
        }
        for surface, reason in invalid_surfaces.items():
            observation = observations_by_surface.get(surface)
            if observation is None:
                retrieved_at = max(
                    fact.retrieved_at
                    for fact in fact_rows
                    if fact.base == surface
                )
                observations_by_surface[surface] = StoredTeamMatchupObservation(
                    surface=surface,
                    status="unavailable",
                    unavailable_reason=reason,
                    retrieved_at=retrieved_at,
                )
            else:
                observations_by_surface[surface] = replace(
                    observation,
                    status="unavailable",
                    unavailable_reason=reason,
                )
        return TeamMatchupWindow(
            scope=scope,
            fact_scopes={
                surface: fact_scope
                for surface, fact_scope in fact_scopes.items()
                if surface not in invalid_surfaces
            },
            fact_retrieved_at={
                surface: max(
                    fact.retrieved_at for fact in fact_rows if fact.base == surface
                )
                for surface in fact_scopes
                if surface not in invalid_surfaces
            },
            league_metrics=tuple(league_metrics),
            team_metrics={
                team_id: tuple(metrics)
                for team_id, metrics in sorted(team_metrics.items())
            },
            observations=tuple(
                observations_by_surface[surface]
                for surface in sorted(observations_by_surface)
            ),
        )

    @staticmethod
    def _allowed_per_48(raw, denominator, denominator_unit) -> float:
        try:
            raw_value = float(raw)
            denominator_value = float(denominator)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(
                "an available team matchup fact needs finite numeric values"
            ) from error
        if not isfinite(raw_value) or not isfinite(denominator_value):
            raise ValueError(
                "an available team matchup fact needs finite numeric values"
            )
        if denominator_value <= 0:
            raise ValueError(
                "an available team matchup fact needs a positive denominator"
            )
        if denominator_unit == "minutes":
            return raw_value * 48 / denominator_value
        if denominator_unit == "seconds":
            return raw_value * 48 * 60 / denominator_value
        raise ValueError(
            "an available team matchup fact has an unknown denominator unit"
        )


__all__ = [
    "LeagueMatchupMetric",
    "TeamMatchupMetric",
    "TeamMatchupQueryService",
    "TeamMatchupWindow",
]
