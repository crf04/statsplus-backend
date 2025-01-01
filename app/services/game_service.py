from functools import lru_cache
import pandas as pd
from nba_api.stats import endpoints
from nba_api.stats.static import teams
from ..utils.helpers import get_opponent_team, nba_team_to_abbreviation
from ..utils.filters import filter_players_on_off, apply_filters
from difflib import get_close_matches

class GameService:
    def __init__(self, db_engine):
        self.engine = db_engine
        self.all_teams = teams.get_teams()

    def get_player_id(self, player_name):
        player_dict = self._fetch_data_from_table('Player_Information')

        player_names = player_dict['full_name'].tolist()
        closest_match = get_close_matches(player_name, player_names, n=1, cutoff=0.8)

        if closest_match:
            player = player_dict[player_dict['full_name'] == closest_match[0]]
            return player['id'].values[0]
        else:
            raise ValueError(f"No matching player found for {player_name}.")

    def _fetch_data_from_table(self, table_name):
        query = f"SELECT * FROM '{table_name}'"
        with self.engine.connect() as conn:
            return pd.read_sql(query, conn)

    @lru_cache(maxsize=32)
    def get_player_game_logs_with_ratings(self, player_name):
        """Retrieve game logs for a player and calculate ratings"""
        game_logs_df, next_team = self._get_game_logs(player_name)
        
        # Calculate ratings
        game_logs_df['PLAYTYPE_RTG'] = game_logs_df.apply(
            lambda row: self.calculate_matchup_rating(
                row['PLAYER_NAME'], 
                get_opponent_team(row['MATCHUP'])
            ), 
            axis=1
        )
        game_logs_df['AST_LOC_RTG'] = game_logs_df.apply(
            lambda row: self.calculate_assist_location_rating(
                row['PLAYER_NAME'], 
                get_opponent_team(row['MATCHUP'])
            ), 
            axis=1
        )
        
        return game_logs_df, next_team

    def _get_game_logs(self, player_name, season='2024-25'):
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
        next_game = endpoints.PlayerNextNGames(number_of_games=1,player_id=player_id).get_data_frames()[0]
        team1, team2 = next_game.loc[0,'VISITOR_TEAM_ID'], next_game.loc[0,'HOME_TEAM_ID']
        if team1 != gamelogs.loc[0, 'TEAM_ID']:
            next_team = team1
        else:
            next_team = team2
            
        
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

    def calculate_matchup_rating(self, player_name, team):
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
        return round(matchupRTG, 2)

    def get_filtered_logs(self, player_name, filter_params):
        """Get filtered game logs based on parameters"""
        full_game_logs, next_team = self.get_player_game_logs_with_ratings(player_name)
        
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
        filtered_logs = apply_filters(full_game_logs.copy(), filter_params)
        
        # Calculate statistics
        average_columns = ['MIN', 'PTS', 'REB', 'AST', 'PRA', 'PA', 'PR', 'RA', 
                         'FD_PTS', 'FGM', 'FGA', 'FG3M', 'FG3A', 'FTM', 'FTA', 
                         'OREB', 'DREB', 'TOV', 'STL', 'BLK', 'PF', 'STKS']
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
    
    def filter_teams(self, filter, rank_filter, date_filter = None):
        #filter into diff types
        Catch_Shoot_types = ['C&S 3s', 'C&S PTS','C&S 3A']
        Pullup_types = ['PU 2s', 'PU 3s', 'PU PTS']
        playtypes = ['Transition', 'Isolation', 'PRBallHandler', 'PRRollMan', 'OffRebound','Spotup', 'Cut', 'Handoff', 'OffScreen', 'Misc', 'Postup']
        overall_opp_types = ['OPP_AST','OPP_PTS','OPP_REB','OPP_STOCKS']
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
            df = self._fetch_data_from_table('Less Than 10 Ft')
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
        """Helper method to fetch data from database table"""
        query = f"SELECT * FROM '{table_name}'"
        with self.engine.connect() as conn:
            return pd.read_sql(query, conn)

    # Function that returns general opponent stats sorted by the filter
    def general_opp_filtering(self, filter, date_filter):
        if date_filter is not None:
            date_filter = pd.to_datetime(date_filter)
            df = endpoints.LeagueDashTeamStats(measure_type_detailed_defense = 'Opponent',per_mode_detailed = 'Per48',date_from_nullable = date_filter).get_data_frames()[0]
        else:
            df = self._fetch_data_from_table('General Opponent Stats')
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
                df = self._fetch_data_from_table('Catch and Shoot')
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
            df = self._fetch_data_from_table('Pullups')
        df['PTS'] = df['FG3M'] * 3 + df['FG2M'] * 2
        df['team'] = df['TEAM_ABBREVIATION']
        return df.sort_values(by = f_map[filter], ascending = False) 

    # Function to find team name by ID
    def _get_team_name_by_id(self, team_id): 
        for team in self.all_teams:
            if team['id'] == team_id:
                return team['full_name']
        return None