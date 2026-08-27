"""Game service: game-log filtering with no provider client of its own.

Requests are served by threaded Flask workers, so this service is fully
synchronous: there is no per-request event loop.  This service holds no
provider client: player game logs come from the injected
:mod:`app.services.game_logs_source` seam -- which decides for itself whether a
season is served from durable facts or a cached live call -- and Team Filters
rank opponents from the Season publications through
:class:`~app.services.team_filter_rankings.TeamFilterRankingService` (#198).
Filters arrive as one typed :class:`GameLogQuery` (built by the route or the NL
executor) and the response is a validated :class:`GameLogResponse` whose
``game_logs`` and ``averages`` are ordinary JSON arrays, not pandas JSON
strings (#9).
"""

import json
import logging
from difflib import get_close_matches

import pandas as pd
from nba_api.stats.static import teams

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.domain.play_type_matchup import play_type_matchup
from app.models.game_logs import GameLogQuery, GameLogResponse
from app.utils.cache_config import get_redis_client, set_cache_with_1am_expiry
from app.utils.tables import normalize_table_name
from app.utils.telemetry import CACHE_DISABLED, CACHE_MISS
from .nba_cache import NBAGameCache
from .team_filter_rankings import TEAM_FILTER_RANKINGS

logger = logging.getLogger(__name__)

# The Log Workspace rating crosses the player's Season play-type Diet with the
# opponent's Season play-type window.  Synergy publishes a single scoring stat
# per play type, so the crossing reads exactly the ``PTS`` metric the Matchup
# page reads.
_PLAY_TYPE_BASE = "play_types"
_PLAY_TYPE_STAT_KEY = "PTS"
_DEFAULT_PLAYSTYLE_RANGE = (0.0, 200.0)


def _records(frame: pd.DataFrame):
    """Convert a DataFrame to JSON-safe dictionaries (empty frame -> [])."""
    if frame is None or frame.shape[0] == 0:
        return []

    return json.loads(frame.to_json(orient="records", date_format="iso"))


class GameService:
    # Whitelist of allowed database tables to prevent SQL injection
    ALLOWED_TABLES = {'player_information'}

    def __init__(
        self,
        db_engine,
        redis_client=None,
        settings: RuntimeSettings | None = None,
        game_logs_source=None,
        player_diets=None,
        team_matchups=None,
        team_filter_rankings=None,
    ):
        self.engine = db_engine
        self.settings = settings or get_runtime_settings()
        self.all_teams = teams.get_teams()

        # Request-time game-log source.  Production injects the database-first
        # source; there is no provider fallback to reach from here.
        self.game_logs_source = game_logs_source

        # Season Rankings for every ``teams_against`` filter.  Absent against
        # the read-only demo database, which ranks nothing rather than calling
        # a provider.
        self.team_filter_rankings = team_filter_rankings

        # Governed rating inputs.  Both are absent against the read-only demo
        # database, which leaves PLAYTYPE_RTG null rather than failing.
        self.player_diets = player_diets
        self.team_matchups = team_matchups

        # Initialize cache
        if redis_client is None:
            redis_client = get_redis_client(self.settings)
        self.cache = NBAGameCache(redis_client, settings=self.settings)

        logger.info(f"GameService initialized with cache {'enabled' if self.cache and self.cache.enabled else 'disabled'}")

    def get_player_id(self, player_name):
        player_dict = self._fetch_data_from_table('player_information')

        player_names = player_dict['full_name'].tolist()
        closest_match = get_close_matches(player_name, player_names, n=1, cutoff=0.8)
        if closest_match:
            player = player_dict[player_dict['full_name'] == closest_match[0]]
            return player['id'].values[0]
        else:
            raise ValueError(f"No matching player found for {player_name}.")

    @property
    def cache_decorator(self):
        """Get cache decorator for this instance"""
        return self.cache.cache_player_logs()

    def _get_game_logs(self, player_name, season=None):
        """Get game logs with daily caching - only hits the source once per day"""
        season = season or self.settings.nba.current_season
        game_logs_plan = None
        source_is_cacheable = True
        if self.game_logs_source is not None:
            prepare = getattr(type(self.game_logs_source), "prepare", None)
            if callable(prepare):
                game_logs_plan = prepare(self.game_logs_source, season)
                source_is_cacheable = game_logs_plan.cacheable
            else:
                source_is_cacheable = self.game_logs_source.cached(season)
        if self.game_logs_source is not None and not source_is_cacheable:
            # A durably complete season is served straight from the database;
            # Redis caching would only shadow the fresher stored facts.
            return self._fetch_game_logs_from_api(
                player_name,
                season,
                cache_status=CACHE_DISABLED,
                game_logs_plan=game_logs_plan,
            )
        cache_status = (
            CACHE_DISABLED
            if not self.cache or not self.cache.enabled
            else CACHE_MISS
        )
        # Apply caching manually since we can't use decorators on dynamic methods
        if self.cache and hasattr(self.cache, 'enabled') and self.cache.enabled:
            from ..utils.cache_config import CACHE_PREFIXES

            # Determine cache strategy based on season
            if self.cache._is_current_season(season):
                # Current season - cache until 4 AM ET next day with date key
                cache_key = self.cache._generate_key(
                    CACHE_PREFIXES['player_logs_daily'],
                    True,  # include_date
                    '_fetch_game_logs_from_api',
                    player_name, season
                )
                cache_type = 'daily_nba_data'
            else:
                # Historical season - cache for 30 days without date key
                cache_key = self.cache._generate_key(
                    CACHE_PREFIXES['season_data'],
                    False,  # include_date
                    '_fetch_game_logs_from_api',
                    player_name, season
                )
                cache_type = 'season_historical'

            # Check cache first
            cached_result = self.cache.get(cache_key)
            if cached_result is not None:
                logger.debug(f"Cache hit for player logs: {player_name}, {season} (avoiding provider call)")
                self.game_logs_source.record_cache_hit("player_game_logs")
                return cached_result

            # Cache miss - make provider call
            logger.info(f"Cache miss for player logs: {player_name}, {season} - making provider call")
            result = self._fetch_game_logs_from_api(
                player_name,
                season,
                cache_status=CACHE_MISS,
                game_logs_plan=game_logs_plan,
            )

            # Cache the result with 1 AM CST expiry for current season data
            if self.cache._is_current_season(season):
                # Current season - use 1 AM CST expiry
                if set_cache_with_1am_expiry(self.cache.redis_client, cache_key, self.cache._serialize_data(result)):
                    logger.info(f"Cached game logs for {player_name}, {season} until 1 AM CST tomorrow")
                else:
                    # Fallback to regular TTL
                    ttl = self.cache._get_ttl(cache_type)
                    self.cache.set(cache_key, result, ttl)
            else:
                # Historical season - use regular TTL
                ttl = self.cache._get_ttl(cache_type)
                self.cache.set(cache_key, result, ttl)

            return result
        else:
            # No cache - direct API call
            return self._fetch_game_logs_from_api(
                player_name,
                season,
                cache_status=cache_status,
                game_logs_plan=game_logs_plan,
            )

    def _fetch_game_logs_from_api(
        self,
        player_name,
        season=None,
        *,
        cache_status=CACHE_DISABLED,
        game_logs_plan=None,
    ):
        season = season or self.settings.nba.current_season
        if self.game_logs_source is None:
            raise RuntimeError(
                "GameService needs an injected game-log source; there is no "
                "request-time provider fallback"
            )
        player_id = int(self.get_player_id(player_name))

        # The pre-issues-9 contract deliberately leaves next_game unset.  Keep
        # that behavior until a separately specified provider seam exists.
        if game_logs_plan is not None:
            return game_logs_plan.get_player_logs(
                player_id, season, cache_status=cache_status
            ), None
        return self.game_logs_source.get_player_logs(
            player_id, season, cache_status=cache_status
        ), None

    def get_common_games(self, primary_player_logs, other_players_names, season=None):
        """Find common games between players"""
        season = season or self.settings.nba.current_season
        primary_game_team_pairs = set(zip(primary_player_logs['GAME_ID'], primary_player_logs['TEAM_ABBREVIATION']))

        # Loop through other players and find intersections based on game IDs and team abbreviations
        for player_name in other_players_names:
            # Reuse existing cached game logs function - returns tuple (gamelogs, next_team)
            player_gamelogs, _ = self._get_game_logs(player_name, season)
            player_game_team_pairs = set(zip(player_gamelogs['GAME_ID'], player_gamelogs['TEAM_ABBREVIATION']))

            primary_game_team_pairs = primary_game_team_pairs.intersection(player_game_team_pairs)

            if not primary_game_team_pairs:
                break

        common_game_ids = {pair[0] for pair in primary_game_team_pairs}
        return set(common_game_ids)

    def get_games_to_exclude(self, player_logs, players_off_names, season=None):
        """Find same-team games where any named player appeared."""
        season = season or self.settings.nba.current_season
        exclude_game_ids = set()
        primary_game_team_pairs = set(zip(
            player_logs['GAME_ID'], player_logs['TEAM_ABBREVIATION']
        ))

        # Union same-team appearances for every player named as "off".
        for player_name in players_off_names:
            player_gamelogs, _ = self._get_game_logs(player_name, season)
            player_game_team_pairs = set(zip(
                player_gamelogs['GAME_ID'], player_gamelogs['TEAM_ABBREVIATION']
            ))
            exclude_game_ids |= {
                game_id for game_id, _ in
                primary_game_team_pairs.intersection(player_game_team_pairs)
            }

        return exclude_game_ids

    def filter_players_on_off(self, df, players_on, players_off, season):
        """Filter games based on players on/off"""
        if players_on:
            common_games = self.get_common_games(df, players_on, season)
            df = df[df['GAME_ID'].isin(common_games)]

        if players_off:
            exclude_games = self.get_games_to_exclude(df, players_off, season)
            df = df[~df['GAME_ID'].isin(exclude_games)]

        return df

    def apply_filters(self, df, query, teams_against=None):
        """Apply all filters to game logs dataframe using one typed query."""
        min_filter, max_filter = query.minutes_filter
        df = df[(df['MIN'] >= min_filter) & (df['MIN'] <= max_filter)]

        # Apply date filter
        if query.date_filter:
            game_dates = pd.to_datetime(df['GAME_DATE'], errors='coerce')
            df = df[game_dates >= pd.Timestamp(query.date_filter)]

        # Apply location filter
        if query.location_filter != 'Both':
            matchup = df['MATCHUP'].astype(str)
            if query.location_filter == 'Home':
                df = df[~matchup.str.contains('@')]
            else:
                df = df[matchup.str.contains('@')]

        # Apply teams-against filter (resolved opponent set from the query).
        # None means the query had no opponent filter; an empty set is a
        # resolved-but-emptied filter and must match zero games, not all games.
        if teams_against is not None:
            # Extract opponent team ("LAL@ HOU" -> "HOU"), then keep matches
            opponents = df['MATCHUP'].astype(str).str.extract(r'(?:vs\.|@)\s*([A-Z]{2,3})')[0]
            df = df[opponents.isin(teams_against)]

        # Apply playstyle filter.  A rating-less row (no play-type facts for
        # the player or the opponent) is NaN, so a non-default range excludes
        # it rather than scoring it as neutral.
        if query.playstyle_range != _DEFAULT_PLAYSTYLE_RANGE:
            min_rating, max_rating = query.playstyle_range
            df = df[(df['PLAYTYPE_RTG'] >= min_rating) & (df['PLAYTYPE_RTG'] <= max_rating)]

        # Apply players on/off filter
        if query.players_on or query.players_off:
            df = self.filter_players_on_off(
                df, query.players_on, query.players_off, query.season_filter
            )

        # Apply typed self filters.  HTTP ``min,max`` ranges have already been
        # normalized to ``between`` by GameLogQuery; NLP operator semantics
        # remain exact here rather than being reduced to an implicit range.
        for self_filter in query.self_filters:
            stat = self_filter.stat
            if stat not in df.columns:
                continue
            df = df[self_filter.apply(df[stat])]

        # Apply last games filter
        if query.game_filter:
            df = df.head(query.game_filter)

        return df

    def _with_playtype_rating(self, df, player_name, season):
        """Return a copy of the frame carrying one PLAYTYPE_RTG per row.

        The rating depends only on (player, opponent), so the governed facts
        are read once per request and every row is scored from that map.
        """
        df = df.copy()
        ratings = self._playtype_ratings(player_name, season)
        if not ratings or 'MATCHUP' not in df.columns:
            df['PLAYTYPE_RTG'] = pd.Series(
                float('nan'), index=df.index, dtype='float64'
            )
            return df

        by_abbreviation = {
            team['abbreviation']: ratings[team['id']]
            for team in self.all_teams
            if team['id'] in ratings
        }
        opponents = df['MATCHUP'].astype(str).str.extract(
            r'(?:vs\.|@)\s*([A-Z]{2,3})'
        )[0]
        df['PLAYTYPE_RTG'] = pd.to_numeric(
            opponents.map(by_abbreviation), errors='coerce'
        )
        return df

    def _playtype_ratings(self, player_name, season):
        """Map every opponent team id to its PLAYTYPE_RTG for this player.

        One Diet read plus one team-window read; ``100`` is a league-average
        matchup.  A team whose crossing cannot be evidenced is simply absent.
        """
        if self.player_diets is None or self.team_matchups is None:
            return {}

        player_id = int(self.get_player_id(player_name))
        diets = self.player_diets.get_for_players(season, (player_id,))
        shares = {
            fact.slice_key: fact.share
            for fact in diets.players.get(player_id, ())
            if fact.base == _PLAY_TYPE_BASE
        }
        if not shares:
            return {}

        window = self.team_matchups.get_latest_window(season)
        if window is None:
            return {}
        league_allowed = self._play_type_allowed(
            window.league_metrics, 'average_allowed_per_48'
        )
        ratings = {}
        for team_id, metrics in window.team_metrics.items():
            matchup = play_type_matchup(
                shares,
                self._play_type_allowed(metrics, 'allowed_per_48'),
                league_allowed,
            )
            if matchup is not None:
                ratings[team_id] = round(100 * (1 + matchup), 1)
        return ratings

    @staticmethod
    def _play_type_allowed(metrics, attribute):
        return {
            metric.slice_key: getattr(metric, attribute)
            for metric in metrics
            if metric.base == _PLAY_TYPE_BASE
            and metric.stat_key == _PLAY_TYPE_STAT_KEY
        }

    def get_filtered_logs(self, player_name, query: GameLogQuery):
        """Get filtered game logs based on one typed :class:`GameLogQuery`."""
        full_game_logs, next_team = self._get_game_logs(player_name, query.season_filter)
        full_game_logs = self._with_playtype_rating(
            full_game_logs, player_name, query.season_filter
        )

        # Resolve opponent filters into one intersecting team set, read from
        # one publication generation so two filters cannot disagree about which
        # season and which activation they ranked.
        resolved_teams = None
        if query.teams_against:
            rankings = self._season_rankings(
                query.teams_against, query.season_filter
            )
            for index, team_filter in enumerate(query.teams_against):
                matching = set(
                    self._select_rank(
                        rankings[team_filter], query.rank_filter[index]
                    )
                )
                resolved_teams = matching if resolved_teams is None else resolved_teams & matching
            resolved_teams = resolved_teams or set()

        filtered_logs = self.apply_filters(full_game_logs.copy(), query, teams_against=resolved_teams)

        # Calculate statistics
        average_columns = ['MIN', 'PTS', 'REB', 'AST', 'PRA', 'PA', 'PR', 'RA',
                         'FD_PTS', 'FGM', 'FGA', 'FG_PCT', 'FG2A', 'FG2M', 'FG3M', 'FG3A', 'FTM', 'FTA',
                         'OREB', 'DREB', 'TOV', 'STL', 'BLK', 'PF', 'STKS']
        filtered_logs['GAME_DATE'] = pd.to_datetime(filtered_logs['GAME_DATE'], errors='coerce').dt.date
        if not filtered_logs.empty:
            filtered_averages = filtered_logs[average_columns].mean().round(2)
            filtered_average_rows = _records(filtered_averages.to_frame().T)
        else:
            filtered_average_rows = []
        season_average_rows = []
        if not full_game_logs.empty:
            season_averages = full_game_logs[average_columns].mean().round(2)
            season_average_rows = _records(season_averages.to_frame().T)
        filtered_logs.drop(['PLAYER_NAME', 'PLAYER_ID', 'GAME_ID', 'NBA_FANTASY_PTS', 'FT_PCT', 'PLUS_MINUS', '+/-', 'MIN_SEC', 'TEAM_ID', 'TEAM_ABBREVIATION'], axis=1, inplace=True, errors='ignore')
        filtered_logs['GAME_DATE'] = filtered_logs['GAME_DATE'].astype(str)

        result = GameLogResponse(
            game_logs=_records(filtered_logs),
            averages=filtered_average_rows,
            season_averages=season_average_rows,
            next_game=self._get_team_name_by_id(next_team),
        )
        return result.model_dump()

    def filter_teams(self, team_filter, rank_filter, season):
        """Select the top-N or bottom-N opponents by Season Rankings.

        The rankings are whole-Regular-Season aggregates for the requested
        season, so ``date_filter`` deliberately takes no part here: a date
        trims the player's own logs without reshaping which opponents rank
        where.  The read is not cached: an activation, a rollback, or a season
        rollover must never be shadowed by a previous generation's ranking.
        """

        ranked = self._season_rankings((team_filter,), season)[team_filter]
        return self._select_rank(ranked, rank_filter)

    def _season_rankings(self, team_filters, season):
        """Rank every requested Team Filter from one publication generation."""

        for team_filter in team_filters:
            if team_filter not in TEAM_FILTER_RANKINGS:
                raise ValueError(f"Unsupported team filter: {team_filter!r}")
        if self.team_filter_rankings is None:
            logger.warning(
                "Team Filters %s cannot rank without Season publications",
                list(team_filters),
            )
            return {team_filter: [] for team_filter in team_filters}
        return self.team_filter_rankings.rank_all(tuple(team_filters), season)

    @staticmethod
    def _select_rank(ranked, rank_filter):
        if rank_filter >= 0:
            return ranked[:rank_filter]
        return ranked[rank_filter:]

    def _fetch_data_from_table(self, table_name):
        """Helper method to fetch data from database table with caching"""
        # Validate table name to prevent SQL injection
        if table_name not in self.ALLOWED_TABLES:
            raise ValueError(f"Invalid table name: {table_name}. Allowed tables: {list(self.ALLOWED_TABLES)}")

        cache_key = None

        # Check cache first for static tables
        if self.cache and self.cache.enabled:
            cache_key = self.cache._generate_key('table_data', False, table_name)
            cached_result = self.cache.get(cache_key)
            if cached_result is not None:
                logger.debug(f"Cache hit for table: {table_name}")
                return cached_result

        # Fetch from database
        normalized = normalize_table_name(table_name)
        query = f"SELECT * FROM {normalized}"
        with self.engine.connect() as conn:
            result = pd.read_sql(query, conn)

        # Cache static tables for longer periods
        if cache_key:
            ttl = self.cache._get_ttl('player_info')  # Use longer TTL for static data
            self.cache.set(cache_key, result, ttl)
            logger.debug(f"Cached table data for {table_name}")

        return result

    # Function to find team name by ID
    def _get_team_name_by_id(self, team_id):
        for team in self.all_teams:
            if team['id'] == team_id:
                return team['full_name']
        return None
