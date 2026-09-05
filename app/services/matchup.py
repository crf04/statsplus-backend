"""Compose the matchup response exclusively from durable governed seams."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.config.settings import MatchupScoreSettings, RuntimeSettings
from app.domain.freshness import (
    exact_age_seconds,
    exact_seconds,
    time_window_timedelta,
    within_max_age,
)
from app.domain.matchup_experience import (
    CURRENT_MODE,
    GAME_LOG_SOURCE,
    HISTORICAL_MODE,
    PLAYER_POOL_SOURCE,
    experience_mode,
    is_historical_matchup,
    player_source,
)
from app.domain.nba_events import resolve_stored_event_classification
from app.domain.play_type_matchup import complete_play_type_shares, play_type_matchup
from app.domain.utc import assume_utc, parse_utc_iso
from app.errors import ProviderUnavailableError, ResourceNotFoundError
from app.domain.team_matchup_taxonomy import (
    SHOT_TYPE_DISPLAY_TO_STORED,
    SHOT_TYPE_SLICES,
    SHOT_TYPE_STORED_TO_DISPLAY,
    SHOT_ZONE_SLICES,
    THREE_POINT_SHOT_ZONES,
    TWO_POINT_SHOT_ZONES,
)
from app.services.player_diet import (
    PLAYER_DIET_BASES,
    PLAYER_DIET_PUBLICATION_STREAM_KEYS,
    PlayerDietResult,
    StoredPlayerDietFact,
)
from app.services.player_game_log_repository import (
    PlayerGameLogReadFreshness,
    PlayerGameLogRecord,
    PlayerGameLogSyncStatus,
    PlayerSeasonLogSummary,
)
from app.services.player_game_log_values import player_game_log_focal_line
from app.services.statistic_catalog import StatisticCatalog
from app.services.player_pool import PlayerPool, PoolPlayer, SingleGamePlayerPoolReader
from app.services.matchup_injuries import (
    MatchupInjuryResult,
    unavailable_injury_result,
)
from app.services.slate_service import SlateService
from app.services.stats_freshness_repository import StatsFreshness
from app.services.team_matchup_query import (
    LeagueMatchupMetric,
    TEAM_MATCHUP_PUBLICATION_STREAM_KEYS,
    TeamMatchupMetric,
    TeamMatchupWindow,
)
from app.services.database_first_activation import DatabaseFirstPublicationReader
from app.services.publication_snapshot_calls import (
    accepts_keyword,
    call_with_read_scope,
)
from app.services.request_reads import request_read_scope


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
_TWO_POINT_SHOT_ZONES = TWO_POINT_SHOT_ZONES
_THREE_POINT_SHOT_ZONES = THREE_POINT_SHOT_ZONES
_GOVERNED_SHOT_ZONES = frozenset(SHOT_ZONE_SLICES)
_SHOT_TYPE_DISPLAY_SLICES = dict(SHOT_TYPE_STORED_TO_DISPLAY)
_SHOT_TYPE_STORED_SLICES = dict(SHOT_TYPE_DISPLAY_TO_STORED)
_GOVERNED_SHOT_TYPES = frozenset(SHOT_TYPE_SLICES)
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
    "shot_zones": _GOVERNED_SHOT_ZONES,
    "shot_types": frozenset(_SHOT_TYPE_STORED_SLICES),
    "assist_locations": _ASSIST_LOCATION_SLICES,
}
# Provider shares are rounded rather than derived from the persisted volumes.
# Recorded/live 2025-26 observations span 0.998-1.002 for a full play-type
# partition and 0.900-1.001 for the three governed shot-type rows.
# Synergy publishes a player's play-type row only where the sample is large
# enough to rate, so a player's observed shares are a partial partition whose
# unobserved residual is neutral by construction. Below this coverage the
# component still computes, but the cell is marked thin.
_PLAY_TYPE_THIN_COVERAGE = 0.85
_DIET_SHARE_BOUNDS = {
    "shot_zones": (1.0 - 1e-6, 1.0 + 1e-6),
    "shot_types": (0.9, 1.01),
    "assist_locations": (1.0 - 1e-6, 1.0 + 1e-6),
}

# The score contract's required Bases per governed Stat Category. A Base named
# here and absent from a window's computed components is a named missing input
# rather than a silent partial blend.
_SCORE_INPUT_BASES = {
    market: tuple(base for base, _weights, _slices in inputs)
    for market, inputs in _PRIMITIVE_SCORE_INPUTS.items()
}
_SCORE_INPUT_BASES.update(
    {
        "REB": ("traditional",),
        "TOV": ("traditional",),
        "STL": ("traditional",),
        "BLK": ("traditional",),
        "STKS": ("traditional",),
    }
)
_SCORE_INPUT_BASES.update(
    {
        market: tuple(
            base
            for base in DEFENSE_BASES
            if any(base in _SCORE_INPUT_BASES[part] for part in parts)
        )
        for market, parts in _COMBO_PARTS.items()
    }
)
#: Categories whose score consumes the player's own stored Season rate.
_SEASON_RATE_MARKETS = frozenset({*_COMBO_PARTS, "STKS"})
#: Every Stat Category the backend can score from stored evidence.
_SCOREABLE_MARKETS = frozenset(_SCORE_INPUT_BASES)

_PUBLICATION_STREAM_KEYS = (
    "player_game_logs",
    "player_per36",
    "synergy:l15",
    *sorted(PLAYER_DIET_PUBLICATION_STREAM_KEYS),
    *sorted(frozenset().union(*TEAM_MATCHUP_PUBLICATION_STREAM_KEYS.values())),
)
_PROJECTION_ONLY_STREAM_KEYS = frozenset({"player_game_logs"})


class EventCatalogReader(Protocol):
    def count_events(self, season: str) -> int: ...

    def get_event(self, season: str, nba_game_id: str) -> Mapping[str, Any] | None: ...

    def latest_final_scheduled_at(self, season: str) -> datetime | None: ...

    def get_freshness(self, season: str, *, now: datetime) -> Mapping[str, Any]: ...


class PlayerLogReader(Protocol):
    def get_player_summaries(
        self,
        season: str,
        player_ids: Sequence[int],
        *,
        publication_snapshot: Any | None = None,
    ) -> dict[int, PlayerSeasonLogSummary]: ...

    def get_read_freshness(
        self, season: str, *, publication_snapshot: Any | None = None
    ) -> PlayerGameLogReadFreshness: ...

    def list_game_rows(
        self,
        season: str,
        game_id: str,
        *,
        publication_snapshot: Any | None = None,
    ) -> Sequence[PlayerGameLogRecord]: ...

    def get_sync_status(
        self, season: str, game_id: str
    ) -> PlayerGameLogSyncStatus | None: ...


class PlayerDietReader(Protocol):
    def get_for_players(
        self,
        season: str,
        player_ids: Sequence[int],
        *,
        publication_snapshot: Any | None = None,
    ) -> PlayerDietResult: ...


class TeamMatchupReader(Protocol):
    def get_latest_window(
        self,
        season: str,
        *,
        window_games: int | None = None,
        as_of: date | None = None,
        publication_snapshot: Any | None = None,
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
class _Participant:
    """One rail row and the evidence that named it.

    A current-slate participant is a Player Pool entry; a Historical Matchup
    participant is one canonical game-log row, so its team identity is the
    identity recorded for that game rather than a current roster.
    """

    canonical_player_id: int
    name: str
    team_id: int
    tricode: str
    market_categories: tuple[str, ...]
    provenance: Mapping[str, tuple[str, ...]]
    source: str
    focal_game_line: dict[str, Any] | None = None


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


@dataclass(frozen=True, slots=True)
class _PlayerDiet:
    """One player's Season Diet facts, with each Base read for coverage once.

    Completeness is asked of a Base many times over one player -- once per
    Stat Category and window whose score consumes it, and once more for the
    Base's own thin verdict -- and the answer cannot change between those
    reads.  Computing it once with the facts it describes keeps the reads off
    the hot path and keeps every consumer looking at the same number.
    """

    facts: tuple[StoredPlayerDietFact, ...]
    by_base: Mapping[str, tuple[StoredPlayerDietFact, ...]]
    observed_shares: Mapping[str, float | None]

    @classmethod
    def build(
        cls,
        facts: Sequence[StoredPlayerDietFact],
        complete_diet: Callable[
            [str, Sequence[StoredPlayerDietFact]], float | None
        ],
    ) -> _PlayerDiet:
        by_base = {
            base: tuple(fact for fact in facts if fact.base == base)
            for base in PLAYER_DIET_BASES
        }
        return cls(
            facts=tuple(facts),
            by_base=by_base,
            observed_shares={
                base: complete_diet(base, base_facts)
                for base, base_facts in by_base.items()
            },
        )


def observed_diet_share(
    base: str, facts: Sequence[StoredPlayerDietFact]
) -> float | None:
    """Return the observed share sum of a complete Diet, else ``None``.

    A Base's own coverage, and the first leg of the thin rule below: ``None``
    means the stored Diet is not a usable partition of the Base at all.  It is
    module-level rather than private to the Matchup because every surface that
    has to ask "is this player's Diet for this Base thin" -- the Target
    backtest included -- has to read coverage the same way the Matchup does.
    """

    if not facts:
        return None
    if base == "play_types":
        shares = complete_play_type_shares(
            (fact.slice_key, fact.share) for fact in facts
        )
        return None if shares is None else sum(shares.values())
    keys = [fact.slice_key for fact in facts]
    if len(keys) != len(set(keys)):
        return None
    if set(keys) != _DIET_SLICE_KEYS[base]:
        return None
    lower, upper = _DIET_SHARE_BOUNDS[base]
    share_sum = sum(fact.share for fact in facts)
    if not lower - 1e-12 <= share_sum <= upper + 1e-12:
        return None
    return share_sum


def diet_evidence_thin(
    *,
    base: str,
    observed_share: float | None,
    selected_facts: Sequence[StoredPlayerDietFact],
    summary: PlayerSeasonLogSummary | None,
    settings: MatchupScoreSettings,
) -> bool:
    """Whether a player's Diet evidence for one Base is too slight to lean on.

    The single statement of "thin" in the backend.  A Matchup Score cell marks
    itself thin with it, and every other surface that reports a thin Diet --
    Target resolution included -- reports the same verdict from here, so the
    two can never disagree about the same player.

    `selected_facts` is the evidence actually consumed: the whole Base for a
    whole-Base read, or the slices a score component restricted itself to.
    `observed_share` is the Base's own coverage, `None` when the stored Diet
    is not a usable partition of the Base at all -- which is thin by
    definition, since a score refuses to consume it.  Play types are thin
    below a coverage threshold as well: Synergy publishes a row only where the
    sample rates, so a partition that falls short describes less of the player
    than its shares appear to.  No evidence at all is thin too: an absent Base
    clears no floor.
    """

    return (
        observed_share is None
        or (base == "play_types" and observed_share < _PLAY_TYPE_THIN_COVERAGE)
        or any(fact.games_played < settings.min_games for fact in selected_facts)
        or sum(fact.volume / fact.games_played for fact in selected_facts)
        < settings.minimum_volume_per_game(base)
        or summary is None
        or summary.season_rate is None
        or summary.season_rate.game_count < settings.min_games
    )


class MatchupService:
    """Build one response without NBA/DFS calls or lazy pool refreshes."""

    def __init__(
        self,
        *,
        event_catalog: EventCatalogReader | None,
        player_pool: SingleGamePlayerPoolReader | None,
        player_logs: PlayerLogReader,
        player_diets: PlayerDietReader | None,
        team_matchups: TeamMatchupReader | None,
        stats_freshness: StatsFreshnessReader,
        settings: RuntimeSettings,
        statistic_catalog: StatisticCatalog | None = None,
        injuries: MatchupInjuryReader | None = None,
        clock: Callable[[], datetime] | None = None,
        database_only: bool = False,
        publication_reader: DatabaseFirstPublicationReader | None = None,
        engine: Engine | None = None,
    ) -> None:
        self.event_catalog = event_catalog
        self.player_pool = player_pool
        self.player_logs = player_logs
        self.player_diets = player_diets
        self.team_matchups = team_matchups
        self.stats_freshness = stats_freshness
        self.settings = settings
        self.injuries = injuries
        # Historical Stat Categories come from the governed Statistic Catalog
        # crossed with the score-input contract, never from a DFS archive.
        self._statistics = (
            {}
            if statistic_catalog is None
            else {
                statistic.market_category: statistic
                for statistic in statistic_catalog.statistics
                if statistic.market_category in _SCOREABLE_MARKETS
            }
        )
        self._historical_categories = tuple(sorted(self._statistics))
        self.database_only = bool(database_only)
        self.publication_reader = publication_reader
        # One request checks out one connection for every read it composes;
        # without an engine each seam keeps opening its own.
        self._engine = engine
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._schedule_max_age = time_window_timedelta(
            settings.catalog.slate_schedule_max_age_hours,
            unit_seconds=3600,
            field="SLATE_SCHEDULE_MAX_AGE_HOURS",
        )

    def get_matchup(self, *, game_id: str) -> dict[str, Any]:
        with request_read_scope(self._engine) as (connection, session):
            return self._compose_matchup(
                game_id=game_id, connection=connection, session=session
            )

    def _compose_matchup(
        self,
        *,
        game_id: str,
        connection: Connection | None,
        session: Session | None,
    ) -> dict[str, Any]:
        season = self.settings.nba.current_season
        observed_at = assume_utc(self._clock())
        publication_snapshot = self._publication_snapshot(season, session=session)
        event = self._event(season, game_id, connection=connection)
        schedule_freshness = self._schedule_freshness(
            season, observed_at=observed_at, connection=connection
        )

        pool = (
            None
            if self.player_pool is None
            else call_with_read_scope(
                self.player_pool.get_pool_for_game,
                season=season,
                game_id=game_id,
                connection=connection,
            )
        )
        if pool is None:
            pool = PlayerPool((), {}, PlayerPool.unavailable_freshness())
        team_ids = (int(event["away_team_id"]), int(event["home_team_id"]))
        pool_players = tuple(
            player for player in pool.players if player.team_id in team_ids
        )
        injury_result = self._injuries(event, season, pool_players)
        # The backend declares the mode so no client ever infers it from tip
        # dates, empty arrays, or freshness markers.
        historical = is_historical_matchup(event, pool_players)
        if historical:
            players, participants_section = self._historical_participants(
                season,
                game_id,
                team_ids,
                publication_snapshot=publication_snapshot,
                connection=connection,
            )
        else:
            # A matched Out entry removes a targetable player; a game-log
            # participant already played, so injuries never remove one.
            players = tuple(
                _Participant(
                    canonical_player_id=player.canonical_player_id,
                    name=player.name,
                    team_id=player.team_id,
                    tricode=str(self._event_team(event, player.team_id)["tricode"]),
                    market_categories=player.market_categories,
                    provenance=player.provenance,
                    source=PLAYER_POOL_SOURCE,
                )
                for player in pool_players
                if player.canonical_player_id not in injury_result.out_player_ids
            )
            participants_section = self._pool_participants_section(pool_players)
        player_ids = tuple(player.canonical_player_id for player in players)
        # One season-summary read feeds both the rail's display fields and
        # the Matchup Score inputs, in every mode. It is season-to-date
        # evidence in current mode; #47 made it the completed-season evidence
        # a Historical Matchup scores from too, focal game included, with
        # hindsight disclosed by label rather than by withholding an input.
        summaries = call_with_read_scope(
            self.player_logs.get_player_summaries,
            season,
            player_ids,
            publication_snapshot=publication_snapshot,
            connection=connection,
        )
        log_freshness = call_with_read_scope(
            self.player_logs.get_read_freshness,
            season,
            publication_snapshot=publication_snapshot,
            connection=connection,
        )
        diets = self._diets(
            season,
            players,
            publication_snapshot=publication_snapshot,
            connection=connection,
        )

        slate_date = self._event_date(event)
        scheduled_at = parse_utc_iso(str(event["scheduled_at"]))
        team_as_of = slate_date if scheduled_at <= observed_at else None
        season_window = self._team_window(
            season,
            window_games=None,
            as_of=team_as_of,
            publication_snapshot=publication_snapshot,
            connection=connection,
        )
        last_15_window = self._team_window(
            season,
            window_games=15,
            as_of=team_as_of,
            publication_snapshot=publication_snapshot,
            connection=connection,
        )
        windows = {"season": season_window, "last_15": last_15_window}
        metric_indexes = {
            name: None if not window else _WindowMetricIndex.build(window)
            for name, window in windows.items()
        }
        availability = self._surface_availability(
            windows, metric_indexes, team_ids
        )
        # #47 supersedes the earlier focal-safe scoring rule: a Historical
        # Matchup scores from the same completed-season windows the Defense
        # Sheet displays, focal game included. Last-15 still has no
        # point-in-time snapshot, so it reports its own `team_defense:<base>`
        # gap in `missing_inputs` exactly as it always has.
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
            team_id: sum(
                player.team_id == team_id
                for player in players
                if player.source == PLAYER_POOL_SOURCE
            )
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
            "experience": self._experience(
                historical=historical,
                schedule_freshness=schedule_freshness,
                participants=participants_section,
                availability=availability,
                injury_block=injury_result.block,
            ),
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
                {} if historical else injury_result.badge_refs,
            ),
            "injuries": dict(injury_result.block),
            "freshness": {
                "schedule": schedule_freshness,
                "pool": dict(pool.freshness),
                "stats": self._stats_freshness(season, connection=connection),
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
            **self._publication_metadata(
                season,
                publication_snapshot,
            ),
        }

    def _historical_participants(
        self,
        season: str,
        game_id: str,
        team_ids: Sequence[int],
        *,
        publication_snapshot=None,
        connection: Connection | None = None,
    ) -> tuple[tuple[_Participant, ...], dict[str, Any]]:
        """Name the players with a complete canonical row for this game."""

        sync = call_with_read_scope(
            self.player_logs.get_sync_status,
            season,
            game_id,
            connection=connection,
        )
        if sync is None or sync.status != "complete":
            # Incomplete canonical logs remove only Participants; every other
            # section keeps reporting its own evidence.
            return (), {
                "status": "unavailable",
                "source": "player_game_logs",
                "context": None,
                "unavailable_reason": "game_logs_incomplete",
            }
        rows = call_with_read_scope(
            self.player_logs.list_game_rows,
            season,
            game_id,
            publication_snapshot=publication_snapshot,
            connection=connection,
        )
        participants = tuple(
            _Participant(
                canonical_player_id=int(record.player_id),
                name=record.player_name,
                # The game-time identity is authoritative: a later trade
                # cannot rewrite who a player suited up for that night.
                team_id=int(record.team_id),
                tricode=str(record.team_tricode),
                market_categories=self._historical_categories,
                provenance={},
                source=GAME_LOG_SOURCE,
                focal_game_line=player_game_log_focal_line(
                    record, self._historical_categories, self._statistics
                ),
            )
            for record in rows
            if int(record.team_id) in tuple(team_ids)
        )
        if not participants:
            return (), {
                "status": "unavailable",
                "source": "player_game_logs",
                "context": None,
                "unavailable_reason": "no_game_log_rows",
            }
        return participants, {
            "status": "available",
            "source": "player_game_logs",
            "context": "completed_season",
            "unavailable_reason": None,
        }

    @staticmethod
    def _pool_participants_section(
        pool_players: Sequence[PoolPlayer],
    ) -> dict[str, Any]:
        if not pool_players:
            return {
                "status": "unavailable",
                "source": "player_pool",
                "context": None,
                "unavailable_reason": "player_pool_unavailable",
            }
        return {
            "status": "available",
            "source": "player_pool",
            "context": "posted_markets",
            "unavailable_reason": None,
        }

    @classmethod
    def _experience(
        cls,
        *,
        historical: bool,
        schedule_freshness: Mapping[str, Any],
        participants: Mapping[str, Any],
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
        injury_block: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Declare the experience mode and each section's own evidence."""

        return {
            "mode": experience_mode(historical),
            "player_source": player_source(historical),
            "sections": {
                "schedule": cls._schedule_section(historical, schedule_freshness),
                "participants": dict(participants),
                "season_defense": cls._defense_section(
                    availability,
                    "season",
                    context="completed_season" if historical else "pregame",
                    historical_reason=None,
                ),
                "last_15_defense": cls._defense_section(
                    availability,
                    "last_15",
                    context="pregame",
                    historical_reason=(
                        "no_point_in_time_snapshot" if historical else None
                    ),
                ),
                "injuries": cls._injury_section(historical, injury_block),
            },
        }

    @staticmethod
    def _schedule_section(
        historical: bool, schedule_freshness: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Report the catalog that named this game, plus its collection time.

        A completed season's catalog is immutable evidence, so its collection
        age stays provenance rather than an operational warning; the separate
        ``freshness.schedule`` surface keeps its age-based status.
        """

        return {
            "status": "available",
            "source": "event_catalog",
            "context": (
                "completed_season_catalog"
                if historical
                else "current_season_catalog"
            ),
            "unavailable_reason": None,
            "collected_at": schedule_freshness["retrieved_at"],
        }

    @staticmethod
    def _defense_section(
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
        window_name: str,
        *,
        context: str | None,
        historical_reason: str | None,
    ) -> dict[str, Any]:
        """Report a Defense Sheet window from its own Surface evidence.

        The window renders whenever any governed Base is available; the
        per-Base authority stays ``league.surface_availability``.
        """

        states = [availability[base][window_name] for base in DEFENSE_BASES]
        if any(state["status"] == "available" for state in states):
            return {
                "status": "available",
                "source": "team_matchup_publication",
                "context": context,
                "unavailable_reason": None,
            }
        status = (
            "unavailable"
            if any(state["status"] == "unavailable" for state in states)
            else "missing"
        )
        reason = historical_reason or next(
            (
                state["unavailable_reason"]
                for state in states
                if state["unavailable_reason"]
            ),
            "not_stored",
        )
        return {
            "status": status,
            "source": None,
            "context": None,
            "unavailable_reason": reason,
        }

    @staticmethod
    def _injury_section(
        historical: bool, injury_block: Mapping[str, Any]
    ) -> dict[str, Any]:
        if injury_block["status"] in {"fresh", "stale"}:
            # A stopped game only ever serves its retained pre-tip snapshot, so
            # available historical injury evidence is pregame by construction.
            return {
                "status": "available",
                "source": "rotowire",
                "context": "pregame" if historical else "current",
                "unavailable_reason": None,
            }
        if historical:
            # Current injury evidence is never projected backward onto a
            # completed game.
            return {
                "status": "unavailable",
                "source": None,
                "context": None,
                "unavailable_reason": "no_pregame_snapshot",
            }
        return {
            "status": "unavailable",
            "source": "rotowire",
            "context": "current",
            "unavailable_reason": injury_block["unavailable_reason"],
        }

    def _publication_snapshot(self, season: str, *, session: Session | None = None):
        if self.publication_reader is None:
            return None
        snapshot = getattr(self.publication_reader, "snapshot", None)
        if not callable(snapshot):
            snapshot = getattr(self.publication_reader, "read_snapshot", None)
        if not callable(snapshot):
            return None
        keyword = {}
        if accepts_keyword(snapshot, "projection_only_keys"):
            # The season-wide game-log payload is never needed here: the
            # summaries read resolves this game's players from the projection.
            keyword["projection_only_keys"] = _PROJECTION_ONLY_STREAM_KEYS
        if session is not None and accepts_keyword(snapshot, "session"):
            keyword["session"] = session
        return snapshot(_PUBLICATION_STREAM_KEYS, season=season, **keyword)

    def _publication_metadata(
        self,
        season: str,
        publication_snapshot=None,
    ) -> dict[str, Any]:
        """Add the immutable reader's truthful, additive provenance."""

        if publication_snapshot is not None:
            metadata = publication_snapshot.metadata()
        elif self.publication_reader is not None:
            metadata = self.publication_reader.metadata(
                _PUBLICATION_STREAM_KEYS,
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
            # the provider path used before this migration. Statistical
            # activation does not change the Injury Reports contract.
            return self.injuries.get_injuries(
                event=event,
                season=season,
                pool_players=pool_players,
            )
        return unavailable_injury_result("disabled")

    def _event(
        self,
        season: str,
        game_id: str,
        *,
        connection: Connection | None = None,
    ) -> Mapping[str, Any]:
        if self.event_catalog is None:
            raise ProviderUnavailableError(
                "The matchup schedule is currently unavailable."
            )
        event = call_with_read_scope(
            self.event_catalog.get_event, season, game_id, connection=connection
        )
        if event is not None:
            classification = resolve_stored_event_classification(
                game_id,
                str(event.get("classification", event.get("season_type", ""))),
            )
            if classification.kind != "Regular Season":
                raise ResourceNotFoundError(
                    "The requested matchup is outside the Regular Season window."
                )
            return event
        if call_with_read_scope(
            self.event_catalog.count_events, season, connection=connection
        ) == 0:
            raise ProviderUnavailableError(
                "The matchup schedule is currently unavailable."
            )
        raise ResourceNotFoundError("The requested matchup game was not found.")

    def _schedule_freshness(
        self,
        season: str,
        *,
        observed_at: datetime,
        connection: Connection | None = None,
    ) -> dict[str, Any]:
        if self.event_catalog is None:
            return {"status": "missing", "retrieved_at": None}
        observed = call_with_read_scope(
            self.event_catalog.get_freshness,
            season,
            now=observed_at,
            connection=connection,
        )
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
        self,
        season: str,
        *,
        window_games: int | None,
        as_of: date | None,
        publication_snapshot=None,
        connection: Connection | None = None,
    ) -> TeamMatchupWindow | None:
        """Read one Defense Sheet window.

        This is the shared window: `league` and `teams` display it, and #47
        made it the Matchup Score input too, in every mode.
        """

        if self.team_matchups is None:
            return None
        return call_with_read_scope(
            self.team_matchups.get_latest_window,
            season,
            window_games=window_games,
            as_of=as_of,
            publication_snapshot=publication_snapshot,
            connection=connection,
        )

    def _diets(
        self,
        season: str,
        players: Sequence[_Participant],
        *,
        publication_snapshot=None,
        connection: Connection | None = None,
    ) -> PlayerDietResult:
        if self.player_diets is None:
            return PlayerDietResult(season, {}, ())
        return call_with_read_scope(
            self.player_diets.get_for_players,
            season,
            tuple(player.canonical_player_id for player in players),
            publication_snapshot=publication_snapshot,
            connection=connection,
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
        players: Sequence[_Participant],
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
            # One shared season-summary read feeds both the rail's display
            # fields and the Matchup Score inputs, in every mode; #47 made it
            # completed-season evidence -- focal game included -- in
            # historical mode specifically.
            summary = summaries.get(player.canonical_player_id)
            scoring = (
                None
                if summary is None or summary.season_rate is None
                else summary.season_rate.per_game.get("PTS")
            )
            diet_by_base = {base: [] for base in PLAYER_DIET_BASES}
            diet = _PlayerDiet.build(
                diets.players.get(player.canonical_player_id, ()),
                observed_diet_share,
            )
            # One whole-Base verdict per Base, from the same rule a score cell
            # marks itself thin with.  A score component states it only for the
            # slices that component consumed, and only where that market was
            # posted and its window available, so it cannot answer "is this
            # player's Diet for this Base thin" on its own.
            diet_thin = {
                base: diet_evidence_thin(
                    base=base,
                    observed_share=diet.observed_shares[base],
                    selected_facts=diet.by_base[base],
                    summary=summary,
                    settings=self.settings.matchup_scores,
                )
                for base in PLAYER_DIET_BASES
            }
            for fact in diet.facts:
                baseline = diets.baselines.get((fact.base, fact.slice_key))
                league_average_share = (
                    None if baseline is None else baseline.league_average_share
                )
                sigma_deviation = (
                    None if baseline is None else baseline.sigma_deviation(fact.share)
                )
                diet_by_base[fact.base].append(
                    {
                        "key": fact.slice_key,
                        "season": {
                            "share": self._number(fact.share),
                            "volume": self._number(fact.volume),
                            "games_played": fact.games_played,
                            "volume_unit": fact.volume_unit,
                            "league_average_share": (
                                None
                                if league_average_share is None
                                else self._number(league_average_share)
                            ),
                            "sigma_deviation": (
                                None
                                if sigma_deviation is None
                                else self._number(sigma_deviation)
                            ),
                        },
                    }
                )
            scores = self._scores(
                player,
                summary,
                diet,
                event,
                windows,
                metric_indexes,
                availability,
            )
            rows.append(
                {
                    "canonical_id": int(player.canonical_player_id),
                    "name": player.name,
                    "team_id": int(player.team_id),
                    "tricode": player.tricode,
                    "player_source": player.source,
                    "stat_categories": list(player.market_categories),
                    "focal_game_line": player.focal_game_line,
                    "posted_markets": (
                        [] if player.source == GAME_LOG_SOURCE
                        else list(player.market_categories)
                    ),
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
                    "diet_thin": diet_thin,
                    "scores": scores,
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
        player: _Participant,
        summary: PlayerSeasonLogSummary | None,
        diet: _PlayerDiet,
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
        withhold = player.source == GAME_LOG_SOURCE
        return {
            market: {
                window_name: self._presented_window(
                    self._score_window(
                        market,
                        window_name,
                        metric_indexes[window_name],
                        opponent_id,
                        summary,
                        diet,
                        availability,
                        memo,
                    ),
                    withhold,
                )
                for window_name, window in windows.items()
            }
            for market in player.market_categories
        }

    @staticmethod
    def _presented_window(
        window: dict[str, Any], withhold_partial_blend: bool
    ) -> dict[str, Any]:
        """Return one score window as the response carries it.

        A Historical Matchup withholds the Blend unless every input the score
        contract requires was present, so a mean of the surviving Bases is
        never presented as a complete blended score. Component evidence and the
        named gaps still ship. The memoized window keeps its computed Blend, so
        a combo still composes from its parts exactly as it did.
        """

        if not withhold_partial_blend or not window["missing_inputs"]:
            return window
        if window.get("blend") is None:
            return window
        return {**window, "blend": None}

    def _score_window(
        self,
        market: str,
        window_name: str,
        metric_index: _WindowMetricIndex | None,
        opponent_id: int,
        summary: PlayerSeasonLogSummary | None,
        diet: _PlayerDiet,
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
        memo: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        key = (market, window_name)
        if key not in memo:
            window = self._compute_score_window(
                market,
                window_name,
                metric_index,
                opponent_id,
                summary,
                diet,
                availability,
                memo,
            )
            # An unavailable cell names what it lacked instead of leaving the
            # reader to guess why no blend arrived.
            window["missing_inputs"] = self._missing_score_inputs(
                market, window_name, window["components"], summary, availability
            )
            memo[key] = window
        return memo[key]

    @staticmethod
    def _missing_score_inputs(
        market: str,
        window_name: str,
        components: Mapping[str, Any],
        summary: PlayerSeasonLogSummary | None,
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> list[str]:
        """Name every score-contract input this window could not consume."""

        needs_rate = market in _SEASON_RATE_MARKETS
        rate_missing = summary is None or summary.season_rate is None
        missing = ["player_season_rate"] if needs_rate and rate_missing else []
        for base in _SCORE_INPUT_BASES.get(market, ()):
            if availability[base][window_name]["status"] != "available":
                missing.append(f"team_defense:{base}")
            elif base in components or (needs_rate and rate_missing):
                # A named missing Season rate already explains the gap; the
                # Diet evidence behind it was never consulted.
                continue
            elif base == "traditional":
                missing.append("team_defense:traditional")
            else:
                missing.append(f"player_diet:{base}")
        return missing

    def _compute_score_window(
        self,
        market: str,
        window_name: str,
        metric_index: _WindowMetricIndex | None,
        opponent_id: int,
        summary: PlayerSeasonLogSummary | None,
        diet: _PlayerDiet,
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
                diet,
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
                diet=diet,
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
        diet: _PlayerDiet,
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
                diet,
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
        diet: _PlayerDiet,
        availability: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> dict[str, Any] | None:
        if (
            metric_index is None
            or availability[base][window_name]["status"] != "available"
        ):
            return None
        facts = diet.by_base[base]
        observed_share = diet.observed_shares[base]
        if observed_share is None:
            return None
        selected_facts = tuple(
            fact for fact in facts if slice_keys is None or fact.slice_key in slice_keys
        )
        if not selected_facts:
            return None
        if base == "play_types":
            # The Log Workspace rating scores the same crossing, so the
            # arithmetic is stated once in the domain instead of twice here.
            # play_types carries a single stat whose weight cancels inside the
            # opponent/league ratio, so the domain takes bare per-48 values.
            (stat_key,) = stat_weights
            identities = {
                fact.slice_key: (base, fact.slice_key, stat_key)
                for fact in selected_facts
            }
            opponent_metrics = metric_index.teams.get(opponent_id, {})
            value = play_type_matchup(
                {fact.slice_key: fact.share for fact in selected_facts},
                {
                    slice_key: metric.allowed_per_48
                    for slice_key, identity in identities.items()
                    if (metric := opponent_metrics.get(identity)) is not None
                },
                {
                    slice_key: metric.average_allowed_per_48
                    for slice_key, identity in identities.items()
                    if (metric := metric_index.league.get(identity)) is not None
                },
            )
            if value is None:
                return None
        else:
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
            value = total - weight_total
        return {
            "value": self._number(value),
            "thin": diet_evidence_thin(
                base=base,
                observed_share=observed_share,
                selected_facts=selected_facts,
                summary=summary,
                settings=self.settings.matchup_scores,
            ),
        }

    @staticmethod
    def _availability(window: TeamMatchupWindow | None, base: str) -> dict[str, Any]:
        if not window:
            return {"status": "missing", "unavailable_reason": "not_stored"}
        observation = next(
            (item for item in window.observations if item.surface == base), None
        )
        if observation is None:
            return {"status": "missing", "unavailable_reason": "not_stored"}
        state = {
            "status": observation.status,
            "unavailable_reason": observation.unavailable_reason,
        }
        publication = observation.publication
        # Only a read that departed from its ordinary date ordering owes the
        # reader an explanation, so the key stays absent otherwise.
        if publication is not None and publication.reason:
            state["reason"] = publication.reason
        return state

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
                if base == "play_types" and window_name == "last_15":
                    # NBA Synergy exposes no bounded Last-15/date window.
                    # Keep this exact public reason even when an older legacy
                    # snapshot happened to contain a similarly named row.
                    result[base][window_name] = {
                        "status": "unavailable",
                        "unavailable_reason": "provider_window_unsupported",
                    }
                    continue
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
        self,
        season: str,
        *,
        connection: Connection | None = None,
    ) -> dict[str, Any]:
        completed = call_with_read_scope(
            self.stats_freshness.get, connection=connection
        ).last_successful_completion
        status = "missing"
        if completed is not None:
            completed = assume_utc(completed)
            latest_completed_game = call_with_read_scope(
                self.event_catalog.latest_final_scheduled_at,
                season,
                connection=connection,
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
    def _observation_time(
        window: TeamMatchupWindow | None, base: str
    ) -> str | None:
        if not isinstance(window, TeamMatchupWindow):
            return None
        observation = next(
            (item for item in window.observations if item.surface == base), None
        )
        if (
            observation is None
            or observation.status == "missing"
            or observation.retrieved_at is None
            or observation.retrieved_at == datetime.min
        ):
            return None
        return assume_utc(observation.retrieved_at).isoformat()

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


#: The stat key each Diet Base states an *outcome* in.  A Base publishes a
#: Defense Sheet row per stat key, but only some of those rows are things a
#: player produced: a shot zone has an FGM row and an FGA row, and only FGM is
#: production.  Attempt and possession rows (``FGA``, ``FG2A``, ``FG3A``,
#: ``POSS``) are deliberately absent, so a backtest column is always something
#: that happened rather than something that was tried.  Assist locations name
#: the slice as their own stat key, so they are read from the slice rather
#: than listed here.
_BASE_OUTCOME_STAT_KEYS = {
    "play_types": ("PTS",),
    "shot_types": ("FG2M", "FG3M"),
    "shot_zones": ("FGM",),
}


def qualifier_slice_outcome_markets(base: str, slice_key: str) -> tuple[str, ...]:
    """The Stat Categories a Diet slice's outcome rows map to.

    The one mapping from a Qualifier's slice to the box-score columns that
    stand in for it, restricted to the rows that state an outcome: ``Corner
    3`` is points and threes, ``Transition`` is points and the point combos,
    and neither carries the attempts its own Defense Sheet row also reports.

    The mapping itself is ``MatchupService._markets``, so a column here can
    never disagree with the ``markets`` a Defense Sheet row advertises for the
    same slice; what this narrows is which of that slice's rows are asked.
    """

    markets: list[str] = []
    for stat_key in _BASE_OUTCOME_STAT_KEYS.get(base, (slice_key,)):
        for market in MatchupService._markets(base, slice_key, stat_key):
            if market not in markets:
                markets.append(market)
    return tuple(markets)


__all__ = [
    "CURRENT_MODE",
    "DEFENSE_BASES",
    "DEFENSIVE_COLUMNS",
    "HISTORICAL_MODE",
    "MatchupService",
    "diet_evidence_thin",
    "observed_diet_share",
    "qualifier_slice_outcome_markets",
]
