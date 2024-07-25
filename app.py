from flask import Flask, jsonify, request
from flask_cors import CORS
from nba_api.stats import endpoints
from nba_api.stats.static import teams, players
from nba_api.stats.endpoints import playergamelogs
import pandas as pd
from sqlalchemy import create_engine
import requests
from functools import lru_cache

# Initialize Flask app
app = Flask(__name__)
CORS(app)  # Enable CORS

# Set up database connection
engine = create_engine('sqlite:///nba_play_types.db')

#function to return a player's id
def get_player_id(player_name):
    player_dict = fetch_data_from_table('Player_Information')
    player = player_dict[player_dict['full_name'] == player_name]
    player_id = player['id'].values[0]
    return player_id


    
# Function to fetch data from a table
def fetch_data_from_table(table_name):
    query = f"SELECT * FROM '{table_name}'"
    with engine.connect() as conn:
        df = pd.read_sql(query, conn)
    return df





#FOR FILTERING GAME LOGS

# Function to find common games between players
def get_common_games(primary_player_logs, other_players_names,season='2023-24'):
    primary_game_team_pairs = set(zip(primary_player_logs['GAME_ID'], primary_player_logs['TEAM_ABBREVIATION']))
    
    # Loop through other players and find intersections based on game IDs and team abbreviations
    for player_name in other_players_names:
        player_id = get_player_id(player_name)
        player_gamelogs = playergamelogs.PlayerGameLogs(player_id_nullable=player_id, season_nullable=season).get_data_frames()[0]
        player_game_team_pairs = set(zip(player_gamelogs['GAME_ID'], player_gamelogs['TEAM_ABBREVIATION']))
        
        primary_game_team_pairs = primary_game_team_pairs.intersection(player_game_team_pairs)
        
        if not primary_game_team_pairs:
            break
    
    common_game_ids = {pair[0] for pair in primary_game_team_pairs}
    
    return set(common_game_ids)

# Function to find games to exclude due to filtering
def get_games_to_exclude(player_logs, players_off_names, season= '2023-24'):
    exclude_game_ids = set()
    
    # Loop through players_off and union game IDs
    for player_name in players_off_names:
        player_id = get_player_id(player_name)
        player_gamelogs = playergamelogs.PlayerGameLogs(player_id_nullable=player_id, season_nullable=season).get_data_frames()[0]
        player_game_ids = set(player_gamelogs['GAME_ID'])
        
        # Union with exclude_game_ids to accumulate games where any player_off played
        exclude_game_ids |= player_game_ids

    return exclude_game_ids

# Function to calculate the player matchup rating
def calculate_matchup_rating(player_name,team):
    teams_df = fetch_data_from_table('team_play_types')
    players_df = fetch_data_from_table('player_play_types')

    playtypes = ['Cut', 'Isolation', 'PRRollMan', 'PRBallHandler', 'OffRebound', 'Spotup', 'Handoff', 'OffScreen', 'Misc', 'Postup', 'Transition']

    player_columns = [playtype + '%' for playtype in playtypes]
    team_columns = playtypes

    player_data = players_df.loc[players_df['PLAYER_NAME'] == player_name, player_columns]
    team_data = teams_df.loc[teams_df['team'] == team, team_columns]

    matchupRTG = (player_data.values * team_data.values).sum()

    return round(matchupRTG, 2)

def calculate_assist_location_rating(player_name, team):
    teams_df = fetch_data_from_table('processed_team_assists')
    players_df = fetch_data_from_table('processed_player_assists')
    
    cats = ["Arc3Assists","Corner3Assists","AtRimAssists","ShortMidRangeAssists","LongMidRangeAssists"]
    
    player_data = players_df.loc[players_df['Name'] == player_name, cats]
    team_data = teams_df.loc[teams_df['Name'] == team, cats]

    matchupRTG = (player_data.values * team_data.values).sum()

    return round(matchupRTG, 2)
    

def filter_players_on_off(df, players_on, players_off, season):
    if players_on:
        common_games = get_common_games(df, players_on, season)
        df = df[df['GAME_ID'].isin(common_games)]
    
    if players_off:
        exclude_games = get_games_to_exclude(df, players_off, season)
        df = df[~df['GAME_ID'].isin(exclude_games)]
    
    return df

def calculate_additional_stats(df):
    df['PRA'] = df['PTS'] + df['REB'] + df['AST']
    df['PA'] = df['PTS'] + df['AST']
    df['PR'] = df['PTS'] + df['REB']
    df['RA'] = df['REB'] + df['AST']
    df['STKS'] = df['STL'] + df['BLK']
    df['FD_PTS'] = df['NBA_FANTASY_PTS']
    df['+/-'] = df['PLUS_MINUS']
    df['MIN'] = df['MIN'].round().astype(int)
    return df
    
# Function to get the opponent team of the player
def get_opponent_team(match):
    parts = match.split(' ')
    if 'vs.' in match:
        return parts[2]
    elif '@' in match:
        return parts[-1]
    else:
        return None

# Function to fetch list of teams that fit the criteria
def filter_teams(filter, rank_filter, date_filter = None):
    #filter into diff types
    Catch_Shoot_types = ['C&S 3s', 'C&S PTS']
    Pullup_types = ['PU 2s', 'PU 3s', 'PU PTS']
    playtypes = ['Transition', 'Isolation', 'PRBallHandler', 'PRRollMan', 'OffRebound','Spotup', 'Cut', 'Handoff', 'OffScreen', 'Misc', 'Postup']
    overall_opp_types = ['OPP_AST','OPP_PTS','OPP_REBOUNDS','OPP_STOCKS']
    assist_types = ["TwoPtAssists","ThreePtAssists","Arc3Assists","Corner3Assists","AtRimAssists","ShortMidRangeAssists","LongMidRangeAssists"]
    
    if filter in Catch_Shoot_types:
        df = catch_shoot_filtering(filter, date_filter)
    elif filter in Pullup_types:
        df = pullup_filtering(filter,date_filter)
    elif filter in playtypes:
        df = playtype_filtering(filter)
    elif filter in overall_opp_types:
        df = general_opp_filtering(filter, date_filter)
    elif filter == 'Less Than 10 ft':
        df = fetch_data_from_table('Less Than 10 Ft')
        df.sort_values(by = 'FG2M', ascending = False, inplace=True)
        df['team'] = df['TEAM_ABBREVIATION']
    elif filter in assist_types:
        df = fetch_data_from_table('processed_team_assists')
        df.sort_values(by= filter, ascending = False, inplace = True)
        df['team'] = df['Name']
        
        
    if rank_filter >= 0:
        return df.head(rank_filter)['team'].tolist()
    else:
        return df.tail(-rank_filter)['team'].tolist()

# Function that returns general opponent stats sorted by the filter
def general_opp_filtering(filter, date_filter):
    if date_filter is not None:
        date_filter = pd.to_datetime(date_filter)
        df = endpoints.LeagueDashTeamStats(measure_type_detailed_defense = 'Opponent',per_mode_detailed = 'Per48',date_from_nullable = date_filter).get_data_frames()[0]
    else:
        df = fetch_data_from_table('General Opponent Stats')
    df['OPP_STOCKS'] = df['OPP_BLK'] + df['OPP_STL']
    df['team'] = df['TEAM_NAME'].apply(nba_team_to_abbreviation)
    return df.sort_values(by = filter, ascending = False)

# Function that returns opponent playtype data sorted
def playtype_filtering(filter):
    df = fetch_data_from_table('team_play_types')
    return df.sort_values(by= filter, ascending=False)
        
#Function that filters when the user is filtering for catch and shoot teams
def catch_shoot_filtering(filter, date_filter):
    f_map = {'C&S 3s': 'FG3M', 'C&S PTS' : 'PTS'}         
    if date_filter is not None:
            date_filter = pd.to_datetime(date_filter)
            df = endpoints.LeagueDashOppPtShot(general_range_nullable = 'Catch and Shoot', date_from_nullable = date_filter).get_data_frames()[0]
    else:
            df = fetch_data_from_table('Catch and Shoot')
    df['PTS'] = df['FG3M'] * 3 + df['FG2M'] * 2
    df['team'] = df['TEAM_ABBREVIATION']
    return df.sort_values(by = f_map[filter], ascending = False)

#Function that filters when the user is filtering for pullup teams
def pullup_filtering(filter, date_filter):
    f_map = {'PU 2s': 'FG2M', 'PU 3s': 'FG3M', 'PU PTS' : 'PTS'}
    if date_filter is not None:
        date_filter = pd.to_datetime(date_filter)
        df = endpoints.LeagueDashOppPtShot(general_range_nullable = 'Pullups', date_from_nullable = date_filter).get_data_frames()[0]
    else:
        df = fetch_data_from_table('Pullups')
    df['PTS'] = df['FG3M'] * 3 + df['FG2M'] * 2
    df['team'] = df['TEAM_ABBREVIATION']
    return df.sort_values(by = f_map[filter], ascending = False)  
         
# Add this cache decorator
@lru_cache(maxsize=32)
def cached_playergamelogs(player_id, season):
    gamelogs = playergamelogs.PlayerGameLogs(player_id_nullable=player_id, season_nullable=season)
    return gamelogs.get_data_frames()[0]

def game_log(player_name, minutes_filter=(0,48), players_on=[], players_off=[], date_filter=None, teams_against=[], location_filter=None, last_games=None, playstyle_filter=None):
    player_id= get_player_id(player_name)
    #if not player_id:
        #return pd.DataFrame()

    season = '2023-24'

    gamelogs_df = cached_playergamelogs(player_id, season)
    gamelogs_df = gamelogs_df.drop(['SEASON_YEAR','PLAYER_ID','GP_RANK', 'W_RANK', 'L_RANK', 'W_PCT_RANK','MIN_RANK', 'FGM_RANK', 'FGA_RANK', 'FG_PCT_RANK', 'FG3M_RANK',
   'FG3A_RANK', 'FG3_PCT_RANK', 'FTM_RANK', 'FTA_RANK', 'FT_PCT_RANK',
   'OREB_RANK', 'DREB_RANK', 'REB_RANK', 'AST_RANK', 'TOV_RANK',
   'STL_RANK', 'BLK_RANK', 'BLKA_RANK', 'PF_RANK', 'PFD_RANK', 'PTS_RANK',
   'PLUS_MINUS_RANK', 'NBA_FANTASY_PTS_RANK', 'DD2_RANK', 'TD3_RANK',
   'WNBA_FANTASY_PTS_RANK', 'AVAILABLE_FLAG','NICKNAME', 'TEAM_ID','TEAM_NAME','DD2','TD3','WNBA_FANTASY_PTS','BLKA','PFD'],axis = 1)
    gamelogs_df = calculate_additional_stats(gamelogs_df)
    
    gamelogs_df['GAME_DATE'] = gamelogs_df['GAME_DATE'].astype(str)
    gamelogs_df.drop(['NBA_FANTASY_PTS','FT_PCT','PLUS_MINUS', 'MIN_SEC'],axis=1, inplace = True)
    return gamelogs_df

#function to retrieve team stats
def team_stats(category,team, date):
    if category == 'Traditional':
        df = fetch_opponent_data(date) if date else fetch_data_from_table('General Opponent Stats')
        df['OPP_STL+BLK'] = df['OPP_STL'] + df['OPP_BLK']
        df['OPP_STL+BLK_RANK'] = df['OPP_STL+BLK'].rank(method='min', ascending=True)
    elif category == 'Playtypes':
        df = fetch_data_from_table('team_play_types')
        columns = [c for c in df.columns if 'eam' not in c or 'EAM' not in c]
        for col in columns:
            name = f'{col}_RANK'
            df[name] = df[col].rank(method='min', ascending = True)
        del df['Team_ID']
        del df['team']
    elif category == 'Assists':
        df = fetch_data_from_table('processed_team_assists')
        abbr = nba_team_to_abbreviation(team)
        df = df[df['Name'] == abbr]
        return df
    elif category == 'Zone Shooting':
        df = fetch_opp_shooting_zone_data(date) if date else fetch_data_from_table('opp_shooting_zone')
        
    df = df[df['TEAM_NAME'] == team]
    return df

# fetch overall opponent data
def fetch_opponent_data(date_filter = None):
    response = endpoints.LeagueDashTeamStats(measure_type_detailed_defense = 'Opponent',per_mode_detailed = 'Per48',date_from_nullable = date_filter)
    return response.get_data_frames()[0]

# process and store yearly opponent data
def process_opponent_scoring():
    df = fetch_opponent_data()
    df.to_sql('General Opponent Stats', engine, if_exists = 'replace', index =False)
    
# fetch opponent shooting dashboard from nba API
def fetch_opp_shooting_data(type, date_filter=None):
    response  = endpoints.LeagueDashOppPtShot(general_range_nullable = type, date_from_nullable = date_filter)
    return response.get_data_frames()[0]

# fetch opponent zone shooting dashboard
def fetch_opp_shooting_zone_data(date_filter = None):
    response = endpoints.LeagueDashTeamShotLocations(distance_range = 'By Zone', measure_type_simple = 'Opponent', per_mode_detailed = 'PerGame',date_from_nullable = date_filter)
    opp_zone_df = response.get_data_frames()[0]
    opp_zone_df.columns = ['_'.join(filter(None, col)).strip() for col in opp_zone_df.columns]
    columns = [a for a in opp_zone_df.columns if 'OPP' in a and 'PCT' not in a and 'Backcourt' not in a]
    for c in columns:
        col_name = f'{c}_RANK'
        opp_zone_df[col_name] = opp_zone_df[c].rank(method = 'min', ascending = True)
    return opp_zone_df
    

# process and store total yearly opp shooting data
def process_opp_shooting():
    types = ['Catch and Shoot', 'Pullups', 'Less Than 10 ft']
    for type in types:
        try:
            df = fetch_opp_shooting_data(type)
            df['FG3M'] = df['FG3M'] / df['GP']
            df['FG2M'] = df['FG2M'] / df['GP']
            df.to_sql(f'{type}', engine, if_exists='replace', index=False)
        except Exception as e:
            print(f"Error fetching data for play type {type}: {e}")
            continue

#process opponent shot zone data
def process_opp_shooting_zone():
    opp_zone_df = fetch_opp_shooting_zone_data()
    opp_zone_df.to_sql('opp_shooting_zone', engine, if_exists='replace', index=False)
    
#Function to fetch shooting zone data from NBA API
def fetch_player_zone(date_filter = None):
    return endpoints.LeagueDashPlayerShotLocations(distance_range = 'By Zone', date_from_nullable = date_filter).get_data_frames()[0]

#function to process player shooting zone data and store it in database
def process_player_zone():
    player_zones = fetch_player_zone()
    player_zones.columns = ['_'.join(filter(None, col)).strip() for col in player_zones.columns]
    player_zones = player_zones[[c for c in player_zones.columns if ('FGM' in c or '_NAME' in c) and 'Back' not in c]]
    for col in player_zones.columns:
        if 'NAME' not in col:
            player_zones[col]  = player_zones[col] * 2 if '3' not in col else player_zones[col] * 3
        

    sums = player_zones.drop(['PLAYER_NAME'], axis = 1).sum(axis=1)

    player_zones['Sum'] = sums

    for col in player_zones.columns:
        if 'NAME' not in col:
            percentage_column_name = col.split('_')[0] + "_PTS%"
            player_zones[percentage_column_name] = player_zones[col] / player_zones['Sum'] * 100  
    player_zones.drop([c for c in player_zones.columns if 'PTS%' not in c and 'NAME' not in c],axis = 1, inplace=True)
    player_zones.fillna(0, inplace=True)
    player_zones.to_sql('player_shooting_zones', engine, if_exists='replace', index=False)
        
# Function to fetch play type data for teams from the NBA API
def fetch_team_play_type_data(play_type):
    response = endpoints.SynergyPlayTypes(play_type_nullable=play_type, player_or_team_abbreviation='T', type_grouping_nullable='Defensive')
    return response.get_data_frames()[0]

# Map team names to abbreviations
def nba_team_to_abbreviation(team_name):
    nba_teams = teams.get_teams()
    team_abbr_map = {team['full_name']: team['abbreviation'] for team in nba_teams}
    return team_abbr_map.get(team_name, "Unknown")


# Function to process and store team data
def process_and_store_team_data():
    playtypes = [
        'Transition', 'Isolation', 'PRBallHandler', 'PRRollman', 'OffRebound',
        'Spotup', 'Cut', 'Handoff', 'OffScreen', 'Misc', 'Postup'
    ]
    team_dfs = []
    # Fetch data for each play type and store in a list of dataframes
    for play_type in playtypes:
        try:
            df = fetch_team_play_type_data(play_type)
            df['PTS/G'] = df['PTS'] / df['GP']
            team_dfs.append(df)
        except Exception as e:
            print(f"Error fetching data for play type {play_type}: {e}")
            continue

    # Combine all dataframes into one
    combined_team_df = pd.concat(team_dfs, ignore_index=True)
    
    playtypes = [
        'Transition', 'Isolation', 'PRBallHandler', 'PRRollMan', 'OffRebound',
        'Spotup', 'Cut', 'Handoff', 'OffScreen', 'Misc', 'Postup'
    ]
    # Calculate the mean points per game for each play type
    mean_pts_per_game = {play_type: combined_team_df[combined_team_df['PLAY_TYPE'] == play_type]['PTS/G'].mean() for play_type in playtypes}
    # Calculate PTS/G+ for each play type
    for play_type in playtypes:
        combined_team_df.loc[combined_team_df['PLAY_TYPE'] == play_type, 'PTS/G+'] = combined_team_df['PTS/G'] / mean_pts_per_game[play_type]

    # Pivot the combined dataframe
    teams_df = combined_team_df.pivot_table(
        index='TEAM_NAME',
        columns='PLAY_TYPE',
        values='PTS/G+',
        aggfunc='first'
    ).reset_index()

    nba_teams = teams.get_teams()
    
    # Correct team names and add team IDs
    teams_df.loc[teams_df['TEAM_NAME'] == 'LA Clippers', 'TEAM_NAME'] = 'Los Angeles Clippers'
    team_ids = {team['full_name']: team['id'] for team in nba_teams}
    teams_df['Team_ID'] = teams_df['TEAM_NAME'].map(team_ids)
    new_order = ['TEAM_NAME', 'Cut', 'Isolation', 'PRRollMan', 'PRBallHandler', 'OffRebound', 'Spotup', 'Handoff', 'OffScreen', 'Misc', 'Postup', 'Transition', 'Team_ID']
    teams_df = teams_df[new_order]
    teams_df['team'] = teams_df['TEAM_NAME'].apply(nba_team_to_abbreviation)

    # Store the final dataframe in the database
    teams_df.to_sql('team_play_types', engine, if_exists='replace', index=False)


# Function to fetch play type data from the NBA API
def fetch_play_type_data(play_type):
    response = endpoints.SynergyPlayTypes(play_type_nullable=play_type, player_or_team_abbreviation='P', type_grouping_nullable='Offensive')
    return response.get_data_frames()[0]

# Function to process and store data
def process_playstyles():
    playtypes = [
        'Transition', 'Isolation', 'PRBallHandler', 'PRRollman', 'OffRebound',
        'Spotup', 'Cut', 'Handoff', 'OffScreen', 'Misc', 'Postup'
    ]
    dfs = []
    # Fetch data for each play type and store in a list of dataframes
    for play_type in playtypes:
        try:
            df = fetch_play_type_data(play_type)
            print(f"Fetched data for play type: {play_type}")
            dfs.append(df)
        except Exception as e:
            print(f"Error fetching data for play type {play_type}: {e}")
            continue

    # Combine all dataframes into one
    combined_df = pd.concat(dfs, ignore_index=True)
    
    # Pivot the combined dataframe
    pivot_df = combined_df.pivot_table(
        index='PLAYER_NAME',
        columns='PLAY_TYPE',
        values='PTS',
        aggfunc='first'
    ).reset_index()

    # Merge team information
    player_team_df = combined_df.groupby('PLAYER_NAME')['TEAM_ABBREVIATION'].agg('first').reset_index()
    pivot_df = pd.merge(pivot_df, player_team_df, on='PLAYER_NAME', how='left')
    
    # Calculate the sum of points for each player
    sums = pivot_df.drop(['PLAYER_NAME', 'TEAM_ABBREVIATION'], axis=1).sum(axis=1)
    pivot_df['Sum'] = sums
    playtypes = ['Cut', 'Isolation', 'PRRollMan', 'PRBallHandler', 'OffRebound', 'Spotup', 'Handoff', 'OffScreen', 'Misc', 'Postup','Transition']
    
    # Calculate percentage of points for each play type
    for play_type in playtypes:
        percentage_column_name = play_type + '%'
        pivot_df[percentage_column_name] = pivot_df[play_type] / pivot_df['Sum'] * 100

    pivot_df.drop(playtypes, axis=1, inplace=True)
    pivot_df.drop('Sum', axis=1, inplace=True)
    
    pivot_df.fillna(0, inplace=True)

    pivot_df.to_sql('player_play_types', engine, if_exists='replace', index=False)

    
# API endpoint to trigger updating of database
@app.route('/api/update_database', methods=['GET'])
def store_database():
    try:
        process_opponent_scoring()
        process_and_store_team_data()
        process_opp_shooting()
        process_opp_shooting_zone()
        process_playstyles()
        process_player_zone()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

        

@lru_cache(maxsize=32)
def get_player_game_logs_with_ratings(player_name):
    """
    Retrieve game logs for a player and calculate ratings.
    This function is cached to avoid redundant calculations.
    """
    game_logs_df = game_log(player_name)
    
    # Calculate Playtype rating and assist location rating
    game_logs_df['PLAYTYPE_RTG'] = game_logs_df.apply(
        lambda row: calculate_matchup_rating(row['PLAYER_NAME'], get_opponent_team(row['MATCHUP'])), 
        axis=1
    )
    game_logs_df['AST_LOC_RTG'] = game_logs_df.apply(
        lambda row: calculate_assist_location_rating(row['PLAYER_NAME'], get_opponent_team(row['MATCHUP'])), 
        axis=1
    )
    
    return game_logs_df

def apply_filters(df, filter_params):
    """Apply filters to the DataFrame without recalculating ratings."""
    # Extract filter parameters
    minutes_filter = filter_params.get('minutes_filter', (0, 48))
    players_on = filter_params.get('players_on', [])
    players_off = filter_params.get('players_off', [])
    date_filter = filter_params.get('date_filter')
    teams_against = filter_params.get('teams_against', [])
    location_filter = filter_params.get('location_filter', 'Both')
    game_filter = filter_params.get('game_filter')
    playstyle_range = filter_params.get('playstyle_range', [75, 125])
    self_filters = filter_params.get('self_filters', {})

    # Apply filters
    df = df[(df['MIN'] >= minutes_filter[0]) & (df['MIN'] <= minutes_filter[1])]
    
    if date_filter:
        df['GAME_DATE'] = pd.to_datetime(df['GAME_DATE'])
        df = df[df['GAME_DATE'] >= date_filter]
    
    if teams_against:
        df['OPPONENT'] = df['MATCHUP'].apply(lambda x: x.split()[-1].upper())
        df = df[df['OPPONENT'].isin(teams_against)]
        
    if location_filter != 'Both':
        df = df[df['MATCHUP'].str.contains('vs' if location_filter == 'Home' else '@')]
    
    df = df[(df['PLAYTYPE_RTG'] >= playstyle_range[0]) & (df['PLAYTYPE_RTG'] <= playstyle_range[1])]
    
    for column, range_values in self_filters.items():
        if column in df.columns:
            df = df[(df[column] >= range_values[0]) & (df[column] <= range_values[1])]
    
    if game_filter:
        df = df.head(int(game_filter))
        
    df = filter_players_on_off(df, players_on, players_off, '2023-24')
    
    
    return df

@app.route('/api/game_logs', methods=['GET'])
def get_game_log():
    player_name = request.args.get('player_name')
    # Retrieve game logs with pre-calculated ratings
    full_game_logs = get_player_game_logs_with_ratings(player_name)
    
    filter_params = {
        'minutes_filter': tuple(map(int, request.args.get('minutes_filter', '0,48').split(','))),
        'players_on': request.args.getlist('players_on[]'),
        'players_off': request.args.getlist('players_off[]'),
        'date_filter': request.args.get('date_filter'),
        'teams_against': request.args.getlist('teams_against[]'),
        'rank_filter': request.args.getlist('filter_numbers[]'),
        'location_filter': request.args.get('location_filter', 'Both'),
        'game_filter': request.args.get('game_filter'),
        'playstyle_range': [
            float(request.args.get('playstyle_RTG_min', '75')),
            float(request.args.get('playstyle_RTG_max', '125'))
        ],
        'self_filters': {
            key[13:-1]: list(map(float, value.split(',')))
            for key, value in request.args.items()
            if key.startswith('self_filters[') and key.endswith(']')
        }
    }
    
    teams_against = None
    for index, ele in enumerate(filter_params['teams_against']):
        filtered_teams = set(filter_teams(ele, int(filter_params['rank_filter'][index]), filter_params['date_filter']))
        
        if teams_against is None:
            teams_against = filtered_teams
        else:
            teams_against = teams_against.intersection(filtered_teams)
            
    if teams_against is None:
        teams_against = set()
    
    filter_params['teams_against'] = teams_against
    filtered_game_logs = apply_filters(full_game_logs, filter_params)
    filtered_game_logs.drop(['PLAYER_NAME','GAME_ID','TEAM_ABBREVIATION'], axis = 1, inplace = True)
    filtered_game_logs['GAME_DATE'] = pd.to_datetime(filtered_game_logs['GAME_DATE']).dt.date
    filtered_game_logs['GAME_DATE'] = filtered_game_logs['GAME_DATE'].astype(str)
    
    # Calculate averages and prepare response
    average_columns = ['MIN', 'PTS', 'REB', 'AST', 'PRA', 'PA', 'PR', 'RA', 'FD_PTS', 'FGM', 'FGA', 'FG3M', 'FG3A', 'FTM', 'FTA', 'OREB', 'DREB', 'TOV', 'STL', 'BLK', 'PF', 'STKS']
    averages = filtered_game_logs[average_columns].mean().round(2).to_frame().T
    
    game_logs_json = filtered_game_logs.to_json(orient='records', date_format='iso')
    averages_json = averages.to_json(orient='records', date_format='iso')
    
    average_season = full_game_logs[average_columns].mean().round(2).to_frame().T
    season_json = average_season.to_json(orient='records', date_format='iso')
    
    return jsonify({
        'game_logs': game_logs_json,
        'averages': averages_json,
        'season_averages': season_json,
        'next_game'  : "Houston Rockets"
    })
    
@app.route('/api/players', methods=['GET'])
def get_players():
    df = fetch_data_from_table('player_play_types')
    players = df['PLAYER_NAME'].unique().tolist()
    return jsonify(players)

@app.route('/api/team_stats', methods=['GET'])
def get_team_stats():
    category = request.args.get('category')
    team = request.args.get('team')
    date = request.args.get('date')
    df = team_stats(category, team, date)
    if df.empty:
        return jsonify({"error": "No data found for the specified team and category"}), 404
    team_stats_dict = df.to_dict(orient='records')[0]
    return jsonify(team_stats_dict)

@app.route('/api/get_teams',methods =['GET'])
def get_teams():
    team = teams.get_teams()
    team_names = [d['full_name'] for d in team]
    return jsonify(team_names)

def get_player_playtypes(player_name):
    df = fetch_data_from_table('player_play_types')
    df = df[df['PLAYER_NAME'] == player_name]
    playtypes= df.to_dict(orient='records')[0]
    return playtypes

#Fetch opponent stats from PBP endpoint
def fetch_PBP_opponent():
    url = 'https://api.pbpstats.com/get-totals/nba?Season=2023-24&SeasonType=Regular%2BSeason&StartType=All&Type=Opponent'

    try:
        response = requests.get(url)
        response.raise_for_status()  # Raise an exception for bad responses
        data = response.json()
        table_data = data['multi_row_table_data']

        df = pd.DataFrame(table_data)
        df = df.reset_index(drop=True)
        df.to_sql('pbp_opponent_stats', engine, if_exists='replace', index=False)

    except requests.RequestException as e:
        print(f"An error occurred while fetching data: {e}")
        return None
    
#Fetch player stats from PBP endpoint
def fetch_PBP_player():
    url = 'https://api.pbpstats.com/get-totals/nba?Season=2023-24&SeasonType=Regular%2BSeason&Type=Player'
    try:
        response = requests.get(url)
        response.raise_for_status()  # Raise an exception for bad responses
        data = response.json()
        table_data = data['multi_row_table_data']

        df = pd.DataFrame(table_data)
        df = df.reset_index(drop=True)
        df.to_sql('pbp_player_stats', engine, if_exists='replace', index=False)

    except requests.RequestException as e:
        print(f"An error occurred while fetching data: {e}")
        return None
    
@app.route('/api/player_PBP', methods = ['PUT'])
def store_PBP_player():
    try:
        fetch_PBP_player()
        return jsonify({'message': 'Player PBP data processed and stored successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@app.route('/api/opponent_PBP', methods = ['PUT'])
def store_PBP_opponent():
    try:
        fetch_PBP_opponent()
        return jsonify({'message': 'Opponent PBP data processed and stored successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Function to fetch players dictionary from nba api
@app.route('/api/fetch_players', methods = ['PUT'])
def fetch_players():
    try: 
        player_dict = players.get_players() #can try to interface with database later
        player_df = pd.DataFrame.from_dict(player_dict)
        player_df.to_sql('Player_Information', engine, if_exists = 'replace', index =False)
        return jsonify({'message': 'Player dict processed and stored successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
#Function to return player profile data
@app.route('/api/player_profile', methods = ['GET'])
def get_player_profile_data():
    player = request.args.get('player_name')
    category = request.args.get('category')
    if category == 'playstyles':
        dict = get_player_playtypes(player)
    elif category == 'assists':
        df = fetch_data_from_table('processed_player_assists')
        df = df[df['Name'] == player]
        dict = df.to_dict(orient = 'records')
    #elif category == 'shooting zones':
        

    
    return jsonify(dict)

def process_assist_data():
    teams_df = fetch_data_from_table('pbp_opponent_stats')
    players_df = fetch_data_from_table('pbp_player_stats')
    
    
    teams_df = teams_df[["Name","Assists","AssistPoints", "TwoPtAssists","ThreePtAssists","Arc3Assists","Corner3Assists","AtRimAssists","ShortMidRangeAssists","LongMidRangeAssists"]]
    columns = ["Assists","AssistPoints", "TwoPtAssists","ThreePtAssists","Arc3Assists","Corner3Assists","AtRimAssists","ShortMidRangeAssists","LongMidRangeAssists"]
    means = teams_df[columns].mean()
    for c in columns:
        teams_df[c] = teams_df[c]/means[c]
    
    names = []
    for col in columns:
        name = f'{col}_RANK'
        names.append(name)
        teams_df[name] = teams_df[col].rank(method='min', ascending = True)
    
    players_df = players_df[["Name","TwoPtAssists","ThreePtAssists","Arc3Assists","Corner3Assists","AtRimAssists","ShortMidRangeAssists","LongMidRangeAssists"]]
    sum = players_df.drop(["Name",'TwoPtAssists','ThreePtAssists'], axis=1).sum(axis=1)
    cats = ["Arc3Assists","Corner3Assists","AtRimAssists","ShortMidRangeAssists","LongMidRangeAssists"]
    for c in cats:
        players_df[c] = players_df[c] / sum * 100
    
    teams_df.to_sql('processed_team_assists', engine, if_exists='replace', index=False)
    players_df.to_sql('processed_player_assists', engine, if_exists='replace', index=False)
    print('hi')
        
    
# Run the Flask app
if __name__ == '__main__':
    store_database()
    
    app.run(debug=True)
    
    
