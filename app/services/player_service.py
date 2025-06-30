import pandas as pd
from nba_api.stats.static import players
from nba_api.stats.endpoints import playergamelogs, PlayerGameLogs, PlayerDashPtShots
from sqlalchemy import create_engine

class PlayerService:
    def __init__(self, db_engine):
        self.engine = db_engine

    def get_all_players(self):
        """Fetch list of all players from database"""
        try:
            df = self._fetch_data_from_table('player_play_types')
            return df['PLAYER_NAME'].values.tolist()
        except Exception as e:
            print(f"Error fetching players: {e}")
            return []

    def get_player_profile(self, player_name, category, opp_team=None):
        """
        Get player profile data based on category.
        Categories: Playtypes, assists, Archetype
        """
        try:
            if category == 'Playtypes':
                return self._get_player_playtypes(player_name)
            elif category == 'assists':
                return self._get_player_assists(player_name)
            elif category == 'Archetype':
                return self._get_archetype_gamelogs(player_name, opp_team)
            elif category == 'Shooting Type':
                return self._get_shooting_type(player_name)
            elif category == 'Zone Shooting':
                return self._get_player_zone_shooting(player_name)
            else:
                raise ValueError(f"Unknown category: {category}")
        except Exception as e:
            print(f"Error getting player profile: {e}")
            return None

    def _get_player_playtypes(self, player_name):
        """Get player playtypes data"""
        df = self._fetch_data_from_table('player_play_types')
        return df[df['PLAYER_NAME'] == player_name].to_dict(orient='records')[0]
    
    def _get_player_zone_shooting(self, player_name):
        """Get player zone shooting data"""
        df = self._fetch_data_from_table('player_shooting_zones')
        return df[df['PLAYER_NAME'] == player_name].to_dict(orient='records')[0]

    def _get_player_assists(self, player_name):
        """Get player assists data"""
        df = self._fetch_data_from_table('processed_player_assists')
        player_data = df[df['Name'] == player_name]
        return player_data.to_dict(orient='records')

    def _get_shooting_type(self, player_name):
        """Get player shooting type data"""
        player_team = self._fetch_data_from_table('Player_Team_Table')
        team_id = player_team[player_team['Player'] == player_name]['Team_ID'].values[0]
        df = PlayerDashPtShots(player_id=self.get_player_id(player_name), team_id = int(team_id), per_mode_simple = 'PerGame' ).get_data_frames()[1]
        df['SHOT_TYPE'].replace({'Less than 10 ft': '<10 Ft'}, inplace=True)
        df['SHOT_TYPE'].replace({'Pull Ups': 'Pullup'}, inplace=True)
        df['SHOT_TYPE'].replace({'Catch and Shoot': 'C&S'}, inplace=True)
        df.fillna(0, inplace=True)
        return df.to_dict(orient='records')
    
    def _get_archetype_gamelogs(self, player_name, opp_team):
        """Get archetype gamelogs for player against specific team"""
        try:
            # Get player's cluster members
            player_ids = self._get_archetype_players_from_player(player_name)
            
            
            # Get team ID
            team_dict = pd.DataFrame(self._get_teams())
            team_id = team_dict.loc[team_dict['full_name'] == opp_team, 'id'].values[0]
            
            # Get game logs
            gl = playergamelogs.PlayerGameLogs(
                season_nullable='2024-25',
                opp_team_id_nullable=team_id
            ).get_data_frames()[0]
            
            # Filter and process game logs
            gl = gl[gl["PLAYER_ID"].isin(player_ids)]
            gl = gl[['PLAYER_NAME', 'PLAYER_ID', 'GAME_DATE', 'MIN', 
                    'FGM', 'FGA', 'FG3M', 'FG3A', 'FTM', 'FTA', 'PTS', 'TOV']]
            
            per36_df = self._fetch_data_from_table('Player_Per36_Stats')
            
            # Calculate per 36minute stats
            for col in ['FGM', 'FGA', 'FG3M', 'FG3A', 'FTM', 'FTA', 'PTS', 'TOV']:
                gl[f'{col}/36MIN'] = (gl[col] / gl['MIN']) * 36
            
            print(gl)
            merged_df = gl.merge(per36_df, left_on="PLAYER_ID", right_on="PLAYER_ID", suffixes=('', '_season'))
            #get percentage diff between game and season
            for col in ['FGM', 'FGA', 'FG3M', 'FG3A', 'FTM', 'FTA', 'PTS', 'TOV']:
                merged_df[f'{col}/36MIN_DIFF'] = (merged_df[f'{col}/36MIN'] - merged_df[f'{col}_season']) / merged_df[f'{col}_season']
            
            merged_df = merged_df[['PLAYER_NAME', 'GAME_DATE', 'MIN', 
                    'FGM', 'FGA', 'FG3M', 'FG3A', 'FTM', 'FTA', 'PTS', 'TOV','FGM/36MIN', 'FGA/36MIN', 'FG3M/36MIN', 'FG3A/36MIN', 
                               'FTM/36MIN', 'FTA/36MIN', 'PTS/36MIN', 
                               'FGM/36MIN_DIFF', 'FGA/36MIN_DIFF', 'FG3M/36MIN_DIFF', 'FG3A/36MIN_DIFF', 
                               'FTM/36MIN_DIFF', 'FTA/36MIN_DIFF', 'PTS/36MIN_DIFF', 'TOV/36MIN_DIFF']]

            return merged_df.to_dict(orient='records')
        except Exception as e:
            print(f"Error getting archetype gamelogs: {e}")
            return []

    def _get_archetype_players_from_player(self, player_name):
        """Get list of player IDs in the same cluster as the given player"""
        try:
            players_df = self._fetch_data_from_table('player_clusters')
            cluster_id = players_df[players_df['PlayerName'] == player_name]['ClusterID'].values[0]
            result = players_df[players_df['ClusterID'] == cluster_id]['PlayerID']
            return result.tolist()
        except Exception as e:
            print(f"Error getting archetype players: {e}")
            return []

    def store_player_information(self):
        """Store basic player information in database"""
        try:
            player_dict = players.get_players()
            player_df = pd.DataFrame.from_dict(player_dict)
            player_df.to_sql('Player_Information', self.engine, 
                           if_exists='replace', index=False)
            return True
        except Exception as e:
            print(f"Error storing player information: {e}")
            return False

    def get_player_id(self, player_name):
        """Get player ID from database"""
        try:
            df = self._fetch_data_from_table('Player_Information')
            player = df[df['full_name'] == player_name]
            return player['id'].values[0]
        except Exception as e:
            print(f"Error getting player ID: {e}")
            return None

    def _fetch_data_from_table(self, table_name):
        """Helper method to fetch data from database table"""
        query = f"SELECT * FROM '{table_name}'"
        with self.engine.connect() as conn:
            return pd.read_sql(query, conn)

    @staticmethod
    def _get_teams():
        """Helper method to get NBA teams"""
        from nba_api.stats.static import teams
        return teams.get_teams()