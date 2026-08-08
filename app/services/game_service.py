"""Game service: game logs filtering with a synchronous provider adapter.

Requests are served by threaded Flask workers, so this service is fully
synchronous: there is no per-request event loop.  Provider calls go through
:class:`NBAStatsAdapter`, which keeps ``stats.nba.com`` concurrency bounded by
``Settings.providers.nba_stats_max_concurrency`` (#10).  Filters arrive as one
typed :class:`GameLogQuery` (built by the route or the NL executor) and the
response is a validated :class:`GameLogResponse` whose ``game_logs`` and
``averages`` are ordinary JSON arrays, not pandas JSON strings (#9).
"""

import json
import logging
from dataclasses import dataclass
from difflib import get_close_matches

import pandas as pd
from nba_api.stats.static import teams

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.models.catalogs import (
    ASSIST_LOCATION_FILTERS,
    CATCH_SHOOT_FILTERS,
    LESS_THAN_TEN_FEET_FILTER,
    OPPONENT_FILTERS,
    PLAY_TYPES,
    PULLUP_FILTERS,
)
from app.models.game_logs import GameLogQuery, GameLogResponse
from app.utils.cache_config import get_redis_client, set_cache_with_1am_expiry
from app.utils.database_utils import nba_team_to_abbreviation
from app.utils.tables import normalize_table_name
from app.utils.telemetry import CACHE_DISABLED, CACHE_MISS
from .nba_cache import NBAGameCache
from .nba_stats_adapter import NBAStatsAdapter

logger = logging.getLogger(__name__)


def _records(frame: pd.DataFrame):
    """Convert a DataFrame to JSON-safe dictionaries (empty frame -> [])."""
    if frame is None or frame.shape[0] == 0:
        return []

    return json.loads(frame.to_json(orient="records", date_format="iso"))


@dataclass(frozen=True, slots=True)
class TeamFilterStrategy:
    """Provider/cache policy and handler for one team filter category."""

    category: str
    handler: str
    provider_operation: str | None
    cache_policy: str
    provider_category: str | None = None
    table_name: str | None = None
    sort_column: str | None = None


def _team_filter_strategies() -> dict[str, TeamFilterStrategy]:
    """Build the one canonical classification map used by the filter path."""

    return {
        **{
            team_filter: TeamFilterStrategy(
                category="catch_shoot",
                handler="_shooting_filtering",
                provider_operation="league_opponent_shot_chart",
                cache_policy="daily_nba_data",
                provider_category="Catch and Shoot",
                table_name="catch_and_shoot",
                sort_column={
                    "C&S 3s": "FG3M",
                    "C&S PTS": "PTS",
                    "C&S 3A": "FG3A",
                }[team_filter],
            )
            for team_filter in CATCH_SHOOT_FILTERS
        },
        **{
            team_filter: TeamFilterStrategy(
                category="pullup",
                handler="_shooting_filtering",
                provider_operation="league_opponent_shot_chart",
                cache_policy="daily_nba_data",
                provider_category="Pullups",
                table_name="pullups",
                sort_column={
                    "PU 2s": "FG2M",
                    "PU 3s": "FG3M",
                    "PU PTS": "PTS",
                }[team_filter],
            )
            for team_filter in PULLUP_FILTERS
        },
        **{
            team_filter: TeamFilterStrategy(
                category="opponent",
                handler="general_opp_filtering",
                provider_operation="league_opponent_team_stats",
                cache_policy="daily_nba_data",
            )
            for team_filter in OPPONENT_FILTERS
        },
        **{
            team_filter: TeamFilterStrategy(
                category="play_type",
                handler="playtype_filtering",
                provider_operation=None,
                cache_policy="intraday_computed",
            )
            for team_filter in PLAY_TYPES
        },
        LESS_THAN_TEN_FEET_FILTER: TeamFilterStrategy(
            category="shooting_zone",
            handler="less_than_ten_feet_filtering",
            provider_operation=None,
            cache_policy="intraday_computed",
        ),
        **{
            team_filter: TeamFilterStrategy(
                category="assist_location",
                handler="assist_location_filtering",
                provider_operation=None,
                cache_policy="intraday_computed",
            )
            for team_filter in ASSIST_LOCATION_FILTERS
        },
    }


# A single descriptor map prevents the cache and dispatch paths from growing
# separate, subtly different classifications of the same team filter.
TEAM_FILTER_STRATEGIES = _team_filter_strategies()


class GameService:
    # Whitelist of allowed database tables to prevent SQL injection
    ALLOWED_TABLES = {
        'player_information', 'team_play_types', 'processed_team_assists',
        'processed_player_assists', 'general_opponent_stats', 'catch_and_shoot',
        'pullups', 'less_than_10_ft'
    }

    def __init__(self, db_engine, redis_client=None, settings: RuntimeSettings | None = None):
        self.engine = db_engine
        self.settings = settings or get_runtime_settings()
        self.all_teams = teams.get_teams()

        # Synchronous provider adapter that bounds concurrent stats.nba.com
        # calls with the configured NBA_STATS_MAX_CONCURRENCY limit (#10).
        self.nba_stats = NBAStatsAdapter(settings=self.settings)

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

    @property
    def daily_cache_decorator(self):
        """Get daily cache decorator for this instance"""
        return self.cache.cache_daily_nba_data()

    def _get_game_logs(self, player_name, season=None):
        """Get game logs with daily caching - only hits NBA API once per day"""
        season = season or self.settings.nba.current_season
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
                logger.debug(f"Cache hit for player logs: {player_name}, {season} (avoiding NBA API call)")
                self.nba_stats.record_cache_hit("player_game_logs")
                return cached_result

            # Cache miss - make NBA API call
            logger.info(f"Cache miss for player logs: {player_name}, {season} - making NBA API call")
            result = self._fetch_game_logs_from_api(
                player_name, season, cache_status=CACHE_MISS
            )

            # Cache the result with 1 AM CST expiry for current season data
            if self.cache._is_current_season(season):
                # Current season - use 1 AM CST expiry
                if set_cache_with_1am_expiry(self.cache.redis_client, cache_key, self.cache._serialize_data(result)):
                    logger.info(f"Cached NBA API result for {player_name}, {season} until 1 AM CST tomorrow")
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
                player_name, season, cache_status=cache_status
            )

    def _fetch_game_logs_from_api(
        self, player_name, season=None, *, cache_status=CACHE_MISS
    ):
        season = season or self.settings.nba.current_season
        player_id = self.get_player_id(player_name)

        gamelogs = self.nba_stats.fetch_player_game_logs(
            player_id, season, cache_status=cache_status
        )

        # Clean up columns
        drop_columns = [
            'SEASON_YEAR', 'PLAYER_ID', 'GP_RANK', 'W_RANK', 'L_RANK',
            'W_PCT_RANK', 'MIN_RANK', 'FGM_RANK', 'FGA_RANK', 'FG_PCT_RANK',
            'FG3M_RANK', 'FG3A_RANK', 'FG3_PCT_RANK', 'FTM_RANK', 'FTA_RANK',
            'FT_PCT_RANK', 'OREB_RANK', 'DREB_RANK', 'REB_RANK', 'AST_RANK',
            'TOV_RANK', 'STL_RANK', 'BLK_RANK', 'BLKA_RANK', 'PF_RANK',
            'PFD_RANK', 'PTS_RANK', 'PLUS_MINUS_RANK', 'NBA_FANTASY_PTS_RANK',
            'DD2_RANK', 'TD3_RANK', 'WNBA_FANTASY_PTS_RANK', 'AVAILABLE_FLAG',
            'NICKNAME', 'TEAM_NAME', 'DD2', 'TD3', 'WNBA_FANTASY_PTS',
            'BLKA', 'PFD'
        ]
        gamelogs = gamelogs.drop(drop_columns, axis=1, errors='ignore')

        # The pre-issues-9 contract deliberately leaves next_game unset.  Keep
        # that behavior until a separately specified provider seam exists.
        next_team = None

        # Calculate additional stats
        gamelogs['PRA'] = gamelogs['PTS'] + gamelogs['REB'] + gamelogs['AST']
        gamelogs['PA'] = gamelogs['PTS'] + gamelogs['AST']
        gamelogs['PR'] = gamelogs['PTS'] + gamelogs['REB']
        gamelogs['RA'] = gamelogs['REB'] + gamelogs['AST']
        gamelogs['STKS'] = gamelogs['STL'] + gamelogs['BLK']
        gamelogs['FD_PTS'] = gamelogs['NBA_FANTASY_PTS']
        gamelogs['+/-'] = gamelogs['PLUS_MINUS']
        gamelogs['MIN'] = gamelogs['MIN'].round().astype(int)
        gamelogs['FG2M'] = gamelogs['FGM'] - gamelogs['FG3M']
        gamelogs['FG2A'] = gamelogs['FGA'] - gamelogs['FG3A']

        gamelogs['GAME_DATE'] = gamelogs['GAME_DATE'].astype(str)

        return gamelogs, next_team

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
        """Find games to exclude due to filtering"""
        season = season or self.settings.nba.current_season
        exclude_game_ids = set()

        # Loop through players_off and union game IDs
        for player_name in players_off_names:
            player_gamelogs, _ = self._get_game_logs(player_name, season)
            player_game_ids = set(player_gamelogs['GAME_ID'])

            # Union with exclude_game_ids to accumulate games where any player_off played
            exclude_game_ids |= player_game_ids

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

        # Apply playstyle filter
        if query.playstyle_range != (0.0, 200.0):
            min_rating, max_rating = query.playstyle_range
            if 'PLAYTYPE_RTG' in df.columns:
                df = df[(df['PLAYTYPE_RTG'] >= min_rating) & (df['PLAYTYPE_RTG'] <= max_rating)]

        # Apply players on/off filter
        if query.players_on or query.players_off:
            df = self.filter_players_on_off(
                df, query.players_on, query.players_off, query.season_filter
            )

        # Apply typed self filters.  HTTP ``min,max`` ranges have already been
        # normalized to ``between`` by GameLogQuery; NLP operator semantics
        # remain exact here rather than being reduced to an implicit range.
        for self_filter in query.self_filters.values():
            stat = self_filter.stat
            if stat not in df.columns:
                continue
            values = df[stat]
            if self_filter.operator == "gte":
                df = df[values >= self_filter.value]
            elif self_filter.operator == "gt":
                df = df[values > self_filter.value]
            elif self_filter.operator == "lt":
                df = df[values < self_filter.value]
            elif self_filter.operator == "lte":
                df = df[values <= self_filter.value]
            elif self_filter.operator == "eq":
                df = df[values == self_filter.value]
            elif self_filter.operator == "between":
                df = df[
                    (values >= self_filter.value)
                    & (values <= self_filter.value2)
                ]

        # Apply last games filter
        if query.game_filter:
            df = df.head(query.game_filter)

        return df

    def get_filtered_logs(self, player_name, query: GameLogQuery):
        """Get filtered game logs based on one typed :class:`GameLogQuery`."""
        full_game_logs, next_team = self._get_game_logs(player_name, query.season_filter)

        # Resolve opponent filters into one intersecting team set.
        resolved_teams = None
        if query.teams_against:
            for index, team_filter in enumerate(query.teams_against):
                matching = set(
                    self.filter_teams(team_filter, query.rank_filter[index], str(query.date_filter) if query.date_filter else None)
                )
                resolved_teams = matching if resolved_teams is None else resolved_teams & matching
            resolved_teams = resolved_teams or set()

        filtered_logs = self.apply_filters(full_game_logs.copy(), query, teams_against=resolved_teams)

        # Calculate statistics
        average_columns = ['MIN', 'PTS', 'REB', 'AST', 'PRA', 'PA', 'PR', 'RA',
                         'FD_PTS', 'FGM', 'FGA', 'FG_PCT', 'FG2A', 'FG2M', 'FG3M', 'FG3A', 'FTM', 'FTA',
                         'OREB', 'DREB', 'TOV', 'STL', 'BLK', 'PF', 'STKS', 'PLUS_MINUS']
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
        filtered_logs.drop(['PLAYER_NAME', 'GAME_ID', 'NBA_FANTASY_PTS', 'FT_PCT', 'PLUS_MINUS', 'MIN_SEC', 'TEAM_ID', 'TEAM_ABBREVIATION'], axis=1, inplace=True, errors='ignore')
        filtered_logs['GAME_DATE'] = filtered_logs['GAME_DATE'].astype(str)

        result = GameLogResponse(
            game_logs=_records(filtered_logs),
            averages=filtered_average_rows,
            season_averages=season_average_rows,
            next_game=self._get_team_name_by_id(next_team),
        )
        return result.model_dump()

    def filter_teams(self, team_filter, rank_filter, date_filter=None):
        """Filter teams using the central strategy and its cache policy."""
        strategy = TEAM_FILTER_STRATEGIES.get(team_filter)
        if strategy is None:
            raise ValueError(f"Unsupported team filter: {team_filter!r}")

        if strategy.provider_operation and date_filter is not None:
            return self._filter_teams_with_api_calls(
                team_filter, strategy, rank_filter, date_filter
            )
        return self._filter_teams_computed(
            team_filter, strategy, rank_filter, date_filter
        )

    def _filter_teams_with_api_calls(
        self,
        team_filter,
        strategy: TeamFilterStrategy,
        rank_filter,
        date_filter=None,
    ):
        """Filter provider-backed teams with the descriptor's daily cache."""
        if self.cache.enabled:
            from ..utils.cache_config import CACHE_PREFIXES

            # Cache with date component since NBA API data changes daily
            cache_key = self.cache._generate_key(
                CACHE_PREFIXES['team_stats_daily'],
                True,  # include_date
                'filter_teams_api',
                team_filter, rank_filter, str(date_filter)
            )

            cached_result = self.cache.get(cache_key)
            if cached_result is not None:
                logger.debug(
                    "Cache hit for team filter: %s (avoiding NBA API call)",
                    team_filter,
                )
                if strategy.provider_operation is not None:
                    self.nba_stats.record_cache_hit(strategy.provider_operation)
                return cached_result

            logger.info(
                "Cache miss for team filter: %s - making NBA API call", team_filter
            )
            result = self._filter_teams_uncached(
                team_filter, rank_filter, date_filter, cache_status=CACHE_MISS
            )

            # Use 1 AM CST expiry for daily NBA data
            if set_cache_with_1am_expiry(self.cache.redis_client, cache_key, self.cache._serialize_data(result)):
                logger.info(
                    "Cached team filter result for %s until 1 AM CST tomorrow",
                    team_filter,
                )
            else:
                # Fallback to regular TTL
                ttl = self.cache._get_ttl(strategy.cache_policy)
                self.cache.set(cache_key, result, ttl)

            return result
        else:
            return self._filter_teams_uncached(
                team_filter, rank_filter, date_filter, cache_status=CACHE_DISABLED
            )

    def _filter_teams_computed(
        self,
        team_filter,
        strategy: TeamFilterStrategy,
        rank_filter,
        date_filter=None,
    ):
        """Filter teams using computed/database data and its cache policy."""
        if self.cache.enabled:
            from ..utils.cache_config import CACHE_PREFIXES

            cache_key = self.cache._generate_key(
                CACHE_PREFIXES['computed'],
                False,  # include_date
                'filter_teams_computed',
                team_filter, rank_filter, str(date_filter)
            )

            cached_result = self.cache.get(cache_key)
            if cached_result is not None:
                logger.debug("Cache hit for computed team filter: %s", team_filter)
                return cached_result

            result = self._filter_teams_uncached(
                team_filter, rank_filter, date_filter, cache_status=CACHE_MISS
            )

            ttl = self.cache._get_ttl(strategy.cache_policy)
            self.cache.set(cache_key, result, ttl)

            return result
        else:
            return self._filter_teams_uncached(
                team_filter, rank_filter, date_filter, cache_status=CACHE_DISABLED
            )

    def _filter_teams_uncached(
        self,
        team_filter,
        rank_filter,
        date_filter=None,
        *,
        cache_status=CACHE_MISS,
    ):
        strategy = TEAM_FILTER_STRATEGIES.get(team_filter)
        if strategy is None:
            raise ValueError(f"Unsupported team filter: {team_filter!r}")
        handler = getattr(self, strategy.handler)
        if strategy.category in {"catch_shoot", "pullup", "opponent"}:
            df = handler(team_filter, date_filter, cache_status=cache_status)
        else:
            df = handler(team_filter)

        if rank_filter >= 0:
            return df.head(rank_filter)['team'].tolist()
        else:
            return df.tail(-rank_filter)['team'].tolist()

    def _fetch_data_from_table(self, table_name):
        """Helper method to fetch data from database table with caching"""
        # Validate table name to prevent SQL injection
        if table_name not in self.ALLOWED_TABLES:
            raise ValueError(f"Invalid table name: {table_name}. Allowed tables: {list(self.ALLOWED_TABLES)}")

        cache_key = None

        # Check cache first for static tables
        if self.cache and self.cache.enabled and table_name in ['player_information', 'team_play_types', 'processed_team_assists', 'processed_player_assists']:
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
        if self.cache and self.cache.enabled and table_name in ['player_information', 'team_play_types', 'processed_team_assists', 'processed_player_assists'] and cache_key:
            ttl = self.cache._get_ttl('player_info')  # Use longer TTL for static data
            self.cache.set(cache_key, result, ttl)
            logger.debug(f"Cached table data for {table_name}")

        return result

    # Function that returns general opponent stats sorted by the filter
    def general_opp_filtering(
        self, team_filter, date_filter, *, cache_status=CACHE_MISS
    ):
        if date_filter is not None:
            date_filter = pd.to_datetime(date_filter)
            df = self.nba_stats.fetch_opponent_team_stats(
                date_filter, cache_status=cache_status
            )
        else:
            df = self._fetch_data_from_table('general_opponent_stats')
        df['OPP_STOCKS'] = df['OPP_BLK'] + df['OPP_STL']
        df['team'] = df['TEAM_NAME'].apply(nba_team_to_abbreviation)
        return df.sort_values(by=team_filter, ascending=False)

    # Function that returns opponent playtype data sorted
    def playtype_filtering(self, team_filter):
        df = self._fetch_data_from_table('team_play_types')
        return df.sort_values(by=team_filter, ascending=False)

    def _shooting_filtering(
        self,
        team_filter,
        date_filter,
        *,
        cache_status=CACHE_MISS,
    ):
        """Apply one parameterized opponent shooting-type pipeline."""
        strategy = TEAM_FILTER_STRATEGIES[team_filter]
        if date_filter is not None:
            date_filter = pd.to_datetime(date_filter)
            df = self.nba_stats.fetch_opponent_shot_chart(
                strategy.provider_category,
                date_filter,
                cache_status=cache_status,
            )
        else:
            df = self._fetch_data_from_table(strategy.table_name)
        df['PTS'] = df['FG3M'] * 3 + df['FG2M'] * 2
        df['team'] = df['TEAM_ABBREVIATION']
        return df.sort_values(by=strategy.sort_column, ascending=False)

    def less_than_ten_feet_filtering(self, team_filter):
        del team_filter
        df = self._fetch_data_from_table('less_than_10_ft')
        df.sort_values(by='FG2M', ascending=False, inplace=True)
        df['team'] = df['TEAM_ABBREVIATION']
        return df

    def assist_location_filtering(self, team_filter):
        df = self._fetch_data_from_table('processed_team_assists')
        df.sort_values(by=team_filter, ascending=False, inplace=True)
        df['team'] = df['Name']
        return df

    # Function to find team name by ID
    def _get_team_name_by_id(self, team_id):
        for team in self.all_teams:
            if team['id'] == team_id:
                return team['full_name']
        return None

    #Rating functions: currently unused
    """def calculate_matchup_rating(self, player_name, team):
        teams_df = self._fetch_data_from_table('team_play_types')
        players_df = self._fetch_data_from_table('player_play_types')

        playtypes = ['Cut', 'Isolation', 'PRRollMan', 'PRBallHandler', 'OffRebound',
                    'Spotup', 'Handoff', 'OffScreen', 'Misc', 'Postup', 'Transition']

        player_columns = [playtype + '%' for playtype in playtypes]
        team_columns = playtypes

        player_data = players_df.loc[players_df['PLAYER_NAME'] == player_name, player_columns]
        team_data = teams_df.loc[teams_df['team'] == team, team_columns]

        matchupRTG = (player_data.values * team_data.values).sum()
        return round(matchupRTG, 2)

    def calculate_assist_location_rating(self, player_name, team):
        teams_df = self._fetch_data_from_table('processed_team_assists')
        players_df = self._fetch_data_from_table('processed_player_assists')

        cats = ["Arc3Assists", "Corner3Assists", "AtRimAssists",
               "ShortMidRangeAssists", "LongMidRangeAssists"]

        player_data = players_df.loc[players_df['Name'] == player_name, cats]
        team_data = teams_df.loc[teams_df['Name'] == team, cats]

        matchupRTG = (player_data.values * team_data.values).sum()
        return round(matchupRTG, 2)"""
