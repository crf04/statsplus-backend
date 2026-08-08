import pandas as pd
import logging
import requests
from collections.abc import Callable
from nba_api.stats.static import players
from nba_api.stats.endpoints import playergamelogs, PlayerDashPtShots
from rapidfuzz import process, fuzz
from typing import Optional
from ..errors import (
    AppError,
    InvalidInputError,
    ProviderUnavailableError,
    ResourceNotFoundError,
)
from app.config.settings import RuntimeSettings, get_runtime_settings
from app.services.nba_stats_adapter import NBAStatsAdapter

logger = logging.getLogger(__name__)

class PlayerService:
    def __init__(self, db_engine, settings: RuntimeSettings | None = None):
        self.engine = db_engine
        self.settings = settings or get_runtime_settings()
        self.nba_stats = NBAStatsAdapter(settings=self.settings)

    def get_all_players(self):
        """Fetch list of all players from database"""
        try:
            df = self._fetch_data_from_table('player_play_types')
            return df['PLAYER_NAME'].values.tolist()
        except Exception as e:
            logger.error("Error fetching players: %s", e)
            return []

    def get_player_profile(self, player_name, category, opp_team=None):
        """
        Get player profile data based on category.
        Categories: Playtypes, assists, Archetype
        """
        
        if not player_name or not category:
            raise InvalidInputError("player_name and category are required.")

        # Fuzzy match player name.
        player_name = self._fuzzy_match_player_name(player_name)
        if player_name is None:
            raise ResourceNotFoundError("The requested player was not found.")

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
                raise InvalidInputError("The requested profile category is invalid.")
        except AppError:
            raise
        except requests.exceptions.RequestException as error:
            raise ProviderUnavailableError(detail=error) from error
        except (IndexError, KeyError) as error:
            raise ResourceNotFoundError(
                "The requested player profile was not found.", detail=error
            ) from error
        except Exception:
            logger.exception("Error getting player profile")
            raise
        
    def _fuzzy_match_player_name(self, player_name: str) -> Optional[str]:
        """
        Fuzzy match player name against the player database.
        
        Args:
            player_name (str): The input player name to match
            
        Returns:
            Optional[str]: The best matching player name from database, or None if no good match found
        """
        try:
            # Get all player names from database
            df = self._fetch_data_from_table('player_information')
            
            if df.empty:
                return None
                
            all_player_names = df['full_name'].tolist()
            
            # First try exact match (case insensitive)
            player_name_lower = player_name.lower().strip()
            for name in all_player_names:
                if name.lower().strip() == player_name_lower:
                    return name
            
            # If no exact match, use fuzzy matching
            match = process.extractOne(
                player_name,
                all_player_names,
                scorer=fuzz.token_sort_ratio,
                score_cutoff=85  # Require 85% similarity
            )
            
            if match:
                return match[0]  # Return the matched name
                
            return None
            
        except Exception:
            logger.exception("Error fuzzy matching player name %r", player_name)
            raise

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
        player_team = self._fetch_data_from_table('player_team_table')
        team_id = player_team[player_team['Player'] == player_name]['Team_ID'].values[0]
        df = self.nba_stats.run_endpoint(
            'player_shot_chart',
            lambda timeout: PlayerDashPtShots(
                player_id=self.get_player_id(player_name),
                team_id=int(team_id),
                per_mode_simple='PerGame',
                timeout=timeout,
            ),
            frame_index=1,
        )
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
            gl = self.nba_stats.run_endpoint(
                "player_gamelogs_against",
                lambda timeout: playergamelogs.PlayerGameLogs(
                    season_nullable=self.settings.nba.current_season,
                    opp_team_id_nullable=team_id,
                    timeout=timeout,
                ),
            )
            
            # Filter and process game logs
            gl = gl[gl["PLAYER_ID"].isin(player_ids)]
            gl = gl[['PLAYER_NAME', 'PLAYER_ID', 'GAME_DATE', 'MIN', 
                    'FGM', 'FGA', 'FG3M', 'FG3A', 'FTM', 'FTA', 'PTS', 'TOV']]
            
            per36_df = self._fetch_data_from_table('player_per36_stats')
            
            # Calculate per 36minute stats
            for col in ['FGM', 'FGA', 'FG3M', 'FG3A', 'FTM', 'FTA', 'PTS', 'TOV']:
                gl[f'{col}/36MIN'] = (gl[col] / gl['MIN']) * 36
            
            logger.debug("Archetype game logs rows: %s", len(gl))
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
            logger.error("Error getting archetype gamelogs: %s", e)
            return []

    def _get_archetype_players_from_player(self, player_name):
        """Get list of player IDs in the same cluster as the given player"""
        try:
            players_df = self._fetch_data_from_table('player_clusters')
            cluster_id = players_df[players_df['PlayerName'] == player_name]['ClusterID'].values[0]
            result = players_df[players_df['ClusterID'] == cluster_id]['PlayerID']
            return result.tolist()
        except Exception as e:
            logger.error("Error getting archetype players: %s", e)
            return []

    def store_player_information(self, progress_callback: Callable | None = None):
        """Store basic player information in database.

        Fetching completes before the table is replaced, so a provider failure
        leaves the existing player list untouched.
        """
        if progress_callback is not None:
            progress_callback(0.1, "Fetching player information")
        player_dict = players.get_players()
        player_df = pd.DataFrame.from_dict(player_dict)
        if progress_callback is not None:
            progress_callback(0.75, "Transforming player information")
        from app.services.table_publisher import AtomicTablePublisher

        if progress_callback is not None:
            progress_callback(0.9, "Publishing player information")
        AtomicTablePublisher(self.engine).publish({"player_information": player_df})
        if progress_callback is not None:
            progress_callback(1.0, "Completed")
        return True

    def get_player_id(self, player_name):
        """Get player ID from database"""
        try:
            df = self._fetch_data_from_table('player_information')
            player = df[df['full_name'] == player_name]
            return player['id'].values[0]
        except Exception as e:
            logger.error("Error getting player ID: %s", e)
            return None

    def _fetch_data_from_table(self, table_name):
        """Helper method to fetch data from database table"""
        from ..utils.tables import normalize_table_name
        table_name = normalize_table_name(table_name)
        query = f"SELECT * FROM {table_name}"
        with self.engine.connect() as conn:
            return pd.read_sql(query, conn)

    @staticmethod
    def _get_teams():
        """Helper method to get NBA teams"""
        from nba_api.stats.static import teams
        return teams.get_teams()
