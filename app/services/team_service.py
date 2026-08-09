import pandas as pd
from nba_api.stats.static import teams
from app.config.settings import RuntimeSettings, get_runtime_settings
from app.models.catalogs import SHOOTING_TYPES
from app.providers.nba_stats import NBAStatsAdapter, NBAStatsProvider

class TeamService:
    def __init__(
        self,
        db_engine,
        settings: RuntimeSettings | None = None,
        nba_stats_provider: NBAStatsProvider | None = None,
    ):
        self.engine = db_engine
        self.settings = settings or get_runtime_settings()
        self.nba_stats = nba_stats_provider or NBAStatsAdapter(settings=self.settings)
    def get_all_teams(self):
        team = teams.get_teams()
        team_names = [d['full_name'] for d in team]
        return team_names

    def get_team_stats(self, category, team, date=None):
        if category == 'Traditional':
            df = self._fetch_opponent_data(date) if date else self._fetch_data_from_table('general_opponent_stats')
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
            df = df[df['TEAM_NAME'] == team]
            return df.to_dict(orient='records')[0]
        elif category == 'Assists':
            df = self._fetch_data_from_table('processed_team_assists')
            abbr = self._nba_team_to_abbreviation(team)
            df = df[df['Name'] == abbr]
            return df.to_dict(orient='records')[0]
        elif category == 'Zone Shooting':
            df = self._fetch_opp_shooting_zone_data(date) if date else self._fetch_data_from_table('opp_shooting_zone')
        elif category == 'Shooting Type':
            combined_df = pd.DataFrame()
            types = SHOOTING_TYPES

            for shooting_type in types:
                if date:
                    df = self._fetch_opp_shooting_data(shooting_type, date)
                else:
                    df = self._fetch_data_from_table(shooting_type.replace(' ', '_').lower())
                if date:
                    df['FG2A_RANK'] = df['FG2A'].rank(method='min', ascending=True)
                    df['FG3A_RANK'] = df['FG3A'].rank(method='min', ascending=True)
                    df['FG2M_RANK'] = df['FG2M'].rank(method='min', ascending=True)
                    df['FG3M_RANK'] = df['FG3M'].rank(method='min', ascending=True)
                df['PTS'] = df['FG2M'] * 2 + df['FG3M'] * 3
                df['PTS_RANK'] = df['PTS'].rank(method='min', ascending=True)
                cols = ['PTS', 'FG2M', 'FG3M', 'FG2A', 'FG3A']
                league_avg = df[cols].mean()
                for col in cols:
                    if league_avg[col] != 0:
                        df[f'{col}_vs_avg_pct'] = ((df[col] - league_avg[col]) / league_avg[col]) * 100
                df = df[df['TEAM_NAME'] == team]
                df['ShootingType'] = shooting_type 
                             
                combined_df = pd.concat([combined_df, df])
            combined_df.fillna(0, inplace=True)
            del combined_df['FG3_PCT']
            return combined_df.to_dict(orient='records')
        numeric_cols = df.select_dtypes(include='number').columns
        numeric_cols = [col for col in numeric_cols if 'rank' not in col.lower() and 'backcourt' not in col.lower() and 'team_id' not in col.lower()]
        league_avg = df[numeric_cols].mean()
        team_df = df[df['TEAM_NAME'] == team]
        
        team_stats = team_df.to_dict(orient='records')[0]
        for col in numeric_cols:
            if col in league_avg and league_avg[col] != 0:
                team_stats[f'{col}_vs_avg_pct'] = ((team_stats[col] - league_avg[col]) / league_avg[col]) * 100
        
        return team_stats

    def _fetch_opp_shooting_data(self, type, date_filter=None):
        return self.nba_stats.fetch_opponent_shot_chart(
            type,
            date_filter,
            per_mode_simple='PerGame',
            league_id='00',
        )
    

    def _fetch_data_from_table(self, table_name):
        from ..utils.tables import normalize_table_name
        table_name = normalize_table_name(table_name)
        query = f"SELECT * FROM {table_name}"
        with self.engine.connect() as conn:
            return pd.read_sql(query, conn)

    def _fetch_opponent_data(self, date_filter=None):
        return self.nba_stats.fetch_opponent_team_stats(
            date_filter,
            per_mode_detailed='Per48',
            league_id='00',
        )

    def _fetch_opp_shooting_zone_data(self, date_filter=None):
        opp_zone_df = self.nba_stats.fetch_opponent_shooting_zone(
            date_filter,
            per_mode_detailed='PerGame',
            league_id='00',
        )
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
