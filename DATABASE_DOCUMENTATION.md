# NBA Backend Database Documentation

## Database Overview
- **Database Type**: SQLite
- **Database File**: `nba_play_types.db`
- **Total Tables**: 18

## Table Descriptions

### 1. **pbp_player_stats**
**Purpose**: Detailed play-by-play statistics for individual players
**Key Columns**:
- `EntityId`, `TeamId`, `Name`, `ShortName` - Player identification
- `SecondsPlayed`, `GamesPlayed`, `Minutes` - Playing time metrics
- `AtRimFGM/FGA`, `ShortMidRangeFGM/FGA`, `LongMidRangeFGM/FGA` - Shot zone statistics
- `Corner3FGM/FGA`, `Arc3FGM/FGA` - 3-point shooting by location
- `Assists`, `Rebounds`, `Steals`, `Blocks`, `Turnovers` - Traditional stats
- `PlusMinus`, `Usage`, `TsPct`, `EfgPct` - Advanced metrics
- Multiple percentage and frequency metrics for detailed analysis

### 2. **pbp_opponent_stats**
**Purpose**: Play-by-play statistics for opponent performance (defensive metrics)
**Key Columns**: 
- Similar structure to `pbp_player_stats` but from defensive perspective
- Opponent shooting percentages and frequencies by zone
- Defensive rebounding and steal metrics
- Plus/minus and possession-based statistics

### 3. **Player_Information**
**Purpose**: Basic player profile information
**Columns**:
- `id` (BIGINT) - Unique player identifier
- `full_name` (TEXT) - Complete player name
- `first_name` (TEXT) - Player's first name
- `last_name` (TEXT) - Player's last name
- `is_active` (BOOLEAN) - Current active status

### 4. **Player_Team_Table**
**Purpose**: Links players to their current teams
**Columns**:
- `Player` (TEXT) - Player name
- `Current Team` (TEXT) - Team name
- `Team_ID` (FLOAT) - Numeric team identifier

### 5. **Team_Info**
**Purpose**: NBA team information and details
**Columns**:
- `id` (BIGINT) - Unique team identifier
- `full_name` (TEXT) - Complete team name
- `abbreviation` (TEXT) - Team abbreviation (e.g., "LAL", "GSW")
- `nickname` (TEXT) - Team nickname
- `city` (TEXT) - Team city
- `state` (TEXT) - Team state
- `year_founded` (BIGINT) - Year team was established

### 6. **Player_Per36_Stats**
**Purpose**: Player statistics normalized per 36 minutes
**Key Columns**:
- `PLAYER_ID`, `PLAYER_NAME`, `TEAM_ID`, `TEAM_ABBREVIATION` - Identification
- `GP`, `W`, `L`, `W_PCT` - Games and win statistics
- `FGM`, `FGA`, `FG_PCT` - Field goal statistics
- `FG3M`, `FG3A`, `FG3_PCT` - Three-point statistics
- `FTM`, `FTA`, `FT_PCT` - Free throw statistics
- `OREB`, `DREB`, `REB` - Rebounding statistics
- `AST`, `TOV`, `STL`, `BLK` - Other traditional stats
- `PTS`, `PLUS_MINUS` - Scoring and impact metrics
- Rankings for all major statistical categories

### 7. **General Opponent Stats**
**Purpose**: Team defensive statistics against opponents
**Key Columns**:
- `TEAM_ID`, `TEAM_NAME` - Team identification
- `OPP_FGM`, `OPP_FGA`, `OPP_FG_PCT` - Opponent field goal stats
- `OPP_FG3M`, `OPP_FG3A`, `OPP_FG3_PCT` - Opponent 3-point stats
- `OPP_REB`, `OPP_AST`, `OPP_TOV` - Opponent other stats
- `OPP_PTS` - Opponent points allowed
- Rankings for all defensive categories

### 8. **player_play_types**
**Purpose**: Breakdown of player offensive play types by percentage
**Columns**:
- `PLAYER_NAME`, `TEAM_ABBREVIATION` - Player identification
- `Transition%` - Percentage of possessions in transition
- `Isolation%` - Percentage of isolation plays
- `PRBallHandler%` - Pick and roll ball handler percentage
- `PRRollMan%` - Pick and roll roll man percentage
- `OffRebound%` - Offensive rebound putback percentage
- `Spotup%` - Spot-up shooting percentage
- `Cut%` - Cutting to basket percentage
- `Handoff%` - Handoff play percentage
- `OffScreen%` - Off-screen play percentage
- `Misc%` - Miscellaneous play percentage
- `Postup%` - Post-up play percentage

### 9. **team_play_types**
**Purpose**: Team offensive play type distributions
**Columns**:
- `TEAM_NAME`, `Team_ID`, `team` - Team identification
- Same play type percentages as player table but aggregated by team

### 10. **player_shooting_zones**
**Purpose**: Player shooting statistics by court zones
**Key Columns**:
- `PLAYER_NAME` - Player identification
- Zone-specific FGM/FGA/FG_PCT for:
  - `Restricted Area` - Close to basket shots
  - `In The Paint (Non-RA)` - Paint shots outside restricted area
  - `Mid-Range` - Mid-range jump shots
  - `Left Corner 3`, `Right Corner 3` - Corner three-pointers
  - `Above the Break 3` - Above-the-break three-pointers
- Points and percentage breakdowns by zone
- Comparative metrics (PTS%+) showing above/below league average

### 11. **opp_shooting_zone**
**Purpose**: Team defensive statistics by opponent shooting zones
**Columns**:
- `TEAM_ID`, `TEAM_NAME` - Team identification
- Opponent shooting percentages and attempts by zone
- Rankings for defensive performance in each zone

### 12. **Catch and Shoot**
**Purpose**: Team catch-and-shoot statistics
**Columns**:
- `TEAM_ID`, `TEAM_NAME`, `TEAM_ABBREVIATION` - Team identification
- `FGA_FREQUENCY` - Frequency of catch-and-shoot attempts
- Separate 2-point and 3-point catch-and-shoot statistics
- Rankings for catch-and-shoot performance

### 13. **Pullups**
**Purpose**: Team pull-up shooting statistics
**Columns**:
- Similar structure to catch-and-shoot table
- Focus on pull-up jump shot attempts and makes
- 2-point and 3-point breakdowns with rankings

### 14. **Less Than 10 ft**
**Purpose**: Team shooting statistics for shots within 10 feet
**Columns**:
- Close-range shooting statistics
- Frequency and accuracy metrics
- Rankings for close-range shooting performance

### 15. **player_clusters**
**Purpose**: Player clustering/grouping analysis
**Columns**:
- `PlayerName` (TEXT) - Player name
- `ClusterID` (BIGINT) - Cluster group identifier
- `PlayerID` (BIGINT) - Unique player ID

### 16. **processed_player_assists**
**Purpose**: Processed assist statistics for players by shot type
**Columns**:
- `Name` (TEXT) - Player name
- Assist breakdowns by shot location:
  - `TwoPtAssists`, `ThreePtAssists`
  - `Arc3Assists`, `Corner3Assists`
  - `AtRimAssists`, `ShortMidRangeAssists`, `LongMidRangeAssists`
- Plus metrics showing above/below average performance

### 17. **processed_team_assists**
**Purpose**: Team-level assist statistics with rankings
**Columns**:
- `Name` (TEXT) - Team name
- Total assists and assist points
- Breakdown by shot type and location
- Rankings for all assist categories

## Key Relationships

### Player-Team Relationships
- `Player_Information.id` ↔ `Player_Team_Table.Player`
- `Team_Info.id` ↔ `Player_Team_Table.Team_ID`
- Player names link across multiple statistical tables

### Statistical Relationships
- `pbp_player_stats` and `pbp_opponent_stats` are complementary (offensive vs defensive)
- `player_play_types` and `team_play_types` show individual vs team play style
- `player_shooting_zones` and `opp_shooting_zone` show offensive vs defensive zone performance

### Processed Data Relationships
- `processed_player_assists` and `processed_team_assists` are aggregated from raw play-by-play data
- `player_clusters` groups players based on statistical similarity

## Data Sources
- **Primary Source**: NBA official statistics API
- **Play-by-Play Data**: Detailed possession-level tracking
- **Traditional Stats**: Standard NBA statistical categories
- **Advanced Metrics**: Calculated efficiency and impact statistics

## Usage Notes
- All shooting statistics include both raw numbers and percentages
- Advanced metrics include league-relative measures (+ statistics)
- Rankings are provided for comparative analysis
- Play-by-play tables contain the most granular statistical breakdowns
- Team tables aggregate individual player performance