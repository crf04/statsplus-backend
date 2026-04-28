import pandas as pd
from .tables import normalize_table_name

def fetch_data_from_table(engine, table_name):
    """Fetch data from a database table (normalized for Postgres)."""
    normalized = normalize_table_name(table_name)
    query = f"SELECT * FROM {normalized}"
    with engine.connect() as conn:
        df = pd.read_sql(query, conn)
    return df

def get_player_id(engine, player_name):
    """Get player ID from player name"""
    player_dict = fetch_data_from_table(engine, 'player_information')
    player = player_dict[player_dict['full_name'] == player_name]
    player_id = player['id'].values[0]
    return player_id

def calculate_additional_stats(df):
    """Calculate additional statistics for game logs"""
    df['PRA'] = df['PTS'] + df['REB'] + df['AST']
    df['PA'] = df['PTS'] + df['AST']
    df['PR'] = df['PTS'] + df['REB']
    df['RA'] = df['REB'] + df['AST']
    df['STKS'] = df['STL'] + df['BLK']
    df['FD_PTS'] = df['NBA_FANTASY_PTS']
    df['+/-'] = df['PLUS_MINUS']
    df['MIN'] = df['MIN'].round().astype(int)
    return df

def get_opponent_team(match):
    """Extract opponent team from match string"""
    parts = match.split(' ')
    if 'vs.' in match:
        return parts[2]
    elif '@' in match:
        return parts[-1]
    else:
        return None

def nba_team_to_abbreviation(team_name):
    """Convert NBA team name to abbreviation"""
    team_mapping = {
        'Atlanta Hawks': 'ATL', 'Boston Celtics': 'BOS', 'Brooklyn Nets': 'BKN',
        'Charlotte Hornets': 'CHA', 'Chicago Bulls': 'CHI', 'Cleveland Cavaliers': 'CLE',
        'Dallas Mavericks': 'DAL', 'Denver Nuggets': 'DEN', 'Detroit Pistons': 'DET',
        'Golden State Warriors': 'GSW', 'Houston Rockets': 'HOU', 'Indiana Pacers': 'IND',
        'LA Clippers': 'LAC', 'Los Angeles Lakers': 'LAL', 'Memphis Grizzlies': 'MEM',
        'Miami Heat': 'MIA', 'Milwaukee Bucks': 'MIL', 'Minnesota Timberwolves': 'MIN',
        'New Orleans Pelicans': 'NOP', 'New York Knicks': 'NYK', 'Oklahoma City Thunder': 'OKC',
        'Orlando Magic': 'ORL', 'Philadelphia 76ers': 'PHI', 'Phoenix Suns': 'PHX',
        'Portland Trail Blazers': 'POR', 'Sacramento Kings': 'SAC', 'San Antonio Spurs': 'SAS',
        'Toronto Raptors': 'TOR', 'Utah Jazz': 'UTA', 'Washington Wizards': 'WAS'
    }
    return team_mapping.get(team_name, team_name) 
