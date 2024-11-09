from nba_api.stats.static import teams

def get_opponent_team(match):
    """Extract opponent team from matchup string"""
    parts = match.split(' ')
    if 'vs.' in match:
        return parts[2]
    elif '@' in match:
        return parts[-1]
    else:
        return None

def nba_team_to_abbreviation(team_name):
    """Convert full team name to abbreviation"""
    nba_teams = teams.get_teams()
    team_abbr_map = {team['full_name']: team['abbreviation'] for team in nba_teams}
    return team_abbr_map.get(team_name, "Unknown")

def calculate_additional_stats(df):
    """Calculate additional statistics for player game logs"""
    df['PRA'] = df['PTS'] + df['REB'] + df['AST']
    df['PA'] = df['PTS'] + df['AST']
    df['PR'] = df['PTS'] + df['REB']
    df['RA'] = df['REB'] + df['AST']
    df['STKS'] = df['STL'] + df['BLK']
    df['FD_PTS'] = df['NBA_FANTASY_PTS']
    df['+/-'] = df['PLUS_MINUS']
    df['MIN'] = df['MIN'].round().astype(int)
    return df