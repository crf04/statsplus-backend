"""Compose the matchup response exclusively from durable governed seams."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
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
from app.services.player_diet import PLAYER_DIET_BASES, PlayerDietResult
from app.services.player_game_log_repository import (
    PlayerGameLogReadFreshness,
    PlayerSeasonLogSummary,
)
from app.services.player_pool import PlayerPool, PoolPlayer
from app.services.slate_service import SlateService
from app.services.stats_freshness_repository import StatsFreshness
from app.services.team_matchup_query import (
    LeagueMatchupMetric,
    TeamMatchupMetric,
    TeamMatchupWindow,
)


EASTERN = ZoneInfo("America/New_York")
DEFENSE_BASES = (
    "play_types",
    "shot_zones",
    "shot_types",
    "assist_locations",
    "traditional",
)
DEFENSIVE_COLUMNS = ("OPP_TOV", "OPP_STL", "OPP_BLK")
_WIRE_PRECISION = 6
_SCORES_UNAVAILABLE = {
    "status": "unavailable",
    "unavailable_reason": "not_in_scope",
}
_INJURY_SOURCE_URL = "https://www.rotowire.com/basketball/injury-report.php"
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
    "OPP_STL": ("STL", "STKS"),
    "OPP_BLK": ("BLK", "STKS"),
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


class MatchupService:
    """Build one response without provider calls or lazy pool refreshes."""

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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.event_catalog = event_catalog
        self.player_pool = player_pool
        self.player_logs = player_logs
        self.player_diets = player_diets
        self.team_matchups = team_matchups
        self.stats_freshness = stats_freshness
        self.settings = settings
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._schedule_max_age = time_window_timedelta(
            settings.catalog.slate_schedule_max_age_hours,
            unit_seconds=3600,
            field="SLATE_SCHEDULE_MAX_AGE_HOURS",
        )

    def get_matchup(self, *, game_id: str) -> dict[str, Any]:
        season = self.settings.nba.current_season
        observed_at = assume_utc(self._clock())
        event = self._event(season, game_id)
        schedule_freshness = self._schedule_freshness(season, observed_at=observed_at)

        pool = (
            None
            if self.player_pool is None
            else self.player_pool.get_pool_for_game(season=season, game_id=game_id)
        )
        if pool is None:
            pool = PlayerPool((), {}, PlayerPool.unavailable_freshness())
        team_ids = (int(event["away_team_id"]), int(event["home_team_id"]))
        players = tuple(player for player in pool.players if player.team_id in team_ids)
        summaries = self.player_logs.get_player_summaries(
            season, tuple(player.canonical_player_id for player in players)
        )
        log_freshness = self.player_logs.get_read_freshness(season)
        diets = self._diets(season, players)

        slate_date = self._event_date(event)
        current_date = observed_at.astimezone(EASTERN).date()
        team_as_of = slate_date if slate_date <= current_date else None
        season_window = self._team_window(season, window_games=None, as_of=team_as_of)
        last_15_window = self._team_window(season, window_games=15, as_of=team_as_of)
        windows = {"season": season_window, "last_15": last_15_window}
        availability = {
            base: {
                window_name: self._availability(window, base)
                for window_name, window in windows.items()
            }
            for base in DEFENSE_BASES
        }
        league = self._league(windows, availability)
        teams = [
            self._team(event, team_id, windows, availability) for team_id in team_ids
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

        injury_freshness = {"status": "unavailable", "retrieved_at": None}
        return {
            "game": game,
            "league": league,
            "teams": teams,
            "players": self._players(players, summaries, diets, event),
            "injuries": {
                "status": "unavailable",
                "unavailable_reason": "disabled",
                "retrieved_at": None,
                "source": "rotowire",
                "source_url": _INJURY_SOURCE_URL,
                "teams": [],
            },
            "freshness": {
                "schedule": schedule_freshness,
                "pool": dict(pool.freshness),
                "stats": self._stats_freshness(season),
                "team_matchups": {
                    name: self._team_window_freshness(window)
                    for name, window in windows.items()
                },
                "player_diets": self._diet_freshness(diets),
                "player_game_logs": self._timestamped_status(log_freshness),
                "injuries": injury_freshness,
            },
        }

    def _event(self, season: str, game_id: str) -> Mapping[str, Any]:
        if self.event_catalog is None or self.event_catalog.count_events(season) == 0:
            raise ProviderUnavailableError(
                "The matchup schedule is currently unavailable."
            )
        for event in self.event_catalog.get_events(season):
            if str(event.get("nba_game_id")) == game_id:
                return event
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
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        identities = cls._metric_identities(windows, league=True)
        sheets = {base: [] for base in DEFENSE_BASES}
        for base, slice_key, stat_key in identities:
            sheets[base].append(
                {
                    "key": cls._metric_key(slice_key, stat_key),
                    **{
                        window_name: cls._league_window_value(
                            window,
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
                        window,
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
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        identities = cls._metric_identities(windows, league=True)
        sheets = {base: [] for base in DEFENSE_BASES}
        for base, slice_key, stat_key in identities:
            sheets[base].append(
                {
                    "key": cls._metric_key(slice_key, stat_key),
                    "label": cls._metric_label(slice_key, stat_key),
                    "markets": list(_STAT_MARKETS.get(stat_key, ())),
                    **{
                        window_name: cls._team_window_value(
                            window,
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
                        window,
                        team_id,
                        key,
                        availability["traditional"][window_name],
                    )
                    for window_name, window in windows.items()
                }
                for key in DEFENSIVE_COLUMNS
            },
        }

    @classmethod
    def _players(
        cls,
        players: Sequence[PoolPlayer],
        summaries: Mapping[int, PlayerSeasonLogSummary],
        diets: PlayerDietResult,
        event: Mapping[str, Any],
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
                            "share": cls._number(fact.share),
                            "volume": cls._number(fact.volume),
                            "games_played": fact.games_played,
                            "volume_unit": fact.volume_unit,
                        },
                    }
                )
            team = cls._event_team(event, player.team_id)
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
                        None if scoring is None else cls._number(scoring)
                    ),
                    "last_10_minutes": (
                        []
                        if summary is None
                        else [cls._number(value) for value in summary.last_ten_minutes]
                    ),
                    "diet_shares": diet_by_base,
                    "scores": dict(_SCORES_UNAVAILABLE),
                    "injury_badge_ref": None,
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

    @staticmethod
    def _metric_identities(
        windows: Mapping[str, TeamMatchupWindow | None], *, league: bool
    ) -> tuple[tuple[str, str, str], ...]:
        identities = {
            (metric.base, metric.slice_key, metric.stat_key)
            for window in windows.values()
            if window
            for metric in (window.league_metrics if league else ())
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
        window: TeamMatchupWindow | None,
        base: str,
        slice_key: str,
        stat_key: str,
        availability: Mapping[str, Any],
    ) -> dict[str, float] | None:
        if availability["status"] != "available" or not window:
            return None
        metric = cls._league_metric(window, base, slice_key, stat_key)
        if metric is None:
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
        window: TeamMatchupWindow | None,
        team_id: int,
        base: str,
        slice_key: str,
        stat_key: str,
        availability: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if availability["status"] != "available" or not window:
            return None
        metric = cls._team_metric(window, team_id, base, slice_key, stat_key)
        if metric is None:
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
        window: TeamMatchupWindow | None,
        key: str,
        availability: Mapping[str, Any],
    ) -> dict[str, float] | None:
        value = cls._league_window_value(window, "traditional", key, key, availability)
        if value is None:
            return None
        return {
            "average_per_48": value["average_allowed_per_48"],
            "sigma": value["sigma"],
        }

    @classmethod
    def _team_column_value(
        cls,
        window: TeamMatchupWindow | None,
        team_id: int,
        key: str,
        availability: Mapping[str, Any],
    ) -> dict[str, float | None] | None:
        value = cls._team_window_value(
            window, team_id, "traditional", key, key, availability
        )
        if value is None:
            return None
        return {
            "per_48": value["allowed_per_48"],
            "percent_vs_league_average": value["percent_vs_league_average"],
        }

    @staticmethod
    def _league_metric(
        window: TeamMatchupWindow, base: str, slice_key: str, stat_key: str
    ) -> LeagueMatchupMetric | None:
        return next(
            (
                metric
                for metric in window.league_metrics
                if (metric.base, metric.slice_key, metric.stat_key)
                == (base, slice_key, stat_key)
            ),
            None,
        )

    @staticmethod
    def _team_metric(
        window: TeamMatchupWindow,
        team_id: int,
        base: str,
        slice_key: str,
        stat_key: str,
    ) -> TeamMatchupMetric | None:
        return next(
            (
                metric
                for metric in window.team_metrics.get(team_id, ())
                if (metric.base, metric.slice_key, metric.stat_key)
                == (base, slice_key, stat_key)
            ),
            None,
        )

    @staticmethod
    def _metric_key(slice_key: str, stat_key: str) -> str:
        return slice_key if slice_key == stat_key else f"{slice_key}:{stat_key}"

    @staticmethod
    def _metric_label(slice_key: str, stat_key: str) -> str:
        label = slice_key.replace("_", " ").replace("-", " ")
        if slice_key == stat_key:
            return label
        return f"{label} {stat_key}"

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

    def _stats_freshness(self, season: str) -> dict[str, Any]:
        completed = self.stats_freshness.get().last_successful_completion
        status = "missing"
        if completed is not None:
            completed = assume_utc(completed)
            latest_completed_game = max(
                (
                    parse_utc_iso(str(event["scheduled_at"]))
                    for event in (
                        self.event_catalog.get_events(season)
                        if self.event_catalog is not None
                        else ()
                    )
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
    def _team_window_freshness(cls, window: TeamMatchupWindow | None) -> dict[str, Any]:
        surfaces = {
            base: {
                **cls._availability(window, base),
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
