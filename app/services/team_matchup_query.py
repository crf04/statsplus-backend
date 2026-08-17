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
from app.services.database_first_activation import (
    PublicationPayloadError,
    PublicationRead,
    decode_team_window,
)
from app.services.team_matchup_publications import (
    NBA_PUBLICATION_BASES,
    NBA_PUBLICATION_STREAMS,
    PublicationLineage,
    publication_cutoff_reason,
    publication_lineage,
    publication_metric_identity,
    publication_metric_keys,
    validate_publication_rows,
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
        publication_reader=None,
    ) -> None:
        self.repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._publication_reader = publication_reader

    def get_latest_window(
        self,
        season: str,
        *,
        window_games: int | None = None,
        as_of: date | None = None,
        publication_snapshot=None,
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
            return self._database_first_window(
                season,
                cutoff=cutoff,
                window_games=window_games,
                legacy=None,
                publication_snapshot=publication_snapshot,
            )
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
        legacy_window = self._build_window(
            observation_scope,
            fact_scopes=fact_scopes,
            facts=facts,
            observations=observations,
        )
        expected_l15_game_ids = self._expected_l15_game_ids(facts)
        return self._database_first_window(
            season,
            cutoff=cutoff,
            window_games=window_games,
            legacy=legacy_window,
            publication_snapshot=publication_snapshot,
            expected_l15_game_ids=expected_l15_game_ids,
        )

    def get_window(
        self, scope: TeamMatchupSnapshotScope, *, publication_snapshot=None
    ) -> TeamMatchupWindow:
        snapshot = self.repository.get_snapshot(scope)
        legacy_window = self._build_window(
            scope,
            fact_scopes={fact.base: scope for fact in snapshot.facts},
            facts=snapshot.facts,
            observations=snapshot.observations,
        )
        expected_l15_game_ids = self._expected_l15_game_ids(snapshot.facts)
        return self._database_first_window(
            scope.season,
            cutoff=scope.as_of,
            window_games=scope.window_games,
            legacy=legacy_window,
            publication_snapshot=publication_snapshot,
            expected_l15_game_ids=expected_l15_game_ids,
        )

    def _database_first_window(
        self,
        season: str,
        *,
        cutoff: date,
        window_games: int | None,
        legacy: TeamMatchupWindow | None,
        publication_snapshot=None,
        expected_l15_game_ids=None,
    ) -> TeamMatchupWindow | None:
        """Overlay only activated windows; inactive bases remain legacy-backed."""

        if self._publication_reader is None:
            return legacy
        window = "l15" if window_games is not None else "season"
        stream_by_base = {
            "traditional": f"traditional_opponent_{window}",
            "assist_locations": f"assist_locations_{window}",
        }
        stream_by_base.update({
            base: NBA_PUBLICATION_STREAMS[base].format(window=window)
            for base in NBA_PUBLICATION_STREAMS
        })
        if publication_snapshot is not None:
            publication_reads = {
                stream_key: publication_snapshot.read(stream_key)
                for stream_key in stream_by_base.values()
            }
        else:
            read_many = getattr(self._publication_reader, "read_many", None)
            publication_reads = (
                read_many(tuple(stream_by_base.values()), season=season)
                if callable(read_many)
                else {
                    stream_key: self._publication_reader.read(
                        stream_key, season=season
                    )
                    for stream_key in stream_by_base.values()
                }
            )
        reads = {
            base: publication_reads.get(
                stream_key,
                PublicationRead(
                    stream_key=stream_key,
                    publication_id=None,
                    season=season,
                    cutoff=None,
                    version=None,
                    status="missing",
                    freshness="missing",
                    age_seconds=None,
                    payload=None,
                ),
            )
            for base, stream_key in stream_by_base.items()
        }
        active = {
            base: read
            for base, read in reads.items()
            # NBA-owned shot/play surfaces are never allowed to borrow a
            # legacy PBP value, even if a generic reader labels an inactive
            # stream as legacy-fallback eligible.  Only ledger-owned
            # traditional/assist surfaces retain that compatibility path.
            if base in NBA_PUBLICATION_BASES or not read.legacy_fallback_allowed
        }
        if not active:
            return legacy
        base_windows: dict[str, TeamMatchupWindow | None] = {}
        for base, read in active.items():
            cutoff_reason = publication_cutoff_reason(read, cutoff)
            if cutoff_reason is not None:
                read = replace(
                    read,
                    status="unavailable",
                    unavailable_reason=cutoff_reason,
                )
            if not read.available:
                base_windows[base] = None
                continue
            try:
                rows = (
                    tuple(read.decoded)
                    if read.decoded is not None
                    else decode_team_window(
                        read.payload, stream_key=stream_by_base[base]
                    )
                    )
                if base in NBA_PUBLICATION_BASES:
                    rows = tuple(rows)
                    validate_publication_rows(
                        base,
                        rows,
                        expected_l15_game_ids=(
                            expected_l15_game_ids if window_games is not None else None
                        ),
                    )
            except PublicationPayloadError:
                base_windows[base] = None
                continue
            except ValueError:
                base_windows[base] = None
                continue
            base_windows[base] = self._publication_base_window(
                season,
                cutoff=cutoff,
                window_games=window_games,
                base=base,
                rows=rows,
                retrieved_at=read.retrieved_at or self._clock(),
                publication=publication_lineage(read),
            )
        scope = legacy.scope if legacy is not None else TeamMatchupSnapshotScope(
            season=season, as_of=cutoff, window_games=window_games
        )
        legacy_league = () if legacy is None else legacy.league_metrics
        legacy_team = {} if legacy is None else legacy.team_metrics
        legacy_observations = () if legacy is None else legacy.observations
        legacy_fact_scopes = {} if legacy is None else legacy.fact_scopes
        legacy_retrieved = {} if legacy is None else legacy.fact_retrieved_at
        league = [metric for metric in legacy_league if metric.base not in active]
        team_metrics = {
            team_id: [metric for metric in metrics if metric.base not in active]
            for team_id, metrics in legacy_team.items()
        }
        observations = [
            observation
            for observation in legacy_observations
            if observation.surface not in active
        ]
        fact_scopes = {
            base: fact_scope
            for base, fact_scope in legacy_fact_scopes.items()
            if base not in active
        }
        fact_retrieved = {
            base: retrieved
            for base, retrieved in legacy_retrieved.items()
            if base not in active
        }
        for base, read in active.items():
            window = base_windows[base]
            if window is None:
                observations.append(StoredTeamMatchupObservation(
                    surface=base,
                    status="unavailable" if read.status == "unavailable" else "missing",
                    unavailable_reason=(
                        read.unavailable_reason or f"publication_{read.status}"
                    ),
                    retrieved_at=read.retrieved_at or self._clock(),
                    publication=publication_lineage(read),
                ))
                continue
            league.extend(window.league_metrics)
            for team_id, metrics in window.team_metrics.items():
                team_metrics.setdefault(team_id, []).extend(metrics)
            observations.extend(window.observations)
            fact_scopes[base] = window.scope
            fact_retrieved[base] = next(iter(window.fact_retrieved_at.values()))
        if legacy is None and not league and not observations:
            return None
        return TeamMatchupWindow(
            scope=scope,
            fact_scopes=fact_scopes,
            fact_retrieved_at=fact_retrieved,
            league_metrics=tuple(league),
            team_metrics={
                team_id: tuple(metrics)
                for team_id, metrics in sorted(team_metrics.items())
            },
            observations=tuple(sorted(observations, key=lambda item: item.surface)),
        )

    @staticmethod
    def _publication_base_window(
        season: str,
        *,
        cutoff: date,
        window_games: int | None,
        base: str,
        rows,
        retrieved_at: datetime,
        publication: PublicationLineage | None = None,
    ) -> TeamMatchupWindow:
        stat_names = {
            "traditional": {
                "OPP_REB": "rebounds",
                "OPP_TOV": "turnovers",
                "OPP_STL": "steals",
                "OPP_BLK": "blocks",
            },
            "assist_locations": {
                "Assists": "assists",
                "Arc3Assists": "arc3_assists",
                "Corner3Assists": "corner3_assists",
                "AtRimAssists": "at_rim_assists",
                "ShortMidRangeAssists": "short_mid_range_assists",
                "LongMidRangeAssists": "long_mid_range_assists",
            },
        }.get(base)
        if stat_names is None:
            # Grouped shot/zone/Synergy publications carry their complete
            # identity in the metric key (for example
            # ``Isolation_PTS``).  Do not substitute the legacy surface when
            # one of those streams is active; project exactly the keys the
            # immutable payload supplied.
            keys = publication_metric_keys(base)
            identities = tuple(
                (
                    *publication_metric_identity(base, key),
                    key,
                )
                for key in keys
            )
        else:
            identities = tuple(
                (display_key, display_key, metric_key)
                for display_key, metric_key in stat_names.items()
            )
        league_by_key = rows[0].league_average
        sigma_by_key = rows[0].population_sigma
        league_metrics = []
        for slice_key, stat_key, metric_key in identities:
            if metric_key not in league_by_key:
                continue
            league_metrics.append(LeagueMatchupMetric(
                base=base,
                slice_key=slice_key,
                stat_key=stat_key,
                average_allowed_per_48=league_by_key[metric_key],
                sigma=sigma_by_key[metric_key],
                team_count=len(rows),
            ))
        team_metrics = defaultdict(list)
        for row in rows:
            for slice_key, stat_key, metric_key in identities:
                if metric_key not in row.per48:
                    continue
                average = row.league_average[metric_key]
                value = row.per48[metric_key]
                team_metrics[row.team_id].append(TeamMatchupMetric(
                    base=base,
                    slice_key=slice_key,
                    stat_key=stat_key,
                    allowed_per_48=value,
                    percent_vs_league_average=(
                        (value / average - 1) * 100 if average else None
                    ),
                    sigma_deviation=(
                        (value - average) / row.population_sigma[metric_key]
                        if row.population_sigma[metric_key] else 0.0
                    ),
                    rank=row.competition_rank[metric_key],
                ))
        scope = TeamMatchupSnapshotScope(
            season=season, as_of=cutoff, window_games=window_games
        )
        observation = StoredTeamMatchupObservation(
            surface=base,
            status="available" if league_metrics and team_metrics else "unavailable",
            unavailable_reason=None if league_metrics and team_metrics else "publication_surface_incomplete",
            retrieved_at=retrieved_at,
            game_ids=tuple(sorted({
                game_id for row in rows for game_id in row.game_ids
            })),
            publication=publication,
        )
        return TeamMatchupWindow(
            scope=scope,
            fact_scopes={base: scope},
            fact_retrieved_at={base: retrieved_at},
            league_metrics=tuple(league_metrics),
            team_metrics={team_id: tuple(metrics) for team_id, metrics in team_metrics.items()},
            observations=(observation,),
        )

    @staticmethod
    def _expected_l15_game_ids(facts):
        expected = {}
        for fact in facts:
            if fact.base == "traditional" and fact.game_ids:
                expected[fact.team_id] = frozenset(fact.game_ids)
        return expected

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
        observation_rows = tuple(observations)
        observations_by_surface = {
            observation.surface: observation for observation in observation_rows
        }
        unavailable_nba_surfaces = {
            surface
            for surface in NBA_PUBLICATION_BASES
            if (
                (observation := observations_by_surface.get(surface)) is not None
                and observation.status != "available"
                and (
                    observation.publication is not None
                )
            )
        }
        fact_rows = tuple(
            fact for fact in fact_rows if fact.base not in unavailable_nba_surfaces
        )
        for surface in unavailable_nba_surfaces:
            observation = observations_by_surface[surface]
            invalid_surfaces[surface] = (
                observation.unavailable_reason or "surface_unavailable"
            )
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
                key[0] not in invalid_surfaces
                and (
                    len(team_values) != 30
                    or len({team_id for team_id, _ in team_values}) != 30
                )
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
