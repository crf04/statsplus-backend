"""Compose the matchup response exclusively from durable governed seams."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.config.settings import RuntimeSettings
from app.domain.freshness import (
    exact_age_seconds,
    exact_seconds,
    time_window_timedelta,
    within_max_age,
)
from app.domain.nba_events import (
    is_final_event,
    is_postponed_event,
    resolve_stored_event_classification,
)
from app.domain.utc import assume_utc, parse_utc_iso
from app.errors import ProviderUnavailableError, ResourceNotFoundError
from app.models.catalogs import PLAY_TYPES
from app.services.player_diet import (
    PLAYER_DIET_BASES,
    PlayerDietResult,
    StoredPlayerDietFact,
)
from app.services.player_game_log_repository import (
    PlayerGameLogReadFreshness,
    PlayerSeasonLogSummary,
)
from app.services.player_pool import PlayerPool, PoolPlayer
from app.services.matchup_injuries import (
    MatchupInjuryResult,
    unavailable_injury_result,
)
from app.services.slate_service import SlateService
from app.services.stats_freshness_repository import StatsFreshness
from app.services.team_matchup_query import (
    LeagueMatchupMetric,
    TeamMatchupMetric,
    TeamMatchupWindow,
)
from app.services.database_first_activation import DatabaseFirstPublicationReader


EASTERN = ZoneInfo("America/New_York")
DEFENSE_BASES = (
    "play_types",
    "shot_zones",
    "shot_types",
    "assist_locations",
    "traditional",
)
DEFENSIVE_COLUMNS = ("OPP_TOV", "OPP_STL", "OPP_BLK")
_REQUIRED_TRADITIONAL_IDENTITIES = frozenset(
    (key, key) for key in DEFENSIVE_COLUMNS
)
_WIRE_PRECISION = 6
_TWO_POINT_SHOT_ZONES = frozenset(
    {"Restricted Area", "Paint", "In The Paint (Non-RA)", "Mid-Range"}
)
_THREE_POINT_SHOT_ZONES = frozenset({"Corner 3", "Above the Break 3"})
_GOVERNED_SHOT_ZONES = frozenset(
    {
        "Restricted Area",
        "In The Paint (Non-RA)",
        "Mid-Range",
        "Corner 3",
        "Above the Break 3",
    }
)
_SHOT_TYPE_DISPLAY_SLICES = {
    "catch_and_shoot": "Catch and Shoot",
    "pullups": "Pullups",
    "less_than_10_ft": "Less Than 10 ft",
}
_SHOT_TYPE_STORED_SLICES = {
    display: stored for stored, display in _SHOT_TYPE_DISPLAY_SLICES.items()
}
_GOVERNED_SHOT_TYPES = frozenset(_SHOT_TYPE_DISPLAY_SLICES)
_STAT_MARKETS = {
    "PTS": ("PTS", "PA", "PR", "PRA"),
    "POSS": ("PTS",),
    "FGM": ("PTS",),
    "FGA": ("FGA", "FG2A", "FG3A"),
    "FG2M": ("PTS",),
    "FG2A": ("FGA", "FG2A"),
    "FG3M": ("3PM", "PTS"),
    "FG3A": ("FGA", "FG3A"),
    "Assists": ("AST", "PA", "RA", "PRA"),
    "Arc3Assists": ("AST", "PA", "RA", "PRA"),
    "Corner3Assists": ("AST", "PA", "RA", "PRA"),
    "AtRimAssists": ("AST", "PA", "RA", "PRA"),
    "ShortMidRangeAssists": ("AST", "PA", "RA", "PRA"),
    "LongMidRangeAssists": ("AST", "PA", "RA", "PRA"),
    "OPP_TOV": ("TOV",),
    "OPP_REB": ("REB", "PR", "RA", "PRA"),
    "OPP_STL": ("STL", "STKS"),
    "OPP_BLK": ("BLK", "STKS"),
}
_COMBO_PARTS = {
    "PRA": ("PTS", "REB", "AST"),
    "PA": ("PTS", "AST"),
    "PR": ("PTS", "REB"),
    "RA": ("REB", "AST"),
}
_DEFENSIVE_MARKET_COLUMNS = {
    "TOV": "OPP_TOV",
    "STL": "OPP_STL",
    "BLK": "OPP_BLK",
}
_PRIMITIVE_SCORE_INPUTS = {
    "PTS": (
        ("play_types", {"PTS": 1.0}, None),
        ("shot_zones", {"FGM": 1.0}, None),
        ("shot_types", {"FG2M": 2.0, "FG3M": 3.0}, None),
    ),
    "FGA": (
        ("shot_zones", {"FGA": 1.0}, None),
        ("shot_types", {"FG2A": 1.0, "FG3A": 1.0}, None),
    ),
    "3PM": (
        ("shot_zones", {"FGM": 1.0}, _THREE_POINT_SHOT_ZONES),
        ("shot_types", {"FG3M": 1.0}, None),
    ),
    "FG2A": (
        ("shot_zones", {"FGA": 1.0}, _TWO_POINT_SHOT_ZONES),
        ("shot_types", {"FG2A": 1.0}, None),
    ),
    "FG3A": (
        ("shot_zones", {"FGA": 1.0}, _THREE_POINT_SHOT_ZONES),
        ("shot_types", {"FG3A": 1.0}, None),
    ),
    "AST": (("assist_locations", None, None),),
}
_ASSIST_LOCATION_SLICES = frozenset(
    {
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    }
)
_DIET_SLICE_KEYS = {
    "play_types": frozenset(PLAY_TYPES),
    "shot_zones": _GOVERNED_SHOT_ZONES,
    "shot_types": frozenset(_SHOT_TYPE_STORED_SLICES),
    "assist_locations": _ASSIST_LOCATION_SLICES,
}
# Provider shares are rounded rather than derived from the persisted volumes.
# Recorded/live 2025-26 observations span 0.998-1.002 for a full play-type
# partition and 0.900-1.001 for the three governed shot-type rows.
_DIET_SHARE_BOUNDS = {
    "play_types": (0.995, 1.005),
    "shot_zones": (1.0 - 1e-6, 1.0 + 1e-6),
    "shot_types": (0.9, 1.01),
    "assist_locations": (1.0 - 1e-6, 1.0 + 1e-6),
}


class EventCatalogReader(Protocol):
    def count_events(self, season: str) -> int: ...

    def get_events(self, season: str) -> Sequence[Mapping[str, Any]]: ...

    def get_freshness(self, season: str, *, now: datetime) -> Mapping[str, Any]: ...


class StoredPlayerPoolReader(Protocol):
    def get_pool_for_game(self, *, season: str, game_id: str) -> PlayerPool | None: ...


class PlayerLogReader(Protocol):
    def get_player_summaries(
        self, season: str, player_ids: Sequence[int]
    ) -> dict[int, PlayerSeasonLogSummary]: ...

    def get_read_freshness(self, season: str) -> PlayerGameLogReadFreshness: ...


class PlayerDietReader(Protocol):
    def get_for_players(
        self, season: str, player_ids: Sequence[int]
    ) -> PlayerDietResult: ...


class TeamMatchupReader(Protocol):
    def get_latest_window(
        self,
        season: str,
        *,
        window_games: int | None = None,
        as_of: date | None = None,
    ) -> TeamMatchupWindow | None: ...


class StatsFreshnessReader(Protocol):
    def get(self) -> StatsFreshness: ...


class MatchupInjuryReader(Protocol):
    def get_injuries(
        self,
        *,
        event: Mapping[str, Any],
        season: str,
        pool_players: Sequence[PoolPlayer],
    ) -> MatchupInjuryResult: ...


_MetricIdentity = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class _WindowMetricIndex:
    league: Mapping[_MetricIdentity, LeagueMatchupMetric]
    teams: Mapping[int, Mapping[_MetricIdentity, TeamMatchupMetric]]

    @classmethod
    def build(cls, window: TeamMatchupWindow) -> _WindowMetricIndex:
        return cls(
            league={
                (metric.base, metric.slice_key, metric.stat_key): metric
                for metric in window.league_metrics
            },
            teams={
                team_id: {
                    (metric.base, metric.slice_key, metric.stat_key): metric
                    for metric in metrics
                }
                for team_id, metrics in window.team_metrics.items()
            },
        )


class MatchupService:
    """Build one response without NBA/DFS calls or lazy pool refreshes."""

    def __init__(
        self,
        *,
        event_catalog: EventCatalogReader | None,
        player_pool: StoredPlayerPoolReader | None,
        player_logs: PlayerLogReader,
        player_diets: PlayerDietReader | None,
        team_matchups: TeamMatchupReader | None,
        stats_freshness: StatsFreshnessReader,
        settings: RuntimeSettings,
        injuries: MatchupInjuryReader | None = None,
        clock: Callable[[], datetime] | None = None,
        database_only: bool = False,
        publication_reader: DatabaseFirstPublicationReader | None = None,
    ) -> None:
        self.event_catalog = event_catalog
        self.player_pool = player_pool
        self.player_logs = player_logs
        self.player_diets = player_diets
        self.team_matchups = team_matchups
        self.stats_freshness = stats_freshness
        self.settings = settings
        self.injuries = injuries
        self.database_only = bool(database_only)
        self.publication_reader = publication_reader
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._schedule_max_age = time_window_timedelta(
            settings.catalog.slate_schedule_max_age_hours,
            unit_seconds=3600,
            field="SLATE_SCHEDULE_MAX_AGE_HOURS",
        )

    def get_matchup(self, *, game_id: str) -> dict[str, Any]:
        season = self.settings.nba.current_season
        observed_at = assume_utc(self._clock())
        event, events = self._event(season, game_id)
        schedule_freshness = self._schedule_freshness(season, observed_at=observed_at)

        pool = (
            None
            if self.player_pool is None
            else self.player_pool.get_pool_for_game(season=season, game_id=game_id)
        )
        if pool is None:
            pool = PlayerPool((), {}, PlayerPool.unavailable_freshness())
        team_ids = (int(event["away_team_id"]), int(event["home_team_id"]))
        pool_players = tuple(
            player for player in pool.players if player.team_id in team_ids
        )
        injury_result = self._injuries(event, season, pool_players)
        players = tuple(
            player
            for player in pool_players
            if player.canonical_player_id not in injury_result.out_player_ids
        )
        summaries = self.player_logs.get_player_summaries(
            season, tuple(player.canonical_player_id for player in players)
        )
        log_freshness = self.player_logs.get_read_freshness(season)
        diets = self._diets(season, players)

        slate_date = self._event_date(event)
        scheduled_at = parse_utc_iso(str(event["scheduled_at"]))
        team_as_of = slate_date if scheduled_at <= observed_at else None
        season_window = self._team_window(season, window_games=None, as_of=team_as_of)
        last_15_window = self._team_window(season, window_games=15, as_of=team_as_of)
        windows = {"season": season_window, "last_15": last_15_window}
        metric_indexes = {
            name: None if not window else _WindowMetricIndex.build(window)
            for name, window in windows.items()
        }
        availability = self._surface_availability(
            windows, metric_indexes, team_ids
        )
        league = self._league(windows, metric_indexes, availability)
        teams = [
            self._team(
                event,
                team_id,
                windows,
                metric_indexes,
                availability,
            )
            for team_id in team_ids
        ]
        returned_counts = {
            team_id: sum(player.team_id == team_id for player in players)
            for team_id in team_ids
        }
        game = self._game(event)
        for side in ("away_team", "home_team"):
            game[side]["targetable_player_count"] = returned_counts[
                game[side]["team_id"]
            ]

        injury_freshness = {
            "status": injury_result.block["status"],
            "retrieved_at": injury_result.block["retrieved_at"],
        }
        return {
            "game": game,
            "league": league,
            "teams": teams,
            "players": self._players(
                players,
                summaries,
                diets,
                event,
                windows,
                metric_indexes,
                availability,
                injury_result.badge_refs,
            ),
            "injuries": dict(injury_result.block),
            "freshness": {
                "schedule": schedule_freshness,
                "pool": dict(pool.freshness),
                "stats": self._stats_freshness(events),
                "team_matchups": {
                    name: self._team_window_freshness(
                        window,
                        {
                            base: availability[base][name]
                            for base in DEFENSE_BASES
                        },
                    )
                    for name, window in windows.items()
                },
                "player_diets": self._diet_freshness(diets),
                "player_game_logs": self._timestamped_status(log_freshness),
                "injuries": injury_freshness,
            },
            **self._publication_metadata(season),
        }

    def _publication_metadata(self, season: str) -> dict[str, Any]:
        """Add additive provenance without collapsing independent clocks."""

        if self.publication_reader is not None:
            metadata = self.publication_reader.metadata(
                (
                    "player_game_logs",
                    "traditional_opponent_season",
                    "traditional_opponent_l15",
                    "assist_locations_season",
                    "assist_locations_l15",
                    "player_per36",
                    "synergy_play_types",
                    "grouped_shot_types",
                    "exact_shot_zones",
                ),
                season=season,
            )
        else:
            # Legacy deployments without the activation table still expose a
            # truthful additive document based on their stored read seams.
            metadata = {
                "streams": {},
                "mixed_cutoff": False,
                "mixed_freshness": False,
                "coverage_cutoffs": [],
            }
        return {
            "provenance": metadata["streams"],
            "coverage": {
                "mixed_cutoff": bool(metadata["mixed_cutoff"]),
                "mixed_freshness": bool(metadata["mixed_freshness"]),
                "coverage_cutoffs": list(metadata["coverage_cutoffs"]),
                "source": "database",
            },
        }

    def _injuries(
        self,
        event: Mapping[str, Any],
        season: str,
        pool_players: Sequence[PoolPlayer],
    ) -> MatchupInjuryResult:
        if self.injuries is not None:
            # Database-first applies to governed statistical facts.  Injury
            # Reports retain their existing live/snapshot contract, including
            # the provider path used before this migration; callers that need
            # a stored-only injury view use the dedicated service seam.
            return self.injuries.get_injuries(
                event=event,
                season=season,
                pool_players=pool_players,
            )
        return unavailable_injury_result("disabled")

    def _event(
        self, season: str, game_id: str
    ) -> tuple[Mapping[str, Any], Sequence[Mapping[str, Any]]]:
        if self.event_catalog is None or self.event_catalog.count_events(season) == 0:
            raise ProviderUnavailableError(
                "The matchup schedule is currently unavailable."
            )
        events = self.event_catalog.get_events(season)
        for event in events:
            if str(event.get("nba_game_id")) == game_id:
                classification = resolve_stored_event_classification(
                    game_id,
                    str(event.get("classification", event.get("season_type", ""))),
                )
                if classification.kind != "Regular Season":
                    raise ResourceNotFoundError(
                        "The requested matchup is outside the Regular Season window."
                    )
                return event, events
        raise ResourceNotFoundError("The requested matchup game was not found.")

    def _schedule_freshness(
        self, season: str, *, observed_at: datetime
    ) -> dict[str, Any]:
        if self.event_catalog is None:
            return {"status": "missing", "retrieved_at": None}
        observed = self.event_catalog.get_freshness(season, now=observed_at)
        retrieved_at = observed.get("last_success_at")
        if retrieved_at is None:
            return {"status": "missing", "retrieved_at": None}
        retrieved = parse_utc_iso(str(retrieved_at))
        elapsed = max(observed_at - retrieved, timedelta(0))
        status = (
            "fresh"
            if within_max_age(
                exact_age_seconds(exact_seconds(elapsed), field="matchup schedule age"),
                exact_seconds(self._schedule_max_age),
            )
            else "stale"
        )
        return {"status": status, "retrieved_at": retrieved.isoformat()}

    def _team_window(
        self, season: str, *, window_games: int | None, as_of: date | None
    ) -> TeamMatchupWindow | None:
        if self.team_matchups is None:
            return None
        return self.team_matchups.get_latest_window(
            season, window_games=window_games, as_of=as_of
        )

    def _diets(self, season: str, players: Sequence[PoolPlayer]) -> PlayerDietResult:
        if self.player_diets is None:
            return PlayerDietResult(season, {}, ())
        return self.player_diets.get_for_players(
            season, tuple(player.canonical_player_id for player in players)
        )

    @classmethod
    def _league(
        cls,
        windows: Mapping[str, TeamMatchupWindow | None],
        metric_indexes: Mapping[str, _WindowMetricIndex | None],
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        identities = cls._metric_identities(metric_indexes)
        sheets = {base: [] for base in DEFENSE_BASES}
        for base, slice_key, stat_key in identities:
            sheets[base].append(
                {
                    "key": cls._metric_key(base, slice_key, stat_key),
                    **{
                        window_name: cls._league_window_value(
                            metric_indexes[window_name],
                            base,
                            slice_key,
                            stat_key,
                            availability[base][window_name],
                        )
                        for window_name, window in windows.items()
                    },
                }
            )
        return {
            "surface_availability": {
                base: {name: dict(value) for name, value in states.items()}
                for base, states in availability.items()
            },
            "defense_sheet": sheets,
            "defensive_columns": {
                key: {
                    window_name: cls._league_column_value(
                        metric_indexes[window_name],
                        key,
                        availability["traditional"][window_name],
                    )
                    for window_name, window in windows.items()
                }
                for key in DEFENSIVE_COLUMNS
            },
        }

    @classmethod
    def _team(
        cls,
        event: Mapping[str, Any],
        team_id: int,
        windows: Mapping[str, TeamMatchupWindow | None],
        metric_indexes: Mapping[str, _WindowMetricIndex | None],
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        identities = cls._metric_identities(metric_indexes)
        sheets = {base: [] for base in DEFENSE_BASES}
        for base, slice_key, stat_key in identities:
            sheets[base].append(
                {
                    "key": cls._metric_key(base, slice_key, stat_key),
                    "label": cls._metric_label(base, slice_key, stat_key),
                    "markets": list(cls._markets(base, slice_key, stat_key)),
                    **{
                        window_name: cls._team_window_value(
                            metric_indexes[window_name],
                            team_id,
                            base,
                            slice_key,
                            stat_key,
                            availability[base][window_name],
                        )
                        for window_name, window in windows.items()
                    },
                }
            )
        team = cls._event_team(event, team_id)
        return {
            "team_id": team_id,
            "tricode": str(team["tricode"]),
            "name": str(team["name"]),
            "defense_sheet": sheets,
            "defensive_columns": {
                key: {
                    window_name: cls._team_column_value(
                        metric_indexes[window_name],
                        team_id,
                        key,
                        availability["traditional"][window_name],
                    )
                    for window_name, window in windows.items()
                }
                for key in DEFENSIVE_COLUMNS
            },
        }

    def _players(
        self,
        players: Sequence[PoolPlayer],
        summaries: Mapping[int, PlayerSeasonLogSummary],
        diets: PlayerDietResult,
        event: Mapping[str, Any],
        windows: Mapping[str, TeamMatchupWindow | None],
        metric_indexes: Mapping[str, _WindowMetricIndex | None],
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
        injury_badges: Mapping[int, str],
    ) -> list[dict[str, Any]]:
        rows = []
        for player in players:
            summary = summaries.get(player.canonical_player_id)
            scoring = (
                None
                if summary is None or summary.season_rate is None
                else summary.season_rate.per_game.get("PTS")
            )
            diet_by_base = {base: [] for base in PLAYER_DIET_BASES}
            for fact in diets.players.get(player.canonical_player_id, ()):
                diet_by_base[fact.base].append(
                    {
                        "key": fact.slice_key,
                        "season": {
                            "share": self._number(fact.share),
                            "volume": self._number(fact.volume),
                            "games_played": fact.games_played,
                            "volume_unit": fact.volume_unit,
                        },
                    }
                )
            team = self._event_team(event, player.team_id)
            rows.append(
                {
                    "canonical_id": int(player.canonical_player_id),
                    "name": player.name,
                    "team_id": int(player.team_id),
                    "tricode": str(team["tricode"]),
                    "posted_markets": list(player.market_categories),
                    "provenance": {
                        provider: list(categories)
                        for provider, categories in sorted(player.provenance.items())
                    },
                    "season_scoring": (
                        None if scoring is None else self._number(scoring)
                    ),
                    "last_10_minutes": (
                        []
                        if summary is None
                        else [self._number(value) for value in summary.last_ten_minutes]
                    ),
                    "diet_shares": diet_by_base,
                    "scores": self._scores(
                        player,
                        summary,
                        diets.players.get(player.canonical_player_id, ()),
                        event,
                        windows,
                        metric_indexes,
                        availability,
                    ),
                    "injury_badge_ref": injury_badges.get(
                        player.canonical_player_id
                    ),
                }
            )
        rows.sort(
            key=lambda row: (
                row["season_scoring"] is None,
                -(row["season_scoring"] or 0),
                row["canonical_id"],
            )
        )
        return rows

    def _scores(
        self,
        player: PoolPlayer,
        summary: PlayerSeasonLogSummary | None,
        diet_facts: Sequence[StoredPlayerDietFact],
        event: Mapping[str, Any],
        windows: Mapping[str, TeamMatchupWindow | None],
        metric_indexes: Mapping[str, _WindowMetricIndex | None],
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        opponent_id = next(
            team_id
            for team_id in (int(event["away_team_id"]), int(event["home_team_id"]))
            if team_id != player.team_id
        )
        memo: dict[tuple[str, str], dict[str, Any]] = {}
        return {
            market: {
                window_name: self._score_window(
                    market,
                    window_name,
                    metric_indexes[window_name],
                    opponent_id,
                    summary,
                    diet_facts,
                    availability,
                    memo,
                )
                for window_name, window in windows.items()
            }
            for market in player.market_categories
        }

    def _score_window(
        self,
        market: str,
        window_name: str,
        metric_index: _WindowMetricIndex | None,
        opponent_id: int,
        summary: PlayerSeasonLogSummary | None,
        diet_facts: Sequence[StoredPlayerDietFact],
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
        memo: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        key = (market, window_name)
        if key not in memo:
            memo[key] = self._compute_score_window(
                market,
                window_name,
                metric_index,
                opponent_id,
                summary,
                diet_facts,
                availability,
                memo,
            )
        return memo[key]

    def _compute_score_window(
        self,
        market: str,
        window_name: str,
        metric_index: _WindowMetricIndex | None,
        opponent_id: int,
        summary: PlayerSeasonLogSummary | None,
        diet_facts: Sequence[StoredPlayerDietFact],
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
        memo: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        if market in {*_DEFENSIVE_MARKET_COLUMNS, "STKS"}:
            return self._defensive_score_window(
                market,
                window_name,
                metric_index,
                opponent_id,
                summary,
                availability,
            )
        if market == "REB":
            return self._aggregate_offensive_score_window(
                window_name,
                metric_index,
                opponent_id,
                availability,
            )
        if market in _COMBO_PARTS:
            return self._combo_score_window(
                market,
                window_name,
                metric_index,
                opponent_id,
                summary,
                diet_facts,
                availability,
                memo,
            )
        components: dict[str, dict[str, Any]] = {}
        for base, stat_weights, slice_keys in _PRIMITIVE_SCORE_INPUTS.get(market, ()):
            component = self._diet_component(
                base=base,
                stat_weights=stat_weights,
                slice_keys=slice_keys,
                window_name=window_name,
                metric_index=metric_index,
                opponent_id=opponent_id,
                summary=summary,
                diet_facts=diet_facts,
                availability=availability,
            )
            if component is not None:
                components[base] = component
        blend = None
        if components:
            blend = {
                "value": self._number(
                    sum(cell["value"] for cell in components.values())
                    / len(components)
                ),
                "thin": any(cell["thin"] for cell in components.values()),
            }
        return {"components": components, "blend": blend}

    def _aggregate_offensive_score_window(
        self,
        window_name: str,
        metric_index: _WindowMetricIndex | None,
        opponent_id: int,
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        if (
            metric_index is None
            or availability["traditional"][window_name]["status"] != "available"
        ):
            return {"components": {}, "blend": None}
        identity = ("traditional", "OPP_REB", "OPP_REB")
        league = metric_index.league.get(identity)
        team = metric_index.teams.get(opponent_id, {}).get(identity)
        if league is None or team is None or league.average_allowed_per_48 <= 0:
            return {"components": {}, "blend": None}
        cell = {
            "value": self._number(
                team.allowed_per_48 / league.average_allowed_per_48 - 1
            ),
            "thin": False,
        }
        return {"components": {"traditional": cell}, "blend": dict(cell)}

    def _defensive_score_window(
        self,
        market: str,
        window_name: str,
        metric_index: _WindowMetricIndex | None,
        opponent_id: int,
        summary: PlayerSeasonLogSummary | None,
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        if (
            metric_index is None
            or availability["traditional"][window_name]["status"] != "available"
        ):
            return {"components": {}}

        def column_score(column: str) -> float | None:
            identity = ("traditional", column, column)
            league = metric_index.league.get(identity)
            team = metric_index.teams.get(opponent_id, {}).get(identity)
            if league is None or team is None or league.average_allowed_per_48 <= 0:
                return None
            return team.allowed_per_48 / league.average_allowed_per_48 - 1

        if market in _DEFENSIVE_MARKET_COLUMNS:
            value = column_score(_DEFENSIVE_MARKET_COLUMNS[market])
            if value is None:
                return {"components": {}}
            return {
                "components": {
                    "traditional": {"value": self._number(value), "thin": False}
                }
            }

        if summary is None or summary.season_rate is None:
            return {"components": {}}
        contributors = []
        for part in ("STL", "BLK"):
            weight = summary.season_rate.per_game.get(part, 0.0)
            value = column_score(_DEFENSIVE_MARKET_COLUMNS[part])
            if weight > 0 and value is not None:
                contributors.append((weight, value))
        if not contributors:
            return {"components": {}}
        total_weight = sum(weight for weight, _value in contributors)
        return {
            "components": {
                "traditional": {
                    "value": self._number(
                        sum(weight * value for weight, value in contributors)
                        / total_weight
                    ),
                    "thin": (
                        summary.season_rate.game_count
                        < self.settings.matchup_scores.min_games
                    ),
                }
            }
        }

    def _combo_score_window(
        self,
        market: str,
        window_name: str,
        metric_index: _WindowMetricIndex | None,
        opponent_id: int,
        summary: PlayerSeasonLogSummary | None,
        diet_facts: Sequence[StoredPlayerDietFact],
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
        memo: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        if summary is None or summary.season_rate is None:
            return {"components": {}, "blend": None}
        weights = {
            part: summary.season_rate.per_game.get(part, 0.0)
            for part in _COMBO_PARTS[market]
        }
        total_positive_weight = sum(
            weight for weight in weights.values() if weight > 0
        )
        part_scores = {
            part: self._score_window(
                part,
                window_name,
                metric_index,
                opponent_id,
                summary,
                diet_facts,
                availability,
                memo,
            )
            for part, weight in weights.items()
            if weight > 0
        }
        degraded = any(score["blend"] is None for score in part_scores.values())
        components = {}
        season_rate_thin = (
            summary.season_rate.game_count < self.settings.matchup_scores.min_games
        )
        for base in DEFENSE_BASES:
            contributors = [
                (weights[part], score["components"][base])
                for part, score in part_scores.items()
                if base in score["components"]
            ]
            if not contributors:
                continue
            components[base] = {
                "value": self._number(
                    sum(weight * cell["value"] for weight, cell in contributors)
                    / total_positive_weight
                ),
                "thin": (
                    degraded
                    or any(cell["thin"] for _weight, cell in contributors)
                ),
            }
            if season_rate_thin:
                components[base]["thin"] = True
        blend_contributors = [
            (weights[part], score["blend"])
            for part, score in part_scores.items()
            if score["blend"] is not None
        ]
        if not blend_contributors:
            return {"components": components, "blend": None}
        blend = {
            "value": self._number(
                sum(weight * cell["value"] for weight, cell in blend_contributors)
                / total_positive_weight
            ),
            "thin": (
                summary.season_rate.game_count
                < self.settings.matchup_scores.min_games
                or degraded
                or any(cell["thin"] for _weight, cell in blend_contributors)
            ),
        }
        return {"components": components, "blend": blend}

    def _diet_component(
        self,
        *,
        base: str,
        stat_weights: Mapping[str, float] | None,
        slice_keys: frozenset[str] | None,
        window_name: str,
        metric_index: _WindowMetricIndex | None,
        opponent_id: int,
        summary: PlayerSeasonLogSummary | None,
        diet_facts: Sequence[StoredPlayerDietFact],
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> dict[str, Any] | None:
        if (
            metric_index is None
            or availability[base][window_name]["status"] != "available"
        ):
            return None
        facts = tuple(fact for fact in diet_facts if fact.base == base)
        if not self._complete_diet(base, facts):
            return None
        selected_facts = tuple(
            fact for fact in facts if slice_keys is None or fact.slice_key in slice_keys
        )
        if not selected_facts:
            return None
        if slice_keys is None:
            positive_facts = []
            for fact in selected_facts:
                share = fact.share
                if share > 0:
                    positive_facts.append((fact, share))
            weighted_facts = tuple(positive_facts)
        else:
            selected_volume = sum(fact.volume for fact in selected_facts)
            if selected_volume <= 0:
                return None
            weighted_facts = tuple(
                (fact, fact.volume / selected_volume) for fact in selected_facts
                if fact.volume > 0
            )
        if not weighted_facts:
            return None
        total = 0.0
        weight_total = 0.0
        for fact, share in weighted_facts:
            league_value = 0.0
            team_value = 0.0
            slice_key = (
                _SHOT_TYPE_STORED_SLICES.get(fact.slice_key, fact.slice_key)
                if base == "shot_types"
                else fact.slice_key
            )
            resolved_weights = stat_weights or {fact.slice_key: 1.0}
            for stat_key, weight in resolved_weights.items():
                identity = (base, slice_key, stat_key)
                league = metric_index.league.get(identity)
                team = metric_index.teams.get(opponent_id, {}).get(identity)
                if league is None or team is None:
                    return None
                league_value += weight * league.average_allowed_per_48
                team_value += weight * team.allowed_per_48
            if league_value == 0 and team_value == 0:
                continue
            if league_value <= 0:
                return None
            total += share * (team_value / league_value)
            weight_total += share
        if weight_total == 0:
            return None
        volume_per_game = sum(
            fact.volume / fact.games_played for fact in selected_facts
        )
        return {
            "value": self._number(total - weight_total),
            "thin": (
                any(
                    fact.games_played < self.settings.matchup_scores.min_games
                    for fact in selected_facts
                )
                or volume_per_game
                < self.settings.matchup_scores.minimum_volume_per_game(base)
                or summary is None
                or summary.season_rate is None
                or summary.season_rate.game_count
                < self.settings.matchup_scores.min_games
            ),
        }

    @staticmethod
    def _complete_diet(
        base: str, facts: Sequence[StoredPlayerDietFact]
    ) -> bool:
        if not facts:
            return False
        keys = [fact.slice_key for fact in facts]
        if len(keys) != len(set(keys)) or set(keys) != _DIET_SLICE_KEYS[base]:
            return False
        lower, upper = _DIET_SHARE_BOUNDS[base]
        share_sum = sum(fact.share for fact in facts)
        return lower - 1e-12 <= share_sum <= upper + 1e-12

    @staticmethod
    def _availability(window: TeamMatchupWindow | None, base: str) -> dict[str, Any]:
        if not window:
            return {"status": "missing", "unavailable_reason": "not_stored"}
        observation = next(
            (item for item in window.observations if item.surface == base), None
        )
        if observation is None:
            return {"status": "missing", "unavailable_reason": "not_stored"}
        return {
            "status": observation.status,
            "unavailable_reason": observation.unavailable_reason,
        }

    @classmethod
    def _surface_availability(
        cls,
        windows: Mapping[str, TeamMatchupWindow | None],
        metric_indexes: Mapping[str, _WindowMetricIndex | None],
        team_ids: Sequence[int],
    ) -> dict[str, dict[str, dict[str, Any]]]:
        identities = cls._metric_identities(metric_indexes)
        expected_by_base = {
            base: {
                (slice_key, stat_key)
                for metric_base, slice_key, stat_key in identities
                if metric_base == base
            }
            for base in DEFENSE_BASES
        }
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for base in DEFENSE_BASES:
            result[base] = {}
            expected = expected_by_base[base]
            if base == "traditional":
                expected = (
                    expected | _REQUIRED_TRADITIONAL_IDENTITIES
                ) - {("OPP_REB", "OPP_REB")}
            for window_name, window in windows.items():
                metric_index = metric_indexes[window_name]
                state = cls._availability(window, base)
                if state["status"] != "available" or metric_index is None:
                    result[base][window_name] = state
                    continue
                if any(team_id not in metric_index.teams for team_id in team_ids):
                    result[base][window_name] = {
                        "status": "missing",
                        "unavailable_reason": "team_not_in_governed_roster",
                    }
                    continue
                league_identities = {
                    (slice_key, stat_key)
                    for metric_base, slice_key, stat_key in metric_index.league
                    if metric_base == base
                }
                team_identities = {
                    team_id: {
                        (slice_key, stat_key)
                        for metric_base, slice_key, stat_key in metric_index.teams[
                            team_id
                        ]
                        if metric_base == base
                    }
                    for team_id in team_ids
                }
                slice_sets = (
                    {
                        slice_key
                        for metric_base, slice_key, _stat_key in metric_index.league
                        if metric_base == base
                    },
                    *(
                        {
                            slice_key
                            for metric_base, slice_key, _stat_key in metric_index.teams[
                                team_id
                            ]
                            if metric_base == base
                        }
                        for team_id in team_ids
                    ),
                )
                missing_governed_slice = base == "shot_zones" and any(
                    not _GOVERNED_SHOT_ZONES.issubset(slice_set)
                    for slice_set in slice_sets
                )
                invalid_shot_types = base == "shot_types" and any(
                    slice_set != _GOVERNED_SHOT_TYPES for slice_set in slice_sets
                )
                if (
                    missing_governed_slice
                    or invalid_shot_types
                    or not expected.issubset(league_identities)
                    or any(
                        not expected.issubset(team_identities[team_id])
                        for team_id in team_ids
                    )
                ):
                    result[base][window_name] = {
                        "status": "unavailable",
                        "unavailable_reason": "legacy_surface_incomplete",
                    }
                    continue
                result[base][window_name] = state
        return result

    @staticmethod
    def _metric_identities(
        metric_indexes: Mapping[str, _WindowMetricIndex | None],
    ) -> tuple[tuple[str, str, str], ...]:
        identities = {
            identity
            for metric_index in metric_indexes.values()
            if metric_index is not None
            for identity in metric_index.league
            if identity[0] != "shot_zones"
            or identity[1] in _GOVERNED_SHOT_ZONES
            if identity[0] != "shot_types"
            or identity[1] in _SHOT_TYPE_DISPLAY_SLICES
        }
        return tuple(
            sorted(
                identities,
                key=lambda value: (DEFENSE_BASES.index(value[0]), value[1], value[2]),
            )
        )

    @classmethod
    def _league_window_value(
        cls,
        metric_index: _WindowMetricIndex | None,
        base: str,
        slice_key: str,
        stat_key: str,
        availability: Mapping[str, Any],
    ) -> dict[str, float] | None:
        if availability["status"] != "available" or metric_index is None:
            return None
        metric = metric_index.league.get((base, slice_key, stat_key))
        if metric is None:
            if base == "traditional" and slice_key == stat_key == "OPP_REB":
                return None
            raise ProviderUnavailableError(
                "Stored matchup league facts are incomplete."
            )
        return {
            "average_allowed_per_48": cls._number(metric.average_allowed_per_48),
            "sigma": cls._number(metric.sigma),
        }

    @classmethod
    def _team_window_value(
        cls,
        metric_index: _WindowMetricIndex | None,
        team_id: int,
        base: str,
        slice_key: str,
        stat_key: str,
        availability: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if availability["status"] != "available" or metric_index is None:
            return None
        metric = metric_index.teams.get(team_id, {}).get((base, slice_key, stat_key))
        if metric is None:
            if base == "traditional" and slice_key == stat_key == "OPP_REB":
                return None
            raise ProviderUnavailableError("Stored matchup team facts are incomplete.")
        return {
            "allowed_per_48": cls._number(metric.allowed_per_48),
            "percent_vs_league_average": (
                None
                if metric.percent_vs_league_average is None
                else cls._number(metric.percent_vs_league_average)
            ),
            "sigma_deviation": cls._number(metric.sigma_deviation),
            "rank": metric.rank,
        }

    @classmethod
    def _league_column_value(
        cls,
        metric_index: _WindowMetricIndex | None,
        key: str,
        availability: Mapping[str, Any],
    ) -> dict[str, float] | None:
        value = cls._league_window_value(
            metric_index, "traditional", key, key, availability
        )
        if value is None:
            return None
        return {
            "average_per_48": value["average_allowed_per_48"],
            "sigma": value["sigma"],
        }

    @classmethod
    def _team_column_value(
        cls,
        metric_index: _WindowMetricIndex | None,
        team_id: int,
        key: str,
        availability: Mapping[str, Any],
    ) -> dict[str, float | None] | None:
        value = cls._team_window_value(
            metric_index, team_id, "traditional", key, key, availability
        )
        if value is None:
            return None
        return {
            "per_48": value["allowed_per_48"],
            "percent_vs_league_average": value["percent_vs_league_average"],
        }

    @staticmethod
    def _metric_key(base: str, slice_key: str, stat_key: str) -> str:
        display_slice = MatchupService._display_slice(base, slice_key)
        return (
            display_slice
            if display_slice == stat_key
            else f"{display_slice}:{stat_key}"
        )

    @staticmethod
    def _metric_label(base: str, slice_key: str, stat_key: str) -> str:
        display_slice = MatchupService._display_slice(base, slice_key)
        label = display_slice.replace("_", " ").replace("-", " ")
        if display_slice == stat_key:
            return label
        return f"{label} {stat_key}"

    @staticmethod
    def _display_slice(base: str, slice_key: str) -> str:
        if base == "shot_types":
            return _SHOT_TYPE_DISPLAY_SLICES[slice_key]
        return slice_key

    @staticmethod
    def _markets(base: str, slice_key: str, stat_key: str) -> tuple[str, ...]:
        if base == "shot_zones":
            if slice_key in _TWO_POINT_SHOT_ZONES:
                if stat_key == "FGA":
                    return ("FGA", "FG2A")
                if stat_key == "FGM":
                    return ("PTS",)
            if slice_key in _THREE_POINT_SHOT_ZONES:
                if stat_key == "FGA":
                    return ("FGA", "FG3A")
                if stat_key == "FGM":
                    return ("PTS", "3PM")
        return _STAT_MARKETS.get(stat_key, ())

    @staticmethod
    def _event_team(event: Mapping[str, Any], team_id: int) -> Mapping[str, Any]:
        for side in ("away_team", "home_team"):
            team = event.get(side)
            if isinstance(team, Mapping) and int(team["id"]) == team_id:
                return team
        raise ProviderUnavailableError("Stored matchup team identity is incomplete.")

    @staticmethod
    def _event_date(event: Mapping[str, Any]) -> date:
        return parse_utc_iso(str(event["scheduled_at"])).astimezone(EASTERN).date()

    @staticmethod
    def _game(event: Mapping[str, Any]) -> dict[str, Any]:
        game_id = str(event["nba_game_id"])
        classification = resolve_stored_event_classification(
            game_id, str(event.get("classification") or "")
        )
        return SlateService._game(
            event,
            classification=classification.display,
            canonical_kind=classification.kind,
        )

    def _stats_freshness(
        self, events: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        completed = self.stats_freshness.get().last_successful_completion
        status = "missing"
        if completed is not None:
            completed = assume_utc(completed)
            latest_completed_game = max(
                (
                    parse_utc_iso(str(event["scheduled_at"]))
                    for event in events
                    if is_final_event(event) and not is_postponed_event(event)
                ),
                default=None,
            )
            status = (
                "stale"
                if latest_completed_game is not None
                and completed < latest_completed_game
                else "fresh"
            )
        return {
            "status": status,
            "retrieved_at": (
                completed.isoformat() if completed is not None else None
            ),
        }

    @classmethod
    def _team_window_freshness(
        cls,
        window: TeamMatchupWindow | None,
        availability: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        surfaces = {
            base: {
                **availability[base],
                "retrieved_at": cls._observation_time(window, base),
            }
            for base in DEFENSE_BASES
        }
        statuses = {surface["status"] for surface in surfaces.values()}
        if statuses == {"available"}:
            status = "fresh"
        elif "unavailable" in statuses:
            status = "unavailable"
        else:
            status = "missing"
        retrieved = sorted(
            surface["retrieved_at"]
            for surface in surfaces.values()
            if surface["retrieved_at"] is not None
        )
        return {
            "status": status,
            "retrieved_at": retrieved[0] if retrieved else None,
            "surfaces": surfaces,
        }

    @staticmethod
    def _observation_time(window: TeamMatchupWindow | None, base: str) -> str | None:
        if not window:
            return None
        observation = next(
            (item for item in window.observations if item.surface == base), None
        )
        return (
            None
            if observation is None
            else assume_utc(observation.retrieved_at).isoformat()
        )

    @staticmethod
    def _diet_freshness(diets: PlayerDietResult) -> dict[str, Any]:
        observations = {item.base: item for item in diets.observations}
        surfaces = {}
        for base in PLAYER_DIET_BASES:
            observation = observations.get(base)
            surfaces[base] = {
                "status": observation.status if observation else "missing",
                "unavailable_reason": (
                    observation.unavailable_reason if observation else "not_stored"
                ),
                "retrieved_at": (
                    assume_utc(observation.retrieved_at).isoformat()
                    if observation
                    else None
                ),
            }
        statuses = {surface["status"] for surface in surfaces.values()}
        status = (
            "fresh"
            if statuses == {"available"}
            else "unavailable"
            if "unavailable" in statuses
            else "missing"
        )
        retrieved = sorted(
            surface["retrieved_at"]
            for surface in surfaces.values()
            if surface["retrieved_at"] is not None
        )
        return {
            "status": status,
            "retrieved_at": retrieved[0] if retrieved else None,
            "surfaces": surfaces,
        }

    @staticmethod
    def _timestamped_status(value: PlayerGameLogReadFreshness) -> dict[str, Any]:
        return {
            "status": value.status,
            "retrieved_at": (
                assume_utc(value.retrieved_at).isoformat()
                if value.retrieved_at is not None
                else None
            ),
        }

    @staticmethod
    def _number(value: float) -> float:
        return round(float(value), _WIRE_PRECISION)


__all__ = [
    "DEFENSE_BASES",
    "DEFENSIVE_COLUMNS",
    "MatchupService",
]
