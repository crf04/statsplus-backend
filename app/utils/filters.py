import pandas as pd
from app.config.settings import get_runtime_settings
from app.providers.nba_stats import NBAStatsAdapter, NBAStatsProvider
from app.utils.db import get_engine

# Function to get player ID from database
def get_player_id(player_name):
    engine = get_engine()
    query = "SELECT * FROM 'Player_Information'"
    with engine.connect() as conn:
        player_dict = pd.read_sql(query, conn)
    player = player_dict[player_dict['full_name'] == player_name]
    return player['id'].values[0]

def _resolve_nba_stats_provider(
    nba_stats_provider: NBAStatsProvider | None,
) -> NBAStatsProvider:
    """Use an injected provider or create one for this standalone helper call."""

    if nba_stats_provider is not None:
        return nba_stats_provider
    return NBAStatsAdapter(settings=get_runtime_settings())


def _resolve_provider_for_names(names, nba_stats_provider):
    """Resolve one provider for a non-empty player-name collection."""

    if not names:
        return nba_stats_provider
    return _resolve_nba_stats_provider(nba_stats_provider)


def apply_filters(df, filter_params, nba_stats_provider=None):
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
    df = filter_players_on_off(
        df,
        players_on,
        players_off,
        get_runtime_settings().nba.current_season,
        nba_stats_provider=nba_stats_provider,
    )
    
    return df

def filter_players_on_off(
    df,
    players_on,
    players_off,
    season,
    nba_stats_provider=None,
):
    provider = nba_stats_provider
    if provider is None and (players_on or players_off):
        provider = _resolve_nba_stats_provider(None)

    if players_on:
        common_games = get_common_games(
            df,
            players_on,
            season,
            nba_stats_provider=provider,
        )
        df = df[df['GAME_ID'].isin(common_games)]
    
    if players_off:
        exclude_games = get_games_to_exclude(
            df,
            players_off,
            season,
            nba_stats_provider=provider,
        )
        df = df[~df['GAME_ID'].isin(exclude_games)]
    
    return df

def get_games_to_exclude(
    player_logs,
    players_off_names,
    season=None,
    nba_stats_provider=None,
):
    season = season or get_runtime_settings().nba.current_season
    exclude_game_ids = set()
    provider = _resolve_provider_for_names(players_off_names, nba_stats_provider)
    
    # Loop through players_off and union game IDs
    for player_name in players_off_names:
        player_id = get_player_id(player_name)
        player_gamelogs = provider.get_player_game_logs(
            player_id=player_id,
            season=season,
        )
        player_game_ids = set(player_gamelogs['GAME_ID'])
        
        # Union with exclude_game_ids to accumulate games where any player_off played
        exclude_game_ids |= player_game_ids

    return exclude_game_ids

def get_common_games(
    primary_player_logs,
    other_players_names,
    season=None,
    nba_stats_provider=None,
):
    season = season or get_runtime_settings().nba.current_season
    primary_game_team_pairs = set(zip(
        primary_player_logs['GAME_ID'], 
        primary_player_logs['TEAM_ABBREVIATION']
    ))
    provider = _resolve_provider_for_names(other_players_names, nba_stats_provider)
    
    # Loop through other players and find intersections
    for player_name in other_players_names:
        player_id = get_player_id(player_name)
        player_gamelogs = provider.get_player_game_logs(
            player_id=player_id,
            season=season,
        )
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
