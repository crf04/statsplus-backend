import pandas as pd
from nba_api.stats.endpoints import playergamelogs
from nba_api.stats import endpoints
from sqlalchemy import create_engine
from ..utils.helpers import get_opponent_team

# Function to get player ID from database
def get_player_id(player_name):
    engine = create_engine('sqlite:///nba_play_types.db')
    query = f"SELECT * FROM 'Player_Information'"
    with engine.connect() as conn:
        player_dict = pd.read_sql(query, conn)
    player = player_dict[player_dict['full_name'] == player_name]
    return player['id'].values[0]

def apply_filters(df, filter_params):
    """Apply all filters to the DataFrame"""
    minutes_filter = filter_params.get('minutes_filter', (0, 48))
    players_on = filter_params.get('players_on', [])
    players_off = filter_params.get('players_off', [])
    date_filter = filter_params.get('date_filter')
    teams_against = filter_params.get('teams_against', [])
    location_filter = filter_params.get('location_filter', 'Both')
    game_filter = filter_params.get('game_filter')
    playstyle_range = filter_params.get('playstyle_range', [75, 125])
    self_filters = filter_params.get('self_filters', {})

    # Apply minutes filter
    df = df[(df['MIN'] >= minutes_filter[0]) & (df['MIN'] <= minutes_filter[1])]
    # Apply date filter
    if date_filter:
        df['GAME_DATE'] = pd.to_datetime(df['GAME_DATE'])
        df = df[df['GAME_DATE'] >= date_filter]
    
    # Apply team filter
    if teams_against:
        df['OPPONENT'] = df['MATCHUP'].apply(lambda x: x.split()[-1].upper())
        df = df[df['OPPONENT'].isin(teams_against)]
        
    # Apply location filter
    if location_filter != 'Both':
        df = df[df['MATCHUP'].str.contains('vs' if location_filter == 'Home' else '@')]
    
    # Apply playstyle filter
    df = df[(df['PLAYTYPE_RTG'] >= playstyle_range[0]) & 
            (df['PLAYTYPE_RTG'] <= playstyle_range[1])]
    
    # Apply custom filters
    for column, range_values in self_filters.items():
        if column in df.columns:
            df = df[(df[column] >= range_values[0]) & 
                   (df[column] <= range_values[1])]
    
    # Apply game limit filter
    if game_filter:
        df = df.head(int(game_filter))
        
    # Apply players on/off filters
    df = filter_players_on_off(df, players_on, players_off, '2025-26')
    
    return df

def filter_players_on_off(df, players_on, players_off, season):
    if players_on:
        common_games = get_common_games(df, players_on, season)
        df = df[df['GAME_ID'].isin(common_games)]
    
    if players_off:
        exclude_games = get_games_to_exclude(df, players_off, season)
        df = df[~df['GAME_ID'].isin(exclude_games)]
    
    return df

def get_games_to_exclude(player_logs, players_off_names, season='2025-26'):
    exclude_game_ids = set()
    
    # Loop through players_off and union game IDs
    for player_name in players_off_names:
        player_id = get_player_id(player_name)
        player_gamelogs = playergamelogs.PlayerGameLogs(
            player_id_nullable=player_id, 
            season_nullable=season
        ).get_data_frames()[0]
        player_game_ids = set(player_gamelogs['GAME_ID'])
        
        # Union with exclude_game_ids to accumulate games where any player_off played
        exclude_game_ids |= player_game_ids

    return exclude_game_ids

def get_common_games(primary_player_logs, other_players_names, season='2025-26'):
    primary_game_team_pairs = set(zip(
        primary_player_logs['GAME_ID'], 
        primary_player_logs['TEAM_ABBREVIATION']
    ))
    
    # Loop through other players and find intersections
    for player_name in other_players_names:
        player_id = get_player_id(player_name)
        player_gamelogs = playergamelogs.PlayerGameLogs(
            player_id_nullable=player_id, 
            season_nullable=season
        ).get_data_frames()[0]
        player_game_team_pairs = set(zip(
            player_gamelogs['GAME_ID'], 
            player_gamelogs['TEAM_ABBREVIATION']
        ))
        
        primary_game_team_pairs = primary_game_team_pairs.intersection(
            player_game_team_pairs
        )
        
        if not primary_game_team_pairs:
            break
    
    return {pair[0] for pair in primary_game_team_pairs}

