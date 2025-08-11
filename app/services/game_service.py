import pandas as pd
from nba_api.stats import endpoints
from nba_api.stats.endpoints import playergamelogs
from nba_api.stats.static import teams
from ..utils.database_utils import get_opponent_team, nba_team_to_abbreviation, get_player_id, calculate_additional_stats
from ..utils.tables import normalize_table_name
from difflib import get_close_matches
from .nba_cache import NBAGameCache
from ..utils.cache_config import get_redis_client, set_cache_with_1am_expiry
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class GameService:
    # Whitelist of allowed database tables to prevent SQL injection
    ALLOWED_TABLES = {
        'player_information', 'team_play_types', 'processed_team_assists', 
        'processed_player_assists', 'general_opponent_stats', 'catch_and_shoot',
        'pullups', 'less_than_10_ft'
    }
    
    def __init__(self, db_engine, redis_client=None):
        self.engine = db_engine
        self.all_teams = teams.get_teams()
        
        # Initialize cache
        if redis_client is None:
            redis_client = get_redis_client()
        self.cache = NBAGameCache(redis_client)
        
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

    def _fetch_data_from_table(self, table_name):
        # Validate table name to prevent SQL injection
        if table_name not in self.ALLOWED_TABLES:
            raise ValueError(f"Invalid table name: {table_name}. Allowed tables: {list(self.ALLOWED_TABLES)}")
        
        # Normalize legacy names to Postgres-friendly snake_case
        table_name = normalize_table_name(table_name)
        query = f"SELECT * FROM {table_name}"
        with self.engine.connect() as conn:
            return pd.read_sql(query, conn)

    @property
    def cache_decorator(self):
        """Get cache decorator for this instance"""
        return self.cache.cache_player_logs()
    

    @property  
    def daily_cache_decorator(self):
        """Get daily cache decorator for this instance"""
        return self.cache.cache_daily_nba_data()
    
    def _get_game_logs(self, player_name, season='2024-25'):
        """Get game logs with daily caching - only hits NBA API once per day"""
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
                return cached_result
            
            # Cache miss - make NBA API call
            logger.info(f"Cache miss for player logs: {player_name}, {season} - making NBA API call")
            result = self._fetch_game_logs_from_api(player_name, season)
            
            # Cache the result with 1 AM CST expiry for current season data
            if self.cache._is_current_season(season):
                # Current season - use 1 AM CST expiry
                if set_cache_with_1am_expiry(self.cache.redis, cache_key, self.cache._serialize(result)):
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
            return self._fetch_game_logs_from_api(player_name, season)
    
    def _fetch_game_logs_from_api(self, player_name, season='2024-25'):
        player_id = self.get_player_id(player_name)
        gamelogs = endpoints.playergamelogs.PlayerGameLogs(
            player_id_nullable=player_id, 
            season_nullable=season
        ).get_data_frames()[0]
        
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
        gamelogs = gamelogs.drop(drop_columns, axis=1)
        try:
            next_game = endpoints.PlayerNextNGames(number_of_games=1,player_id=player_id).get_data_frames()[0]
            team1, team2 = next_game.loc[0,'VISITOR_TEAM_ID'], next_game.loc[0,'HOME_TEAM_ID']
            if team1 != gamelogs.loc[0, 'TEAM_ID']:
                next_team = team1
            else:
                next_team = team2
        except (Exception, IndexError, KeyError) as e:
            logger.warning(f"Failed to fetch next game for player {player_id}: {str(e)}")
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


    def get_common_games(self, primary_player_logs, other_players_names, season='2024-25'):
        """Find common games between players"""
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

    def get_games_to_exclude(self, player_logs, players_off_names, season='2024-25'):
        """Find games to exclude due to filtering"""
        exclude_game_ids = set()
        
        # Loop through players_off and union game IDs
        for player_name in players_off_names:
            # Reuse existing cached game logs function - returns tuple (gamelogs, next_team)
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

    def apply_filters(self, df, filter_params):
        """Apply all filters to game logs dataframe"""
        # Apply minutes filter
        if 'minutes_filter' in filter_params:
            min_filter, max_filter = filter_params['minutes_filter']
            df = df[(df['MIN'] >= min_filter) & (df['MIN'] <= max_filter)]

        # Apply date filter
        if filter_params.get('date_filter'):
            date_filter = pd.to_datetime(filter_params['date_filter'])
            df['GAME_DATE'] = pd.to_datetime(df['GAME_DATE'])
            df = df[df['GAME_DATE'] >= date_filter]

        # Apply location filter
        if filter_params.get('location_filter') and filter_params['location_filter'] != 'Both':
            if filter_params['location_filter'] == 'Home':
                df = df[~df['MATCHUP'].str.contains('@')]
            elif filter_params['location_filter'] == 'Away':
                df = df[df['MATCHUP'].str.contains('@')]

        # Apply teams against filter
        if filter_params.get('teams_against'):
            teams_against = filter_params['teams_against']
            if isinstance(teams_against, (list, set)) and teams_against:
                # Extract opponent team (second team in matchup like "LAL vs. HOU" or "LAL @ HOU")
                df = df[df['MATCHUP'].str.extract(r'(?:vs\.|@)\s*([A-Z]{2,3})')[0].isin(teams_against)]

        # Apply playstyle filter
        if filter_params.get('playstyle_range'):
            min_rating, max_rating = filter_params['playstyle_range']
            if 'PLAYTYPE_RTG' in df.columns:
                df = df[(df['PLAYTYPE_RTG'] >= min_rating) & (df['PLAYTYPE_RTG'] <= max_rating)]

        # Apply players on/off filter
        season = filter_params.get('season_filter', '2024-25')
        if filter_params.get('players_on') or filter_params.get('players_off'):
            df = self.filter_players_on_off(df, filter_params.get('players_on', []), filter_params.get('players_off', []), season)

        # Apply self filters (stat ranges)
        if filter_params.get('self_filters'):
            # Handle new SelfFilter format
            for self_filter in filter_params['self_filters']:
                if hasattr(self_filter, 'stat_column') and self_filter.stat_column in df.columns:
                    if self_filter.operator == 'gte':
                        df = df[df[self_filter.stat_column] >= self_filter.value]
                    elif self_filter.operator == 'gt':
                        df = df[df[self_filter.stat_column] > self_filter.value]
                    elif self_filter.operator == 'lt':
                        df = df[df[self_filter.stat_column] < self_filter.value]
                    elif self_filter.operator == 'lte':
                        df = df[df[self_filter.stat_column] <= self_filter.value]
                    elif self_filter.operator == 'eq':
                        df = df[df[self_filter.stat_column] == self_filter.value]
                    elif self_filter.operator == 'between' and self_filter.value2 is not None:
                        df = df[(df[self_filter.stat_column] >= self_filter.value) & 
                               (df[self_filter.stat_column] <= self_filter.value2)]
            # Handle old format for backward compatibility
            if isinstance(filter_params['self_filters'], dict):
                for stat, (min_val, max_val) in filter_params['self_filters'].items():
                    if stat in df.columns:
                        df = df[(df[stat] >= min_val) & (df[stat] <= max_val)]
        
        # Apply last games filter
        if filter_params.get('game_filter'):
            try:
                last_games = int(filter_params['game_filter'])
                df = df.head(last_games)
            except (ValueError, TypeError):
                pass

        return df

    def get_filtered_logs(self, player_name, filter_params):
        """Get filtered game logs based on parameters"""
        full_game_logs, next_team = self._get_game_logs(player_name, filter_params['season_filter'])
        
        
        teams_against = None
        for index, ele in enumerate(filter_params['teams_against']):
            filtered_teams = set(self.filter_teams(ele, int(filter_params['rank_filter'][index]), filter_params['date_filter']))
            if teams_against is None:
                teams_against = filtered_teams
            else:
                teams_against = teams_against.intersection(filtered_teams)
        
        if teams_against is None:
            teams_against = set()
        
        filter_params['teams_against'] = teams_against
        filtered_logs = self.apply_filters(full_game_logs.copy(), filter_params)
        
        # Calculate statistics
        average_columns = ['MIN', 'PTS', 'REB', 'AST', 'PRA', 'PA', 'PR', 'RA', 
                         'FD_PTS', 'FGM', 'FGA', 'FG_PCT','FG2A','FG2M', 'FG3M', 'FG3A', 'FTM', 'FTA', 
                         'OREB', 'DREB', 'TOV', 'STL', 'BLK', 'PF', 'STKS','PLUS_MINUS']
        filtered_logs['GAME_DATE'] = pd.to_datetime(filtered_logs['GAME_DATE'], errors='coerce').dt.date
        filtered_averages = filtered_logs[average_columns].mean().round(2)
        season_averages = full_game_logs[average_columns].mean().round(2)
        filtered_logs.drop(['PLAYER_NAME','GAME_ID','NBA_FANTASY_PTS','FT_PCT','PLUS_MINUS', 'MIN_SEC', 'TEAM_ID', 'TEAM_ABBREVIATION'],axis=1, inplace = True)
        filtered_logs['GAME_DATE'] = filtered_logs['GAME_DATE'].astype(str)
        
        return {
            'game_logs': filtered_logs.to_json(orient='records',  date_format='iso'),
            'averages': filtered_averages.to_frame().T.to_json(orient='records'),
            'season_averages': season_averages.to_frame().T.to_json(orient='records'),
            'next_game': self._get_team_name_by_id(next_team)  # This should be implemented properly
        }
    
    def filter_teams(self, filter, rank_filter, date_filter=None):
        """Filter teams with daily caching - avoids repeated NBA API calls"""
        # Check if this filter type requires NBA API calls
        api_dependent_filters = ['C&S 3s', 'C&S PTS', 'C&S 3A', 'PU 2s', 'PU 3s', 'PU PTS'] + \
                               ['OPP_AST','OPP_PTS','OPP_REB','OPP_STOCKS','OPP_FTA', 'OPP_TOV', 'OPP_BLK', 'OPP_STL','OPP_FG3M', 'OPP_FG3A','OPP_FTA']
        
        if filter in api_dependent_filters and date_filter is not None:
            # This might make NBA API calls - cache it daily
            return self._filter_teams_with_api_calls(filter, rank_filter, date_filter)
        else:
            # Static database lookups - can use shorter cache
            return self._filter_teams_computed(filter, rank_filter, date_filter)
    
    def _filter_teams_with_api_calls(self, filter, rank_filter, date_filter=None):
        """Filter teams that may involve NBA API calls - cached daily"""
        if self.cache.enabled:
            from ..utils.cache_config import CACHE_PREFIXES
            
            # Cache with date component since NBA API data changes daily
            cache_key = self.cache._generate_key(
                CACHE_PREFIXES['team_stats_daily'],
                True,  # include_date
                'filter_teams_api',
                filter, rank_filter, str(date_filter)
            )
            
            cached_result = self.cache.get(cache_key)
            if cached_result is not None:
                logger.debug(f"Cache hit for team filter: {filter} (avoiding NBA API call)")
                return cached_result
            
            logger.info(f"Cache miss for team filter: {filter} - making NBA API call")
            result = self._filter_teams_uncached(filter, rank_filter, date_filter)
            
            # Use 1 AM CST expiry for daily NBA data
            if set_cache_with_1am_expiry(self.cache.redis, cache_key, self.cache._serialize(result)):
                logger.info(f"Cached team filter result for {filter} until 1 AM CST tomorrow")
            else:
                # Fallback to regular TTL
                ttl = self.cache._get_ttl('daily_nba_data')
                self.cache.set(cache_key, result, ttl)
            
            return result
        else:
            return self._filter_teams_uncached(filter, rank_filter, date_filter)
    
    def _filter_teams_computed(self, filter, rank_filter, date_filter=None):
        """Filter teams using computed/database data - shorter cache"""
        if self.cache.enabled:
            from ..utils.cache_config import CACHE_PREFIXES
            
            cache_key = self.cache._generate_key(
                CACHE_PREFIXES['computed'],
                False,  # include_date
                'filter_teams_computed',
                filter, rank_filter, str(date_filter)
            )
            
            cached_result = self.cache.get(cache_key)
            if cached_result is not None:
                logger.debug(f"Cache hit for computed team filter: {filter}")
                return cached_result
            
            result = self._filter_teams_uncached(filter, rank_filter, date_filter)
            
            ttl = self.cache._get_ttl('intraday_computed')
            self.cache.set(cache_key, result, ttl)
            
            return result
        else:
            return self._filter_teams_uncached(filter, rank_filter, date_filter)
    
    def _filter_teams_uncached(self, filter, rank_filter, date_filter=None):
        #filter into diff types
        Catch_Shoot_types = ['C&S 3s', 'C&S PTS','C&S 3A']
        Pullup_types = ['PU 2s', 'PU 3s', 'PU PTS']
        playtypes = ['Transition', 'Isolation', 'PRBallHandler', 'PRRollMan', 'OffRebound','Spotup', 'Cut', 'Handoff', 'OffScreen', 'Misc', 'Postup']
        overall_opp_types = ['OPP_AST','OPP_PTS','OPP_REB','OPP_STOCKS','OPP_FTA', 'OPP_TOV', 'OPP_BLK', 'OPP_STL','OPP_FG3M', 'OPP_FG3A','OPP_FTA']
        assist_types = ["TwoPtAssists","ThreePtAssists","Arc3Assists","Corner3Assists","AtRimAssists","ShortMidRangeAssists","LongMidRangeAssists"]
        
        if filter in Catch_Shoot_types:
            df = self.catch_shoot_filtering(filter, date_filter)
        elif filter in Pullup_types:
            df = self.pullup_filtering(filter,date_filter)
        elif filter in playtypes:
            df = self.playtype_filtering(filter)
        elif filter in overall_opp_types:
            df = self.general_opp_filtering(filter, date_filter)
        elif filter == 'Less Than 10 ft':
            df = self._fetch_data_from_table('less_than_10_ft')
            df.sort_values(by = 'FG2M', ascending = False, inplace=True)
            df['team'] = df['TEAM_ABBREVIATION']
        elif filter in assist_types:
            df = self._fetch_data_from_table('processed_team_assists')
            df.sort_values(by= filter, ascending = False, inplace = True)
            df['team'] = df['Name']
            
            
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
        # Normalize legacy table names and avoid quoting identifiers for Postgres
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
    def general_opp_filtering(self, filter, date_filter):
        if date_filter is not None:
            date_filter = pd.to_datetime(date_filter)
            df = endpoints.LeagueDashTeamStats(measure_type_detailed_defense = 'Opponent',per_mode_detailed = 'Per48',date_from_nullable = date_filter).get_data_frames()[0]
        else:
            df = self._fetch_data_from_table('general_opponent_stats')
        df['OPP_STOCKS'] = df['OPP_BLK'] + df['OPP_STL']
        df['team'] = df['TEAM_NAME'].apply(nba_team_to_abbreviation)
        return df.sort_values(by = filter, ascending = False)

    # Function that returns opponent playtype data sorted
    def playtype_filtering(self, filter):
        df = self._fetch_data_from_table('team_play_types')
        return df.sort_values(by= filter, ascending=False)
            
    #Function that filters when the user is filtering for catch and shoot teams
    def catch_shoot_filtering(self, filter, date_filter):
        f_map = {'C&S 3s': 'FG3M', 'C&S PTS' : 'PTS', 'C&S 3A' : 'FG3A'}         
        if date_filter is not None:
                date_filter = pd.to_datetime(date_filter)
                df = endpoints.LeagueDashOppPtShot(general_range_nullable = 'Catch and Shoot', date_from_nullable = date_filter).get_data_frames()[0]
        else:
                df = self._fetch_data_from_table('catch_and_shoot')
        df['PTS'] = df['FG3M'] * 3 + df['FG2M'] * 2
        df['team'] = df['TEAM_ABBREVIATION']
        return df.sort_values(by = f_map[filter], ascending = False)

    #Function that filters when the user is filtering for pullup teams
    def pullup_filtering(self, filter, date_filter):
        f_map = {'PU 2s': 'FG2M', 'PU 3s': 'FG3M', 'PU PTS' : 'PTS'}
        if date_filter is not None:
            date_filter = pd.to_datetime(date_filter)
            df = endpoints.LeagueDashOppPtShot(general_range_nullable = 'Pullups', date_from_nullable = date_filter).get_data_frames()[0]
        else:
            df = self._fetch_data_from_table('pullups')
        df['PTS'] = df['FG3M'] * 3 + df['FG2M'] * 2
        df['team'] = df['TEAM_ABBREVIATION']
        return df.sort_values(by = f_map[filter], ascending = False) 

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