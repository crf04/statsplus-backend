from functools import lru_cache
import pandas as pd
from nba_api.stats.endpoints import playergamelogs
from ..utils.helpers import get_opponent_team, nba_team_to_abbreviation
from ..utils.filters import filter_players_on_off

class GameService:
    def __init__(self, db_engine):
        self.engine = db_engine

    def get_player_id(self, player_name):
        player_dict = self._fetch_data_from_table('Player_Information')
        player = player_dict[player_dict['full_name'] == player_name]
        return player['id'].values[0]

    def _fetch_data_from_table(self, table_name):
        query = f"SELECT * FROM '{table_name}'"
        with self.engine.connect() as conn:
            return pd.read_sql(query, conn)

    @lru_cache(maxsize=32)
    def get_player_game_logs_with_ratings(self, player_name):
        """Retrieve game logs for a player and calculate ratings"""
        game_logs_df = self._get_game_logs(player_name)
        
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
        
        return game_logs_df

    def _get_game_logs(self, player_name, season='2023-24'):
        player_id = self.get_player_id(player_name)
        gamelogs = playergamelogs.PlayerGameLogs(
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
            'NICKNAME', 'TEAM_ID', 'TEAM_NAME', 'DD2', 'TD3', 'WNBA_FANTASY_PTS',
            'BLKA', 'PFD'
        ]
        gamelogs = gamelogs.drop(drop_columns, axis=1)
        
        # Calculate additional stats
        gamelogs['PRA'] = gamelogs['PTS'] + gamelogs['REB'] + gamelogs['AST']
        gamelogs['PA'] = gamelogs['PTS'] + gamelogs['AST']
        gamelogs['PR'] = gamelogs['PTS'] + gamelogs['REB']
        gamelogs['RA'] = gamelogs['REB'] + gamelogs['AST']
        gamelogs['STKS'] = gamelogs['STL'] + gamelogs['BLK']
        gamelogs['FD_PTS'] = gamelogs['NBA_FANTASY_PTS']
        gamelogs['+/-'] = gamelogs['PLUS_MINUS']
        gamelogs['MIN'] = gamelogs['MIN'].round().astype(int)
        
        return gamelogs

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
        full_game_logs = self.get_player_game_logs_with_ratings(player_name)
        filtered_logs = self._apply_filters(full_game_logs.copy(), filter_params)
        
        # Calculate statistics
        average_columns = ['MIN', 'PTS', 'REB', 'AST', 'PRA', 'PA', 'PR', 'RA', 
                         'FD_PTS', 'FGM', 'FGA', 'FG3M', 'FG3A', 'FTM', 'FTA', 
                         'OREB', 'DREB', 'TOV', 'STL', 'BLK', 'PF', 'STKS']
        
        filtered_averages = filtered_logs[average_columns].mean().round(2)
        season_averages = full_game_logs[average_columns].mean().round(2)
        
        return {
            'game_logs': filtered_logs.to_json(orient='records'),
            'averages': filtered_averages.to_frame().T.to_json(orient='records'),
            'season_averages': season_averages.to_frame().T.to_json(orient='records'),
            'next_game': "Houston Rockets"  # This should be implemented properly
        }