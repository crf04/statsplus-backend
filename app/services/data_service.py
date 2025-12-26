import pandas as pd
import requests
from nba_api.stats.endpoints import (
    LeagueDashTeamStats,
    LeagueDashOppPtShot,
    LeagueDashTeamShotLocations,
    SynergyPlayTypes,
    LeagueDashPlayerShotLocations,
    commonplayerinfo,
    LeagueDashPlayerStats
)
from nba_api.stats.static import teams, players
from app.utils.nba_api_config import get_shared_nba_session
from app.utils.performance_monitor import monitor_nba_api_calls, PerformanceTimer

class DataService:
    def __init__(self, db_engine):
        self.engine = db_engine

    def update_all_data(self):
        """Update all database tables with fresh data"""
        try:
            self.store_player_information()
            self.process_opponent_scoring()
            self.store_player_per36_stats()
            self.process_and_store_team_data()
            self.process_opp_shooting()
            self.process_opp_shooting_zone()
            self.process_playstyles()
            self.process_player_zone()
            #self.process_clusters()
            self.fetch_PBP_data()
            self.fetch_PBP_data(data_type = 'Opponent')
            self.process_assist_data()
            return True
        except Exception as e:
            print(f"Error updating database: {e}")
            return False

    def process_opponent_scoring(self):
        """Process and store opponent scoring data"""
        df = self._fetch_opponent_data()
        df.to_sql('general_opponent_stats', self.engine, if_exists='replace', index=False)

    def process_opp_shooting(self):
        """Process and store opponent shooting data"""
        types = ['Catch and Shoot', 'Pullups', 'Less Than 10 ft']
        for type in types:
            try:
                df = self._fetch_opp_shooting_data(type)
                df['FG3M_RANK'] = df['FG3M'].rank(method='min', ascending=True)
                df['FG2M_RANK'] = df['FG2M'].rank(method='min', ascending=True)
                df['FG2A_RANK'] = df['FG2A'].rank(method='min', ascending=True)
                df['FG3A_RANK'] = df['FG3A'].rank(method='min', ascending=True)
                # Normalize table names to snake_case for Postgres
                table_map = {
                    'Catch and Shoot': 'catch_and_shoot',
                    'Pullups': 'pullups',
                    'Less Than 10 ft': 'less_than_10_ft',
                }
                df.to_sql(table_map[type], self.engine, if_exists='replace', index=False)
            except Exception as e:
                print(f"Error processing {type}: {e}")
                continue

    def process_opp_shooting_zone(self):
        """Process and store opponent shooting zone data"""
        opp_zone_df = self._fetch_opp_shooting_zone_data()
        opp_zone_df.columns = ['_'.join(filter(None, col)).strip() 
                              for col in opp_zone_df.columns]
        
        # Add rankings for relevant columns
        columns = [a for a in opp_zone_df.columns 
                  if 'OPP' in a and 'PCT' not in a and 'Backcourt' not in a]
        for c in columns:
            col_name = f'{c}_RANK'
            opp_zone_df[col_name] = opp_zone_df[c].rank(method='min', ascending=True)
            
        opp_zone_df.to_sql('opp_shooting_zone', self.engine, 
                          if_exists='replace', index=False)

    def process_and_store_team_data(self):
        """Process and store team play type data"""
        playtypes = [
            'Transition', 'Isolation', 'PRBallHandler', 'PRRollMan', 'OffRebound',
            'Spotup', 'Cut', 'Handoff', 'OffScreen', 'Misc', 'Postup'
        ]
        team_dfs = []

        # Fetch data for each play type
        for play_type in playtypes:
            try:
                df = self._fetch_team_play_type_data(play_type)
                df['PTS/G'] = df['PTS'] / df['GP']
                team_dfs.append(df)
            except Exception as e:
                print(f"Error fetching data for play type {play_type}: {e}")
                continue

        combined_team_df = pd.concat(team_dfs, ignore_index=True)
        
        # Calculate PTS/G+ for each play type
        mean_pts_per_game = {
            play_type: combined_team_df[
                combined_team_df['PLAY_TYPE'] == play_type
            ]['PTS/G'].mean() 
            for play_type in playtypes
        }
        for play_type in playtypes:
            combined_team_df.loc[
                combined_team_df['PLAY_TYPE'] == play_type, 'PTS/G+'
            ] = combined_team_df['PTS/G'] / mean_pts_per_game[play_type]

        # Pivot and clean up the dataframe
        teams_df = combined_team_df.pivot_table(
            index='TEAM_NAME',
            columns='PLAY_TYPE',
            values='PTS/G+',
            aggfunc='first'
        ).reset_index()

        # Add team IDs and abbreviations
        nba_teams = teams.get_teams()
        teams_df.loc[teams_df['TEAM_NAME'] == 'LA Clippers', 'TEAM_NAME'] = 'Los Angeles Clippers'
        team_ids = {team['full_name']: team['id'] for team in nba_teams}
        teams_df['Team_ID'] = teams_df['TEAM_NAME'].map(team_ids)
        teams_df['team'] = teams_df['TEAM_NAME'].apply(self._nba_team_to_abbreviation)

        # Reorder columns
        new_order = ['TEAM_NAME', 'Cut', 'Isolation', 'PRRollMan', 'PRBallHandler',
                    'OffRebound', 'Spotup', 'Handoff', 'OffScreen', 'Misc', 
                    'Postup', 'Transition', 'Team_ID', 'team']
        teams_df = teams_df[new_order]

        teams_df.to_sql('team_play_types', self.engine, if_exists='replace', index=False)

    def process_playstyles(self):
        """Process and store player play type data"""
        playtypes = [
            'Transition', 'Isolation', 'PRBallHandler', 'PRRollMan', 'OffRebound',
            'Spotup', 'Cut', 'Handoff', 'OffScreen', 'Misc', 'Postup'
        ]
        dfs = []

        for play_type in playtypes:
            try:
                df = self._fetch_play_type_data(play_type)
                dfs.append(df)
            except Exception as e:
                print(f"Error fetching data for play type {play_type}: {e}")
                continue

        combined_df = pd.concat(dfs, ignore_index=True)
        
        # Create pivot table
        pivot_df = combined_df.pivot_table(
            index='PLAYER_NAME',
            columns='PLAY_TYPE',
            values='PTS',
            aggfunc='first'
        ).reset_index()

        # Add team information
        player_team_df = combined_df.groupby('PLAYER_NAME')['TEAM_ABBREVIATION'].agg('first').reset_index()
        pivot_df = pd.merge(pivot_df, player_team_df, on='PLAYER_NAME', how='left')
        
        # Calculate percentages
        sums = pivot_df.drop(['PLAYER_NAME', 'TEAM_ABBREVIATION'], axis=1).sum(axis=1)
        pivot_df['Sum'] = sums
        
        for play_type in playtypes:
            percentage_column_name = play_type + '%'
            pivot_df[percentage_column_name] = pivot_df[play_type] / pivot_df['Sum'] * 100

        # Clean up
        pivot_df.drop(playtypes + ['Sum'], axis=1, inplace=True)
        pivot_df.fillna(0, inplace=True)

        pivot_df.to_sql('player_play_types', self.engine, if_exists='replace', index=False)

    def process_player_zone(self):
        """Process and store player shooting zone data"""
        player_zones = self._fetch_player_zone_data()
        player_zones.columns = ['_'.join(filter(None, col)).strip() 
                    for col in player_zones.columns]

        # Filter and calculate points
        player_zones_columns = [c for c in player_zones.columns 
                                if ('FGM' in c or '_NAME' in c) and 'Back' not in c]
        

        for col in player_zones_columns:
            if 'NAME' not in col:
                player_zones[col.split('_')[0]+ "_PTS"] = (player_zones[col] * 2 
                                if '3' not in col else player_zones[col] * 3)

        
        # Calculate percentages
        sums = player_zones.drop(['PLAYER_NAME','PLAYER_ID','TEAM_ID','TEAM_ABBREVIATION','AGE','NICKNAME'], axis=1).sum(axis=1)
        player_zones['Sum'] = sums

        for col in player_zones_columns:
            if 'NAME' not in col:
                percentage_column_name = col.split('_')[0] + "_PTS%"
                player_zones[percentage_column_name] = (player_zones[col.split('_')[0] + "_PTS"] / 
                                                    player_zones['Sum'] * 100)
        
        means = player_zones.drop(['PLAYER_NAME','PLAYER_ID','TEAM_ID','TEAM_ABBREVIATION','AGE','NICKNAME'], axis=1).mean()
        # Clean up
        cols = [c for c in player_zones.columns if 'PTS%' in c]
        

        for c in cols:
            if means[c] != 0:
                player_zones[f"{c}+"] = player_zones[c] / means[c]
            else:
                player_zones[f"{c}+"] = 0
        
        player_zones.drop([c for c in player_zones.columns 
                        if 'Backcourt' in c], 
                        axis=1, inplace=True)
        player_zones.drop(['PLAYER_ID','TEAM_ID','TEAM_ABBREVIATION','AGE','NICKNAME','Sum'], 
                        axis=1, inplace=True)
        player_zones.fillna(0, inplace=True)
        

        player_zones.to_sql('player_shooting_zones', self.engine, 
                           if_exists='replace', index=False)

    def process_clusters(self):
        """Process and store player clusters"""
        clusters = {
        0: ['Alperen Sengun', 'Anthony Davis', 'Bam Adebayo', 'Bobby Portis', 'Deandre Ayton', 'Joel Embiid', 'Jonas Valanciunas', 'Jusuf Nurkic', 'Kyle Anderson', 'Nikola Jokic', 'Nikola Vucevic', 'Zach Collins'],
        1: ['Alec Burks', 'Alex Caruso', 'Ayo Dosunmu', 'Bojan Bogdanovic', 'Brandin Podziemski', 'Brandon Miller', 'Brice Sensabaugh', 'Caleb Martin', 'Cam Whitmore', 'Cameron Johnson', 'Cameron Payne', 'Cody Martin', 'Davion Mitchell', "De'Andre Hunter", "De'Anthony Melton", 'Derrick White', 'Devin Vassell', 'Dillon Brooks', 'Evan Fournier', 'GG Jackson', 'Gary Trent Jr.', 'Harrison Barnes', 'Jake LaRavia', 'Jalen Suggs', 'Josh Hart', 'Josh Richardson', 'Jrue Holiday', 'Keldon Johnson', 'Kyle Lowry', 'Lonnie Walker IV', 'Malaki Branham', 'Mikal Bridges', 'Mike Conley', 'Miles McBride', 'Moses Moody', 'Naji Marshall', 'Norman Powell', 'Patrick Williams', 'Payton Pritchard', 'Rayan Rupert', 'Vince Williams Jr.', 'Ziaire Williams'],
        2: ['Aaron Holiday', 'Anfernee Simons', 'Anthony Edwards', 'Austin Reaves', 'CJ McCollum', 'Caris LeVert', 'Coby White', 'Cole Anthony', "D'Angelo Russell", 'Damian Lillard', 'Darius Garland', "De'Aaron Fox", 'Dejounte Murray', 'Dennis Schroder', 'Desmond Bane', 'Donovan Mitchell', 'Fred VanVleet', 'Immanuel Quickley', 'Jalen Brunson', 'Jalen Green', 'Jalen Williams', 'James Harden', 'Jayson Tatum', 'Jordan Clarkson', 'Jordan Goodwin', 'Jordan Poole', 'Keyonte George', 'LaMelo Ball', 'Luka Doncic', 'Malcolm Brogdon', 'Malik Monk', 'Marcus Sasser', 'Patrick Beverley', 'Reggie Jackson', 'Scoot Henderson', 'Scotty Pippen Jr.', 'Shaedon Sharpe', 'Spencer Dinwiddie', 'Stephen Curry', 'Talen Horton-Tucker', 'Terry Rozier', 'Trae Young', 'Tre Mann', 'Tyler Herro', 'Tyrese Haliburton', 'Tyrese Maxey', 'Tyus Jones', 'Vasilije Micic', 'Zach LaVine'],
        3: ['Al Horford', 'Dean Wade', 'Dorian Finney-Smith', 'Eric Gordon', 'Gary Harris', 'Georges Niang', 'Grayson Allen', 'Haywood Highsmith', 'Isaiah Livers', 'Jacob Gilyard', 'Jae Crowder', 'Joe Ingles', 'Jose Alvarado', 'Julian Champagnie', 'Luguentz Dort', 'Malik Beasley', 'Matisse Thybulle', 'Maxi Kleber', 'Nickeil Alexander-Walker', 'Nicolas Batum', 'Nikola Jovic', 'P.J. Tucker', 'Pat Connaughton', 'Quentin Grimes', "Royce O'Neale", 'Sam Hauser', 'Simone Fontecchio', 'Taurean Prince', 'Taylor Hendricks', 'Torrey Craig', 'Trey Lyles', 'Trey Murphy III', 'Vit Krejci'],
        4: ['Aaron Nesmith', 'Aaron Wiggins', 'Amir Coffey', 'Anthony Black', 'Ausar Thompson', 'Bilal Coulibaly', 'Cam Reddish', 'Cason Wallace', 'Cedi Osman', 'Chimezie Metu', 'Christian Braun', 'David Roddy', 'Derrick Jones Jr.', 'Dyson Daniels', 'Gary Payton II', 'Herbert Jones', 'Isaac Okoro', 'Jaden McDaniels', 'Jalen Johnson', 'Jalen Wilson', 'Jarred Vanderbilt', 'John Konchar', 'Josh Green', 'Josh Okogie', 'Kevin Knox II', 'Kris Murray', 'OG Anunoby', 'Obi Toppin', 'Ochai Agbaji', 'Peyton Watson', 'Robert Covington', 'Saddiq Bey', 'Tari Eason', 'Terance Mann', 'Toumani Camara'],
        5: ['Andrew Wiggins', 'Bennedict Mathurin', 'Bruce Brown', 'Collin Sexton', 'Dalano Banton', 'Dante Exum', 'Delon Wright', 'Deni Avdija', 'Dennis Smith Jr.', 'Derrick Rose', 'Franz Wagner', 'Giannis Antetokounmpo', 'Gordon Hayward', 'Jaden Ivey', "Jae'Sean Tate", 'Jaime Jaquez Jr.', 'Jaren Jackson Jr.', 'Javon Freeman-Liberty', 'Jaylen Brown', 'Jerami Grant', 'Jeremy Sochan', 'Jimmy Butler', 'Jonathan Kuminga', 'Josh Giddey', 'Julius Randle', 'Kelly Oubre Jr.', 'Kris Dunn', 'Kyle Kuzma', 'LeBron James', 'Markelle Fultz', 'Miles Bridges', 'Pascal Siakam', 'RJ Barrett', 'Russell Westbrook', 'Scottie Barnes', 'Tobias Harris', 'Tre Jones', 'Zion Williamson'],
        6: ['Aaron Gordon', 'Amen Thompson', 'Andre Drummond', 'Bismack Biyombo', 'Bruno Fernando', 'Clint Capela', 'Daniel Gafford', 'Daniel Theis', "Day'Ron Sharpe", 'Dereck Lively II', 'Domantas Sabonis', 'Drew Eubanks', 'Evan Mobley', 'Goga Bitadze', 'Isaiah Hartenstein', 'Ivica Zubac', 'Jakob Poeltl', 'Jalen Duren', 'James Wiseman', 'Jarrett Allen', 'Kevon Looney', 'Luke Kornet', 'Marvin Bagley III', 'Mitchell Robinson', 'Moritz Wagner', 'Nic Claxton', 'Nick Richards', 'Onyeka Okongwu', 'Paul Reed', 'Precious Achiuwa', 'Rudy Gobert', 'Trayce Jackson-Davis', 'Trey Jemison', 'Walker Kessler', 'Xavier Tillman'],
        7: ['Bogdan Bogdanovic', 'Buddy Hield', 'Corey Kispert', 'Donte DiVincenzo', 'Duncan Robinson', 'Davis Bertans', 'Gradey Dick', 'Isaiah Joe', 'Jordan Hawkins', 'Keegan Murray', 'Kentavious Caldwell-Pope', 'Keon Ellis', 'Kevin Huerter', 'Klay Thompson', 'Landry Shamet', 'Luke Kennard', 'Max Strus', 'Michael Porter Jr.', 'Sam Merrill', 'Tim Hardaway Jr.'],
        8: ['Andrew Nembhard', 'Bradley Beal', 'Brandon Ingram', 'Cade Cunningham', 'Cam Thomas', 'Chris Paul', 'DeMar DeRozan', 'Devin Booker', 'Ish Smith', 'Jamal Murray', 'Kawhi Leonard', 'Kevin Durant', 'Khris Middleton', 'Killian Hayes', 'Kyrie Irving', 'Marcus Morris Sr.', 'Paolo Banchero', 'Paul George', 'Shai Gilgeous-Alexander', 'T.J. McConnell'],
        9: ['Brook Lopez', 'Chet Holmgren', 'Christian Wood', 'Dario Saric', 'Draymond Green', 'Duop Reath', 'Grant Williams', 'Isaiah Stewart', 'Jabari Smith Jr.', 'Jabari Walker', 'Jalen Smith', 'Jeff Green', 'John Collins', 'Jonathan Isaac', 'Karl-Anthony Towns', 'Kelly Olynyk', 'Kevin Love', 'Kristaps Porzingis', 'Larry Nance Jr.', 'Lauri Markkanen', 'Myles Turner', 'Naz Reid', 'Noah Clowney', 'P.J. Washington', 'Rui Hachimura', 'Santi Aldama', 'Victor Wembanyama', 'Wendell Carter Jr.']
    }

        player_data = []
        for cluster_id, player_names in clusters.items():
            for player_name in player_names:
                try:
                    player_id = self._get_player_id(player_name)
                    player_data.append({
                        'PlayerName': player_name,
                        'ClusterID': cluster_id,
                        'PlayerID': player_id
                    })
                except Exception as e:
                    print(f"Error processing player {player_name}: {e}")

        players_df = pd.DataFrame(player_data)
        players_df.to_sql('player_clusters', self.engine, 
                         if_exists='replace', index=False)

    @monitor_nba_api_calls
    def fetch_PBP_data(self, data_type='player'):
        """Fetch play-by-play data from external API using optimized session"""
        base_url = 'https://api.pbpstats.com/get-totals/nba'
        params = {
            'Season': '2025-26',
            'SeasonType': 'Regular+Season',
            'Type': 'Player' if data_type == 'player' else 'Opponent'
        }
        
        try:
            # Use optimized session with connection pooling
            session = get_shared_nba_session()
            response = session.get(base_url, params=params, timeout=(10, 30))
            response.raise_for_status()
            
            data = response.json()
            df = pd.DataFrame(data['multi_row_table_data'])
            table_name = f'pbp_{data_type}_stats'
            
            with PerformanceTimer(f"store_{data_type}_pbp_data"):
                df.to_sql(table_name, self.engine, if_exists='replace', index=False)
                
            return True
        except requests.exceptions.Timeout as e:
            print(f"Timeout fetching {data_type} PBP data: {e}")
            return False
        except requests.exceptions.RequestException as e:
            print(f"Request error fetching {data_type} PBP data: {e}")
            return False
        except Exception as e:
            print(f"Error fetching {data_type} PBP data: {e}")
            return False

    def store_player_information(self):
        """Store basic player information"""
        try:
            player_dict = players.get_active_players()
            player_df = pd.DataFrame.from_dict(player_dict)
            player_df.to_sql('player_information', self.engine, 
                           if_exists='replace', index=False)
            return player_df.to_dict(orient='records')
        except Exception as e:
            print(f"Error storing player information: {e}")
            return False
    
    def store_player_per36_stats(self):
        """Store player per36 stats"""
        try:
            df = self._fetch_player_per36_stats()
            df.to_sql('player_per36_stats', self.engine, if_exists='replace', index=False)
            return True
        except Exception as e:
            print(f"Error storing player per36 stats: {e}")
            return False

    # Helper methods
    def _fetch_opponent_data(self, date_filter=None):
        return LeagueDashTeamStats(
            measure_type_detailed_defense='Opponent',
            per_mode_detailed='Per48',
            date_from_nullable=date_filter,
            league_id_nullable='00'  # Filter for NBA teams only (excludes WNBA/G-League)
        ).get_data_frames()[0]

    def _fetch_opp_shooting_data(self, type, date_filter=None):
        return LeagueDashOppPtShot(
            general_range_nullable=type,
            date_from_nullable=date_filter,
            per_mode_simple='PerGame',
            league_id_nullable='00'  # Filter for NBA teams only (excludes WNBA/G-League)
        ).get_data_frames()[0]

    def _fetch_opp_shooting_zone_data(self, date_filter=None):
        return LeagueDashTeamShotLocations(
            distance_range='By Zone',
            measure_type_simple='Opponent',
            per_mode_detailed='PerGame',
            date_from_nullable=date_filter,
            league_id_nullable='00'  # Filter for NBA teams only (excludes WNBA/G-League)
        ).get_data_frames()[0]

    def _fetch_player_zone_data(self, date_filter=None):
        return LeagueDashPlayerShotLocations(
            distance_range='By Zone',
            per_mode_detailed='PerGame',
            date_from_nullable=date_filter,
            league_id_nullable='00'  # Filter for NBA players only (excludes WNBA/G-League)
        ).get_data_frames()[0]

    def _fetch_team_play_type_data(self, play_type):
        return SynergyPlayTypes(
            play_type_nullable=play_type,
            player_or_team_abbreviation='T',
            type_grouping_nullable='Defensive',
            league_id_nullable='00'  # Filter for NBA teams only (excludes WNBA/G-League)
        ).get_data_frames()[0]

    def _fetch_play_type_data(self, play_type):
        return SynergyPlayTypes(
            play_type_nullable=play_type,
            player_or_team_abbreviation='P',
            type_grouping_nullable='Offensive',
            league_id_nullable='00'  # Filter for NBA players only (excludes WNBA/G-League)
        ).get_data_frames()[0]
    
    def _fetch_player_per36_stats(self):
        return LeagueDashPlayerStats(
            measure_type_detailed_defense='Base',
            per_mode_detailed='Per36',
            league_id_nullable='00'  # Filter for NBA players only (excludes WNBA/G-League)
        ).get_data_frames()[0]

    def _fetch_data_from_table(self, table_name):
        from ..utils.tables import normalize_table_name
        normalized = normalize_table_name(table_name)
        query = f"SELECT * FROM {normalized}"
        with self.engine.connect() as conn:
            return pd.read_sql(query, conn)

    @staticmethod
    def _nba_team_to_abbreviation(team_name):
        nba_teams = teams.get_teams()
        team_abbr_map = {team['full_name']: team['abbreviation'] 
                        for team in nba_teams}
        return team_abbr_map.get(team_name, "Unknown")

    @staticmethod
    def _get_player_id(player_name):
        player_dict = players.find_players_by_full_name(player_name)
        if player_dict:
            return player_dict[0]['id']
        raise ValueError(f"Player not found: {player_name}")
    
    def process_assist_data(self):
        """
        Process and store assist data for teams and players.
        Calculates normalized stats and rankings for teams,
        and percentage distributions and plus metrics for players.
        """
        try:
            # Fetch data
            teams_df = self._fetch_data_from_table('pbp_Opponent_stats')
            players_df = self._fetch_data_from_table('pbp_Player_stats')
            
            # Process team assist data
            team_columns = [
                "Name", "Assists", "AssistPoints", "TwoPtAssists", "ThreePtAssists",
                "Arc3Assists", "Corner3Assists", "AtRimAssists", "ShortMidRangeAssists",
                "LongMidRangeAssists"
            ]
            teams_df = teams_df[team_columns]
            
            # Calculate normalized stats
            stat_columns = team_columns[1:]  # All columns except 'Name'
            means = teams_df[stat_columns].mean()
            for col in stat_columns:
                teams_df[col] = teams_df[col] / means[col]
                teams_df[f'{col}_RANK'] = teams_df[col].rank(method='min', ascending=True)
            
            # Process player assist data
            player_columns = [
                "Name", "TwoPtAssists", "ThreePtAssists", "Arc3Assists", "Corner3Assists",
                "AtRimAssists", "ShortMidRangeAssists", "LongMidRangeAssists"
            ]
            players_df = players_df[player_columns]
            
            # Calculate location-based assist percentages
            location_cats = [
                "Arc3Assists", "Corner3Assists", "AtRimAssists",
                "ShortMidRangeAssists", "LongMidRangeAssists"
            ]
            location_sum = players_df.drop(["Name", "TwoPtAssists", "ThreePtAssists"], axis=1).sum(axis=1)
            for cat in location_cats:
                players_df[cat] = players_df[cat] / location_sum * 100
            
            # Calculate type-based assist percentages
            type_sum = players_df.drop(
                ["Name"] + location_cats,
                axis=1
            ).sum(axis=1)
            players_df["TwoPtAssists"] = players_df["TwoPtAssists"] / type_sum * 100
            players_df["ThreePtAssists"] = players_df["ThreePtAssists"] / type_sum * 100
            
            # Calculate plus metrics
            averages = players_df.drop('Name', axis=1).mean()
            for col in players_df.columns:
                if col != 'Name':
                    players_df[f'{col}+'] = players_df[col] / averages[col]
            
            # Store processed data
            teams_df.to_sql('processed_team_assists', self.engine, if_exists='replace', index=False)
            players_df.to_sql('processed_player_assists', self.engine, if_exists='replace', index=False)
            
            return True
                    
        except Exception as e:
            print(f"Error processing assist data: {e}")
            return False
        
    import openpyxl
    def read_excel_and_save_to_db(self):
        # Read the Excel file into a DataFrame
        df = pd.read_excel(r'C:\Users\chris\OneDrive\Documents\NBA_PLAYERS.xlsx')
        df = df[['Player','Current Team']]
        df.to_sql('player_team_table', self.engine, if_exists='replace', index=False)
        return df.to_dict(orient='records')
    
    def save_team(self):
        teams_df = pd.DataFrame(teams.get_teams())
        teams_df.to_sql('team_info', self.engine, if_exists='replace', index=False)
    
    def map_id_to_team(self):
        teams_df = self._fetch_data_from_table('team_info')
        teams_df['full_name'] = teams_df['full_name'].str.replace('76ers', 'Sixers')
        df = self._fetch_data_from_table('player_team_table')
        df = df.iloc[:-1]
        df['Team_ID'] = df['Current Team'].map(teams_df.set_index('full_name')['id'])
        df.to_sql('player_team_table', self.engine, if_exists='replace', index=False)
        return df.to_dict(orient='records')
    
    def get_playtypes(self):
        
        play_types = [
            'Transition', 'Isolation', 'PRBallHandler', 'PRRollMan', 'OffRebound',
            'Spotup', 'Cut', 'Handoff', 'OffScreen', 'Misc', 'Postup'
        ]
        
        combined_df = pd.DataFrame()
        
        for play_type in play_types:
            df = SynergyPlayTypes(
                play_type_nullable=play_type,
                player_or_team_abbreviation='T',
                type_grouping_nullable='Defensive',
                league_id_nullable='00'  # Filter for NBA teams only (excludes WNBA/G-League)
            ).get_data_frames()[0]
            df['PLAY_TYPE'] = play_type
            df['PTS/G'] = df['PTS'] / df['GP']
            combined_df = pd.concat([combined_df, df], ignore_index=True)
            
        # First, pivot just the PTS/G values
        pts_pivot = combined_df.pivot_table(
            index='TEAM_NAME',
            columns='PLAY_TYPE',
            values='PTS/G',
            aggfunc='first'
        ).reset_index()

        # Get one GP value per team (grabbing any play type since they're all the same)
        gp_per_team = combined_df.groupby('TEAM_NAME')['GP'].first().reset_index()

        # Merge the two DataFrames
        teams_df = pts_pivot.merge(gp_per_team, on='TEAM_NAME')
        return teams_df.to_dict(orient='records')
        
        
        
        