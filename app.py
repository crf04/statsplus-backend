from flask import Flask, jsonify, request
from flask_cors import CORS
from nba_api.stats import endpoints
from nba_api.stats.static import teams, players
from nba_api.stats.endpoints import playergamelogs
import pandas as pd
from sqlalchemy import create_engine
import requests

# Initialize Flask app
app = Flask(__name__)
CORS(app)  # Enable CORS

# Set up database connection
engine = create_engine('sqlite:///nba_play_types.db')


#function to retrieve team stats
def team_stats(category,team):
    if category == 'Traditional':
        df = fetch_data_from_table('General Opponent Stats')
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
    df = df[df['TEAM_NAME'] == team]
    return df

# Function to fetch players dictionary from nba api
def fetch_players():
    player_dict = players.get_players() #can try to interface with database later
    return player_dict
    

# Function to find common games between players
def get_common_games(primary_player_logs, other_players_names,season='2023-24'):
    # Combine GAME_ID and TEAM_ABBREVIATION into a single identifier for primary player's games
    primary_game_team_pairs = set(zip(primary_player_logs['GAME_ID'], primary_player_logs['TEAM_ABBREVIATION']))
    
    player_dict = fetch_players()
    # Loop through other players and find intersections based on game IDs and team abbreviations
    for player_name in other_players_names:
        player = next((player for player in player_dict if player['full_name'] == player_name), None)
        if not player:
            continue  # If player not found, skip to the next iteration
        
        player_id = player['id']
        player_gamelogs = playergamelogs.PlayerGameLogs(player_id_nullable=player_id, season_nullable=season).get_data_frames()[0]
        player_game_team_pairs = set(zip(player_gamelogs['GAME_ID'], player_gamelogs['TEAM_ABBREVIATION']))
        
        # Intersect with primary_game_team_pairs to find games and teams where both played
        primary_game_team_pairs = primary_game_team_pairs.intersection(player_game_team_pairs)
        
        # If at any point the intersection is empty, no need to continue
        if not primary_game_team_pairs:
            break
    
    # Extract just the GAME_IDs from the intersecting pairs for further use
    common_game_ids = {pair[0] for pair in primary_game_team_pairs}
    
    return set(common_game_ids)

# Function to find games to exclude due to filtering
def get_games_to_exclude(player_logs, players_off_names, season= '2023-24'):
    exclude_game_ids = set()
    player_dict = fetch_players()
    
    # Loop through players_off and union game IDs
    for player_name in players_off_names:
        player = next((player for player in player_dict if player['full_name'] == player_name), None)
        if not player:
            continue
        
        player_id = player['id']
        player_gamelogs = playergamelogs.PlayerGameLogs(player_id_nullable=player_id, season_nullable=season).get_data_frames()[0]
        player_game_ids = set(player_gamelogs['GAME_ID'])
        
        # Union with exclude_game_ids to accumulate games where any player_off played
        exclude_game_ids |= player_game_ids

    return exclude_game_ids

# Function to calculate the player matchup rating
def calculate_matchup_rating(player_name,team):
    teams_df = fetch_data_from_table('team_play_types')
    players_df = fetch_data_from_table('player_play_types')

    player_data = players_df[players_df['PLAYER_NAME'] == player_name]
    team_data = teams_df[teams_df['team'] == team]
    matchupRTG = 0
    playtypes = ['Cut', 'Isolation', 'PRRollMan', 'PRBallHandler', 'OffRebound', 'Spotup', 'Handoff', 'OffScreen', 'Misc', 'Postup','Transition']
    for playtype in playtypes:
        matchup_score = player_data[playtype + '%'].values[0] * team_data[playtype].values[0]
        matchupRTG += matchup_score
    
    return matchupRTG
        

    
# Function to retrieve all relevant games based on the user filtering
def game_log(player_name, minutes_filter = (0,48), players_on = [], players_off = [],date_filter = None, teams_against = [], location_filter = 'Both', last_games = None, playstyle_filter = None):
    player_dict = fetch_players()
    player = [player for player in player_dict if player['full_name'] == player_name][0]
    player_id = player['id']
    gamelogs = playergamelogs.PlayerGameLogs(player_id_nullable=player_id, season_nullable='2023-24')
    gamelogs_df = gamelogs.get_data_frames()[0]
    gamelogs_df = gamelogs_df.drop(['SEASON_YEAR','PLAYER_ID','GP_RANK', 'W_RANK', 'L_RANK', 'W_PCT_RANK','MIN_RANK', 'FGM_RANK', 'FGA_RANK', 'FG_PCT_RANK', 'FG3M_RANK',
   'FG3A_RANK', 'FG3_PCT_RANK', 'FTM_RANK', 'FTA_RANK', 'FT_PCT_RANK',
   'OREB_RANK', 'DREB_RANK', 'REB_RANK', 'AST_RANK', 'TOV_RANK',
   'STL_RANK', 'BLK_RANK', 'BLKA_RANK', 'PF_RANK', 'PFD_RANK', 'PTS_RANK',
   'PLUS_MINUS_RANK', 'NBA_FANTASY_PTS_RANK', 'DD2_RANK', 'TD3_RANK',
   'WNBA_FANTASY_PTS_RANK', 'AVAILABLE_FLAG','NICKNAME', 'TEAM_ID','TEAM_NAME','DD2','TD3','WNBA_FANTASY_PTS','BLKA','PFD'],axis = 1)
    gamelogs_df['PRA'] = gamelogs_df['PTS'] + gamelogs_df['REB'] + gamelogs_df['AST']
    gamelogs_df['PA'] = gamelogs_df['PTS'] + gamelogs_df['AST']
    gamelogs_df['PR'] = gamelogs_df['PTS'] + gamelogs_df['REB']
    gamelogs_df['RA'] = gamelogs_df['REB'] + gamelogs_df['AST']
    gamelogs_df['STKS'] = gamelogs_df['STL'] + gamelogs_df['BLK']
    gamelogs_df = gamelogs_df[gamelogs_df['MIN'] >= minutes_filter[0]]
    gamelogs_df = gamelogs_df[gamelogs_df['MIN'] <= minutes_filter[1]]
    gamelogs_df['GAME_DATE'] = pd.to_datetime(gamelogs_df['GAME_DATE'])
    gamelogs_df['OPP'] = gamelogs_df['MATCHUP'].apply(get_opponent_team)
    gamelogs_df['PLAYSTYLE_RTG'] = gamelogs_df.apply(lambda row: calculate_matchup_rating(player_name, row['OPP']), axis=1)
    if playstyle_filter is not None:
        gamelogs_df = gamelogs_df[gamelogs_df['PLAYSTYLE_RTG'] > int(playstyle_filter[0])]
        gamelogs_df = gamelogs_df[gamelogs_df['PLAYSTYLE_RTG'] < int(playstyle_filter[1])]
    gamelogs_df['MIN'] = gamelogs_df['MIN'].round().astype(int)
    if len(teams_against) != 0:
        gamelogs_df = gamelogs_df[gamelogs_df['OPP'].isin(teams_against)]
    if date_filter is not None:
        if not pd.isnull(date_filter):
            date_filter = pd.to_datetime(date_filter)
            # Filter the DataFrame to include only rows with 'GAME_DATE' on or after 'date_filter'
            gamelogs_df = gamelogs_df[gamelogs_df['GAME_DATE'] >= date_filter]
    gamelogs_df['FD_PTS'] = gamelogs_df['NBA_FANTASY_PTS']
    common_game_ids = gamelogs_df['GAME_ID']
    exclude_game_ids = []
    if players_on:
        common_game_ids = get_common_games(gamelogs_df,players_on)
    if players_off:
        exclude_game_ids = get_games_to_exclude(gamelogs_df, players_off)
    final_game_ids = set(common_game_ids) - set(exclude_game_ids)
    gamelogs_df = gamelogs_df[gamelogs_df['GAME_ID'].isin(final_game_ids)]
    gamelogs_df = gamelogs_df.drop(['NBA_FANTASY_PTS','OPP','TEAM_ABBREVIATION','GAME_ID','PLAYER_NAME','FT_PCT'],axis=1)
    gamelogs_df.rename(columns={'PLUS_MINUS':'+/-'},inplace = True)
    if location_filter == 'Home':
        gamelogs_df = gamelogs_df[gamelogs_df['MATCHUP'].str.contains('vs')]
    elif location_filter == 'Away':
        gamelogs_df = gamelogs_df[gamelogs_df['MATCHUP'].str.contains('@')]
    if last_games:
        gamelogs_df = gamelogs_df.head(int(last_games))
    gamelogs_df['GAME_DATE'] = gamelogs_df['GAME_DATE'].astype(str)
    return gamelogs_df    

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
        
    if rank_filter >= 0:
        return df.head(rank_filter)['team'].tolist()
    else:
        return df.tail(-rank_filter)['team'].tolist()

# Functions that return a df of the teams that meet the criteria defined
def general_opp_filtering(filter, date_filter):
    if date_filter is not None:
        date_filter = pd.to_datetime(date_filter)
        df = endpoints.LeagueDashTeamStats(measure_type_detailed_defense = 'Opponent',per_mode_detailed = 'Per48',date_from_nullable = date_filter).get_data_frames()[0]
    else:
        df = fetch_data_from_table('General Opponent Stats')
    df['OPP_STOCKS'] = df['OPP_BLK'] + df['OPP_STL']
    df['team'] = df['TEAM_NAME'].apply(nba_team_to_abbreviation)
    return df.sort_values(by = filter, ascending = False)
      
def playtype_filtering(filter):
    df = fetch_data_from_table('team_play_types')
    return df.sort_values(by= filter, ascending=False)
        
    
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
    
# Function to fetch data from a table
def fetch_data_from_table(table_name):
    query = f"SELECT * FROM '{table_name}'"
    with engine.connect() as conn:
        df = pd.read_sql(query, conn)
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
    response = endpoints.LeagueDashTeamShotLocations(distance_range = 'By Zone', measure_type_simple = 'Opponent', per_mode_detailed = 'PerGame')
    return response.get_data_fames()[0]
    

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
    opp_zone_df.columns = ['_'.join(filter(None, col)).strip() for col in opp_zone_df.columns]
    opp_zone_df.to_sql('opp_shooting_zone', engine, if_exists='replace', index=False)
        
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
def process_and_store_data():
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

    # Drop play type columns and the sum column
    pivot_df.drop(playtypes, axis=1, inplace=True)
    pivot_df.drop('Sum', axis=1, inplace=True)
    
    # Fill NaN values with 0
    pivot_df.fillna(0, inplace=True)

    # Store the final dataframe in the database
    pivot_df.to_sql('player_play_types', engine, if_exists='replace', index=False)

# API endpoint to trigger data processing and storage
@app.route('/api/store_play_types', methods=['PUT'])
def store_play_types():
    try:
        process_and_store_data()
        return jsonify({'message': 'Data processed and stored successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
# API endpoint to trigger team data processing and storage
@app.route('/api/store_team_play_types', methods=['PUT'])
def store_team_play_types():
    try:
        process_and_store_team_data()
        return jsonify({'message': 'Team data processed and stored successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
# API endpoint to trigger opponent scoring data processing and storage
@app.route('/api/store_opponent_scoring', methods=['GET'])
def store_opponent_scoring():
    try:
        process_opponent_scoring()
        return jsonify({'message': 'Opponent scoring data processed and stored successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# API endpoint to trigger opponent shooting data processing and storage
@app.route('/api/store_opp_shooting', methods=['PUT'])
def store_opp_shooting():
    try:
        process_opp_shooting()
        return jsonify({'message': 'Opponent shooting data processed and stored successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/store_opp_shooting_zone', methods = ['PUT'])
def store_opp_shooting_zone():
    try:
        process_opp_shooting_zone()
        return jsonify({'message': 'Opponent shooting zone data processed and stored successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
        

# API endpoint to retrieve gamelogs after user filtering on frontend
@app.route('/api/game_logs', methods = ['GET'])
def get_game_log():
    # Extract parameters from the request
    player_name = request.args.get('player_name')
    minutes_filter = request.args.get('minutes_filter', '0,48')
    players_on = request.args.getlist('players_on[]')
    players_off = request.args.getlist('players_off[]')
    date_filter = request.args.get('date_filter')
    filters = request.args.getlist('teams_against[]')
    rank_filter = request.args.getlist('filter_numbers[]')
    location_filter = request.args.get('location_filter')
    game_filter = request.args.get('game_filter')
    playstyle_min = request.args.get('playstyle_RTG_min', '75')
    playstyle_max = request.args.get('playstyle_RTG_max', '125')
    # Convert the minutes_filter to a tuple of integers
    minutes_filter = tuple(map(int, minutes_filter.split(',')))
    teams_against = []
    for index, ele in enumerate(filters):
        teams_against.extend(filter_teams(ele, int(rank_filter[index]),date_filter))
    teams_against = set(teams_against)
    game_logs_df = game_log(player_name, minutes_filter, players_on, players_off, date_filter, teams_against, location_filter, game_filter, [playstyle_min,playstyle_max])
    
    average_columns = ['MIN','PTS','REB','AST', 'PRA','PA','PR','RA','FD_PTS', 'FGM','FGA','FG3M','FG3A','FTM','FTA','OREB','DREB','TOV','STL','BLK','PF','STKS']
    averages = game_logs_df[average_columns].mean().round(2).to_frame().T
    
    game_logs_json = game_logs_df.to_json(orient='records', date_format='iso')
    averages_json = averages.to_json(orient ='records',date_format = 'iso')
    
    season_logs = game_log(player_name)
    average_season = season_logs[average_columns].mean().round(2).to_frame().T
    season_json = average_season.to_json(orient ='records',date_format = 'iso')
    return jsonify({'game_logs': game_logs_json, 'averages': averages_json, 'season_averages': season_json})

@app.route('/api/players', methods=['GET'])
def get_players():
    df = fetch_data_from_table('player_play_types')
    players = df['PLAYER_NAME'].unique().tolist()  # Assuming 'player_name' is the column name
    return jsonify(players)

@app.route('/api/team_stats', methods=['GET'])
def get_team_stats():
    category = request.args.get('category')
    team = request.args.get('team')
    df = team_stats(category, team)
    if df.empty:
        return jsonify({"error": "No data found for the specified team and category"}), 404
    team_stats_dict = df.to_dict(orient='records')[0]  # Assuming we're always getting one row
    return jsonify(team_stats_dict)

@app.route('/api/get_teams',methods =['GET'])
def get_teams():
    team = teams.get_teams()
    team_names = [d['full_name'] for d in team]
    return jsonify(team_names)

@app.route('/api/player_playstyles', methods =['GET'])
def get_player_playtypes():
    player_name = request.args.get('player_name')
    df = fetch_data_from_table('player_play_types')
    df = df[df['PLAYER_NAME'] == player_name]
    playtypes= df.to_dict(orient='records')[0] 
    print(playtypes)
    return jsonify(playtypes)

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
    
@app.route('/api/opponent_PBP', methods = ['GET','PUT'])
def store_PBP_opponent():
    try:
        fetch_PBP_opponent()
        return jsonify({'message': 'Opponent PBP data processed and stored successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Run the Flask app
if __name__ == '__main__':
    app.run(debug=True)
    
