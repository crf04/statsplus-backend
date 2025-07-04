# NBA Backend API Documentation

## Overview

This is a Flask-based REST API that provides NBA statistics, player data, team information, and game logs with advanced filtering capabilities. The API integrates with NBA API and PBP Stats to deliver comprehensive basketball analytics.

## Base URL

```
http://localhost:5000
```

## API Endpoints

### Game Logs

#### Get Player Game Logs
- **URL**: `/api/game_logs` or `/api/games/game_logs`
- **Method**: `GET`
- **Description**: Retrieves filtered game logs for a specific player with various filtering options

**Parameters:**
- `player_name` (required): Name of the player
- `minutes_filter` (optional): Comma-separated min,max minutes (default: "0,48")
- `players_on[]` (optional): Array of player names that must be on court
- `players_off[]` (optional): Array of player names that must be off court
- `date_filter` (optional): Filter games from specific date (YYYY-MM-DD)
- `teams_against[]` (optional): Array of team filter criteria
- `filter_numbers[]` (optional): Array of rank filter numbers
- `location_filter` (optional): "Both", "Home", or "Away" (default: "Both")
- `game_filter` (optional): Specific game filter criteria
- `playstyle_RTG_min` (optional): Minimum playstyle rating (default: 75)
- `playstyle_RTG_max` (optional): Maximum playstyle rating (default: 125)
- `self_filters[STAT]` (optional): Statistical filters in format "min,max"

**Response:**
```json
{
  "game_logs": "[{\"GAME_DATE\":\"2024-01-15\",\"MIN\":35,\"PTS\":28,...}]",
  "averages": "[{\"MIN\":34.5,\"PTS\":25.8,...}]",
  "season_averages": "[{\"MIN\":36.2,\"PTS\":27.1,...}]",
  "next_game": "Houston Rockets"
}
```

**Example:**
```bash
GET /api/game_logs?player_name=LeBron James&minutes_filter=20,48&location_filter=Home
```

---

### Players

#### Get All Players
- **URL**: `/api/players` or `/api/players`
- **Method**: `GET`
- **Description**: Returns list of all available players

**Response:**
```json
[
  "LeBron James",
  "Stephen Curry",
  "Kevin Durant",
  ...
]
```

#### Get Player Profile
- **URL**: `/api/player_profile` or `/api/players/profile`
- **Method**: `GET`
- **Description**: Get detailed player profile data

**Parameters:**
- `player_name` (required): Name of the player
- `category` (required): "Playtypes", "assists", or "Archetype"
- `opp_team` (optional): Opposing team name for archetype analysis

**Response:**
```json
{
  "Cut%": 15.2,
  "Isolation%": 12.8,
  "PRRollMan%": 8.5,
  ...
}
```

#### Fetch/Update Players
- **URL**: `/api/fetch_players` or `/api/players/fetch`
- **Method**: `PUT`
- **Description**: Updates the player database with latest NBA API data

**Response:**
```json
{
  "message": "Player data processed and stored successfully"
}
```

---

### Teams

#### Get All Teams
- **URL**: `/api/get_teams` or `/api/teams`
- **Method**: `GET`
- **Description**: Returns list of all NBA teams

**Response:**
```json
[
  "Los Angeles Lakers",
  "Golden State Warriors",
  "Boston Celtics",
  ...
]
```

#### Get Team Stats
- **URL**: `/api/team_stats` or `/api/teams/stats`
- **Method**: `GET`
- **Description**: Get specific team statistics

**Parameters:**
- `category` (required): Statistical category
- `team` (required): Team name
- `date` (optional): Specific date filter

**Response:**
```json
{
  "TEAM_NAME": "Los Angeles Lakers",
  "GP": 82,
  "W": 45,
  "L": 37,
  "WIN_PCT": 0.549,
  "PTS": 115.2,
  ...
}
```

---

### Data Management

#### Update Database
- **URL**: `/api/update_database` or `/api/data/update_database`
- **Method**: `GET`
- **Description**: Updates all database tables with latest NBA data

**Response:**
```json
{
  "message": "Database updated successfully"
}
```

#### Store Player PBP Data
- **URL**: `/api/player_PBP` or `/api/data/player_PBP`
- **Method**: `PUT`
- **Description**: Fetches and stores player play-by-play data

**Response:**
```json
{
  "message": "Player PBP data processed and stored successfully"
}
```

#### Store Opponent PBP Data
- **URL**: `/api/opponent_PBP` or `/api/data/opponent_PBP`
- **Method**: `PUT`
- **Description**: Fetches and stores opponent play-by-play data

**Response:**
```json
{
  "message": "Opponent PBP data processed and stored successfully"
}
```

#### Fetch Players with Teams
- **URL**: `/api/data/fetch_players_with_teams`
- **Method**: `GET`
- **Description**: Gets players mapped to their teams

**Response:**
```json
[
  {
    "player_id": 2544,
    "player_name": "LeBron James",
    "team": "Los Angeles Lakers"
  },
  ...
]
```

#### Get Playtypes
- **URL**: `/api/data/fetch_playtypes`
- **Method**: `GET`
- **Description**: Returns available playtype categories

**Response:**
```json
[
  "Transition",
  "Isolation", 
  "PRBallHandler",
  "PRRollMan",
  ...
]
```

---

## Data Models

### Game Log Entry
```json
{
  "GAME_DATE": "2024-01-15",
  "MATCHUP": "LAL vs. GSW",
  "WL": "W",
  "MIN": 35,
  "PTS": 28,
  "REB": 8,
  "AST": 11,
  "FGM": 10,
  "FGA": 18,
  "FG_PCT": 0.556,
  "FG3M": 3,
  "FG3A": 7,
  "FG3_PCT": 0.429,
  "FTM": 5,
  "FTA": 6,
  "FT_PCT": 0.833,
  "OREB": 2,
  "DREB": 6,
  "TOV": 4,
  "STL": 2,
  "BLK": 1,
  "PF": 3,
  "PLUS_MINUS": 12,
  "PRA": 47,
  "PA": 39,
  "PR": 36,
  "RA": 19,
  "STKS": 3,
  "FD_PTS": 52.5
}
```

### Player Profile (Playtypes)
```json
{
  "PLAYER_NAME": "LeBron James",
  "Cut": 2.1,
  "Cut%": 8.5,
  "Isolation": 3.2,
  "Isolation%": 12.8,
  "PRRollMan": 1.8,
  "PRRollMan%": 7.2,
  "PRBallHandler": 4.5,
  "PRBallHandler%": 18.0,
  "OffRebound": 0.8,
  "OffRebound%": 3.2,
  "Spotup": 5.2,
  "Spotup%": 20.8,
  "Handoff": 1.1,
  "Handoff%": 4.4,
  "OffScreen": 2.3,
  "OffScreen%": 9.2,
  "Misc": 2.8,
  "Misc%": 11.2,
  "Postup": 1.2,
  "Postup%": 4.8,
  "Transition": 6.1,
  "Transition%": 24.4
}
```

## Error Handling

All endpoints return standard HTTP status codes:

- `200`: Success
- `404`: Resource not found
- `500`: Internal server error

Error responses follow this format:
```json
{
  "error": "Error description message"
}
```

## Usage Examples

### Get LeBron James' home games with 25+ minutes
```bash
curl "http://localhost:5000/api/game_logs?player_name=LeBron James&minutes_filter=25,48&location_filter=Home"
```

### Get player's playstyle data
```bash
curl "http://localhost:5000/api/player_profile?player_name=Stephen Curry&category=Playtypes"
```

### Update database with latest data
```bash
curl -X GET "http://localhost:5000/api/update_database"
```

### Filter games where specific players were on court together
```bash
curl "http://localhost:5000/api/game_logs?player_name=LeBron James&players_on[]=Anthony Davis&players_on[]=Russell Westbrook"
```

## Advanced Filtering

The API supports sophisticated filtering capabilities:

1. **Statistical Filters**: Use `self_filters[STAT]=min,max` format
2. **Teammate Filters**: `players_on[]` for required teammates
3. **Opponent Filters**: Filter against teams with specific rankings
4. **Date Ranges**: Filter games from specific dates
5. **Location**: Home/Away game filtering
6. **Minutes**: Filter by playing time ranges

## Authentication

Currently, the API does not require authentication. CORS is enabled for cross-origin requests.

## Rate Limiting

No rate limiting is currently implemented, but consider implementing it for production use.

## Database

The API uses SQLite database (`nba_play_types.db`) with the following main tables:
- `player_play_types`
- `team_play_types` 
- `Player_Information`
- `pbp_player_stats`
- `pbp_opponent_stats`
- `player_clusters`
- Various processed statistics tables

## Notes

- Some endpoints have both legacy routes (in `app.py`) and modern blueprint routes (in `app/routes/`)
- The API integrates with NBA API and PBP Stats for real-time data
- Statistical calculations include advanced metrics like PRA (Points + Rebounds + Assists), matchup ratings, etc.
- Player clustering/archetype analysis is available for similar player comparisons 