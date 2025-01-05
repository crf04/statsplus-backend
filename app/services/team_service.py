import pandas as pd
from nba_api.stats.static import teams
from nba_api.stats.endpoints import (
    LeagueDashTeamStats, 
    LeagueDashOppPtShot,
    LeagueDashTeamShotLocations
)

class TeamService:
    def __init__(self, db_engine):
        self.engine = db_engine
        
    def get_all_teams(self):
        team = teams.get_teams()
        team_names = [d['full_name'] for d in team]
        return team_names

    def get_team_stats(self, category, team, date=None):
        if category == 'Traditional':
            df = self._fetch_opponent_data(date) if date else self._fetch_data_from_table('General Opponent Stats')
            df['OPP_STL+BLK'] = df['OPP_STL'] + df['OPP_BLK']
            df['OPP_STL+BLK_RANK'] = df['OPP_STL+BLK'].rank(method='min', ascending=True)
            team = 'LA Clippers' if team == 'Los Angeles Clippers' else team
        elif category == 'Playtypes':
            df = self._fetch_data_from_table('team_play_types')
            columns = [c for c in df.columns if 'eam' not in c or 'EAM' not in c]
            for col in columns:
                name = f'{col}_RANK'
                df[name] = df[col].rank(method='min', ascending=True)
            del df['Team_ID']
            del df['team']
            print(df)
        elif category == 'Assists':
            df = self._fetch_data_from_table('processed_team_assists')
            abbr = self._nba_team_to_abbreviation(team)
            df = df[df['Name'] == abbr]
            return df.to_dict(orient='records')[0]
        elif category == 'Zone Shooting':
            df = self._fetch_opp_shooting_zone_data(date) if date else self._fetch_data_from_table('opp_shooting_zone')
        elif category == 'Shooting Type':
            combined_df = pd.DataFrame()
            types = ['Catch and Shoot', 'Pullups', 'Less Than 10 ft']

            for shooting_type in types:
                df = self._fetch_opp_shooting_data(shooting_type, date) if date else self._fetch_data_from_table(shooting_type)
                if date:
                    df['FG2A_RANK'] = df['FG2A'].rank(method='min', ascending=True)
                    df['FG3A_RANK'] = df['FG3A'].rank(method='min', ascending=True)
                    df['FG2M_RANK'] = df['FG2M'].rank(method='min', ascending=True)
                    df['FG3M_RANK'] = df['FG3M'].rank(method='min', ascending=True)
                df['PTS'] = df['FG2M'] * 2 + df['FG3M'] * 3
                df['PTS_RANK'] = df['PTS'].rank(method='min', ascending=True)
                df = df[df['TEAM_NAME'] == team]
                df['ShootingType'] = shooting_type  # Add the ShootingType column
                
                combined_df = pd.concat([combined_df, df])
            del combined_df['FG3_PCT']
            return combined_df.to_dict(orient='records')
        return df[df['TEAM_NAME'] == team].to_dict(orient='records')[0]

    def _fetch_opp_shooting_data(self, type, date_filter=None):
        return LeagueDashOppPtShot(
            general_range_nullable=type,
            date_from_nullable=date_filter,
            per_mode_simple='PerGame'
        ).get_data_frames()[0]
    

    def _fetch_data_from_table(self, table_name):
        query = f"SELECT * FROM '{table_name}'"
        with self.engine.connect() as conn:
            return pd.read_sql(query, conn)

    def _fetch_opponent_data(self, date_filter=None):
        response = LeagueDashTeamStats(
            measure_type_detailed_defense='Opponent',
            per_mode_detailed='Per48',
            date_from_nullable=date_filter
        )
        return response.get_data_frames()[0]

    def _fetch_opp_shooting_zone_data(self, date_filter=None):
        response = LeagueDashTeamShotLocations(
            distance_range='By Zone',
            measure_type_simple='Opponent',
            per_mode_detailed='PerGame',
            date_from_nullable=date_filter
        )
        opp_zone_df = response.get_data_frames()[0]
        opp_zone_df.columns = ['_'.join(filter(None, col)).strip() for col in opp_zone_df.columns]
        
        columns = [a for a in opp_zone_df.columns if 'OPP' in a and 'PCT' not in a and 'Backcourt' not in a]
        for c in columns:
            col_name = f'{c}_RANK'
            opp_zone_df[col_name] = opp_zone_df[c].rank(method='min', ascending=True)
            
        return opp_zone_df

    @staticmethod
    def _nba_team_to_abbreviation(team_name):
        nba_teams = teams.get_teams()
        team_abbr_map = {team['full_name']: team['abbreviation'] for team in nba_teams}
        return team_abbr_map.get(team_name, "Unknown")