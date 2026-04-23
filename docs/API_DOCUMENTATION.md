# NBA Backend API Documentation

## Overview

This is a Flask-based REST API that provides NBA statistics, game logs, and advanced natural language query processing. The API features hybrid NLP/LLM integration using OpenAI's GPT-4o-mini for intelligent query parsing and supports complex filtering capabilities.

## Base URL

```
http://localhost:5000/api
```

## Authentication

Currently runs in development mode without authentication. CORS is enabled for cross-origin requests.

## Content Type

All POST/PUT requests should use `Content-Type: application/json`.

---

## Natural Language Query Endpoint

### Process Natural Language Query
```http
POST /api/nl-query
```

**Description**: Process natural language queries about NBA statistics using hybrid NLP/LLM routing. The system automatically routes complex queries to OpenAI GPT-4o-mini for better accuracy.

**Request Body:**
```json
{
  "query": "Show me LeBron James games with 30+ points against top 5 defenses"
}
```

**Response:**
```json
{
  "player_name": "LeBron James",
  "team_name": null,
  "game_count": null,
  "location": null,
  "players_on": [],
  "players_off": [],
  "teams_against": ["OPP_PTS"],
  "minutes_filter": null,
  "date_range": null,
  "self_filters": [
    {
      "stat_column": "PTS",
      "operator": "gte",
      "value": 30
    }
  ],
  "rank_filter": ["5"],
  "season": "2024-25",
  "confidence": 0.95,
  "intent": "game_logs",
  "time_period": null,
  "original_query": "Show me LeBron James games with 30+ points against top 5 defenses",
  "parsed_by": "llm"
}
```

**Supported Query Types:**
- Player performance queries ("LeBron last 10 games with 30+ points")
- Opponent filtering ("Curry vs top 5 defenses")
- Location filtering ("Dame home games")
- Teammate filtering ("LeBron with AD on court")
- Date range queries ("Curry games since January")
- Statistical thresholds ("Players with 25+ points and 10+ assists")

**Processing Methods:**
- `nlp`: Fast rule-based parsing for simple queries
- `llm`: OpenAI GPT-4o-mini for complex queries requiring better understanding

---

## Game Endpoints

### Get Game Logs (Blueprint Route)
```http
GET /api/games/game_logs
```

**Description**: Get filtered game logs with advanced filtering capabilities.

**Query Parameters:**
- `player_name` (required): Player name (fuzzy matching supported)
- `minutes_filter` (optional): Comma-separated min,max minutes (default: "0,48")
- `players_on[]` (optional): Array of teammate names required on court
- `players_off[]` (optional): Array of teammate names required off court
- `date_filter` (optional): Filter games from specific date (YYYY-MM-DD)
- `teams_against[]` (optional): Array of opponent filter criteria
- `rank_filter[]` (optional): Array of ranking numbers for opponent filters
- `location_filter` (optional): "Both", "Home", or "Away" (default: "Both")
- `game_filter` (optional): Additional game filtering criteria
- `season_filter` (optional): Season (default: "2024-25")
- `playstyle_RTG_min` (optional): Minimum playstyle rating (default: 75)
- `playstyle_RTG_max` (optional): Maximum playstyle rating (default: 125)
- `self_filters[STAT]` (optional): Statistical filters in format "min,max"

**Example:**
```bash
curl "http://localhost:5000/api/games/game_logs?player_name=LeBron James&minutes_filter=25,48&location_filter=Home&self_filters[PTS]=25,50"
```

**Response:**
```json
{
  "game_logs": "[{\"GAME_DATE\":\"2024-01-15\",\"MATCHUP\":\"LAL vs. GSW\",\"WL\":\"W\",\"MIN\":35,\"PTS\":28,\"REB\":8,\"AST\":11,\"FGM\":10,\"FGA\":18,\"FG_PCT\":0.556,\"FG3M\":3,\"FG3A\":7,\"FG3_PCT\":0.429,\"FTM\":5,\"FTA\":6,\"FT_PCT\":0.833,\"OREB\":2,\"DREB\":6,\"TOV\":4,\"STL\":2,\"BLK\":1,\"PF\":3,\"PRA\":47,\"PA\":39,\"PR\":36,\"RA\":19,\"STKS\":3,\"FD_PTS\":52.5}]",
  "averages": "[{\"MIN\":34.5,\"PTS\":25.8,\"REB\":7.2,\"AST\":6.5,\"PRA\":39.5,\"PA\":32.3,\"PR\":33.0,\"RA\":13.7,\"FD_PTS\":45.2}]",
  "season_averages": "[{\"MIN\":36.2,\"PTS\":27.1,\"REB\":8.1,\"AST\":6.8,\"PRA\":42.0,\"PA\":33.9,\"PR\":35.2,\"RA\":14.9,\"FD_PTS\":47.8}]",
  "next_game": "Houston Rockets"
}
```

### Get Game Logs (Legacy Route)
```http
GET /api/game_logs
```

**Description**: Legacy endpoint that calls the same underlying service. Maintained for backward compatibility.

---

## Player Endpoints

### Get All Players
```http
GET /api/players
```

**Description**: Retrieve all available players in the database.

**Response:**
```json
{
  "players": [
    {
      "id": 2544,
      "full_name": "LeBron James",
      "team": "Los Angeles Lakers"
    }
  ]
}
```

### Get Player Profile
```http
GET /api/players/profile
```

**Description**: Get detailed player profile including playstyle breakdowns and statistics.

**Query Parameters:**
- `player_name` (required): Full player name
- `category` (optional): Profile category (e.g., "Playtypes")
- `opp_team` (optional): Filter stats against specific opponent

**Example:**
```bash
curl "http://localhost:5000/api/players/profile?player_name=LeBron James&category=Playtypes"
```

**Response:**
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

### Fetch/Update Player Data
```http
PUT /api/players/fetch
GET /api/players/fetch
```

**Description**: Fetch and store updated player information from NBA API.

**Response:**
```json
{
  "message": "Player data processed and stored successfully"
}
```

### Legacy Player Routes
- `GET /api/players` - Same as blueprint route
- `GET /api/player_profile` - Same as profile endpoint
- `PUT /api/fetch_players` - Same as fetch endpoint

---

## Team Endpoints

### Get Team Statistics
```http
GET /api/teams/stats
```

**Description**: Get team statistics and performance data.

**Query Parameters:**
- `category` (optional): Statistics category
- `team` (optional): Team name or abbreviation
- `date` (optional): Date filter for stats

**Response:**
```json
{
  "team_stats": {
    "team_name": "Los Angeles Lakers",
    "ppg": 115.2,
    "rpg": 45.8,
    "apg": 26.4
  }
}
```

### Get All Teams
```http
GET /api/teams
```

**Description**: Get list of all NBA teams.

**Response:**
```json
{
  "teams": [
    {
      "id": 1,
      "name": "Los Angeles Lakers",
      "abbreviation": "LAL",
      "city": "Los Angeles"
    }
  ]
}
```

### Legacy Team Routes
- `GET /api/get_teams` - Same as teams endpoint
- `GET /api/team_stats` - Same as stats endpoint

---

## Data Management Endpoints

### Update Database
```http
GET /api/data/update_database
```

**Description**: Update all database tables with latest NBA data.

**Response:**
```json
{
  "message": "Database updated successfully"
}
```

### Store Player Play-by-Play Data
```http
PUT /api/data/player_PBP
```

**Description**: Fetch and store player play-by-play statistics.

**Response:**
```json
{
  "message": "Player PBP data processed and stored successfully"
}
```

### Store Opponent Play-by-Play Data
```http
PUT /api/data/opponent_PBP
```

**Description**: Fetch and store opponent play-by-play statistics.

**Response:**
```json
{
  "message": "Opponent PBP data processed and stored successfully"
}
```

### Get Players with Teams
```http
GET /api/data/fetch_players_with_teams
```

**Description**: Get all players mapped to their current teams.

**Response:**
```json
[
  {
    "player_id": 2544,
    "player_name": "LeBron James",
    "team": "Los Angeles Lakers"
  }
]
```

### Get Available Playtypes
```http
GET /api/data/fetch_playtypes
```

**Description**: Get list of available playtype categories for analysis.

**Response:**
```json
[
  "Transition",
  "Isolation",
  "PRBallHandler",
  "PRRollMan",
  "Spotup",
  "Cut",
  "Handoff",
  "OffScreen",
  "Postup",
  "Misc",
  "OffRebound"
]
```

### Legacy Data Routes
- `GET /api/update_database` - Same as data/update_database
- `PUT /api/player_PBP` - Same as data/player_PBP
- `PUT /api/opponent_PBP` - Same as data/opponent_PBP

---

## Advanced Filtering Capabilities

### Opponent Filters

The API supports filtering games against teams ranked by defensive performance in specific categories:

**Available Opponent Filters:**
- `OPP_PTS`: Overall points allowed defense
- `OPP_REB`: Rebounds allowed defense
- `OPP_AST`: Assists allowed defense
- `OPP_STOCKS`: Steals + blocks defense
- `OPP_FTA`: Foul rate (free throw attempts allowed)
- `OPP_TOV`: Turnovers forced
- `OPP_BLK`: Blocks generated
- `OPP_STL`: Steals generated
- `OPP_FG3M`: Three-pointers allowed
- `OPP_FG3A`: Three-point attempts allowed
- `C&S PTS`: Catch-and-shoot defense
- `C&S 3s`: Catch-and-shoot three-point defense
- `C&S 3A`: Catch-and-shoot three-point attempts
- `PU PTS`: Pull-up shot defense
- `PU 2s`: Pull-up two-point defense
- `PU 3s`: Pull-up three-point defense
- `PRBallHandler`: Pick-and-roll ball-handler defense
- `PRRollMan`: Pick-and-roll roll-man defense
- `Transition`: Fast-break defense
- `Isolation`: Isolation defense
- `Spotup`: Spot-up defense
- `Cut`: Cutting defense
- `Handoff`: Handoff defense
- `OffScreen`: Off-screen defense
- `Postup`: Post-up defense
- `OffRebound`: Defensive rebounding
- `Less Than 10 ft`: Paint protection
- `Misc`: Miscellaneous plays defense

**Ranking System:**
- Positive numbers (e.g., `5`) = Top X defenses (best defensive teams)
- Negative numbers (e.g., `-8`) = Worst X defenses (weakest defensive teams)

### Self Filters

Filter player performance using statistical thresholds:

**Format:** `self_filters[STAT]=min,max`

**Available Stats:**
- `PTS`: Points per game
- `REB`: Rebounds per game
- `AST`: Assists per game
- `MIN`: Minutes per game
- `FGM`, `FGA`, `FG_PCT`: Field goal statistics
- `FG3M`, `FG3A`, `FG3_PCT`: Three-point statistics
- `FTM`, `FTA`, `FT_PCT`: Free throw statistics
- `OREB`, `DREB`: Offensive/defensive rebounds
- `TOV`: Turnovers
- `STL`: Steals
- `BLK`: Blocks
- `PF`: Personal fouls
- `PLUS_MINUS`: Plus/minus
- `PRA`: Points + Rebounds + Assists
- `PA`: Points + Assists
- `PR`: Points + Rebounds
- `RA`: Rebounds + Assists
- `STKS`: Steals + Blocks (Stocks)
- `FD_PTS`: Fantasy draft points

### Teammate Filters

- `players_on[]`: Require specific players to be on court
- `players_off[]`: Require specific players to be off court

### Date and Location Filters

- `date_filter`: Filter games from specific date
- `location_filter`: "Home", "Away", or "Both"
- `season_filter`: Season year (e.g., "2024-25")

---

## LLM Integration Features

### OpenAI GPT-4o-mini Configuration

The API uses OpenAI's GPT-4o-mini model for complex natural language query processing:

**Model Settings:**
- **Model**: gpt-4o-mini (optimized for speed and cost)
- **Temperature**: 0 (deterministic responses)
- **Max Tokens**: 512 (concise responses)
- **Timeout**: 10 seconds
- **Max Retries**: 3 attempts with exponential backoff

### Hybrid Routing System

The API intelligently routes queries between traditional NLP and LLM processing:

1. **Traditional NLP**: Fast rule-based parsing for simple, unambiguous queries
2. **LLM Fallback**: Advanced AI processing for complex queries requiring contextual understanding
3. **Confidence Scoring**: Automatic routing based on parsing confidence levels

### Supported LLM Query Examples

```json
{
  "query": "Show me Curry's best shooting games against elite defenses this season"
}
```

```json
{
  "query": "Find games where LeBron and AD both scored 25+ with Russ off the court"
}
```

```json
{
  "query": "Dame's home games vs worst 10 three point defenses last 20 games"
}
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

### Natural Language Query Response
```json
{
  "player_name": "string or null",
  "team_name": "string or null",
  "game_count": "integer or null",
  "location": "home|away or null",
  "players_on": ["array of player names"],
  "players_off": ["array of player names"],
  "teams_against": ["array of opponent filter types"],
  "minutes_filter": "[min, max] or null",
  "date_range": "YYYY-MM-DD or null",
  "self_filters": [
    {
      "stat_column": "string",
      "operator": "gt|gte|lt|lte|eq|between",
      "value": "number",
      "value2": "number (for between operator)"
    }
  ],
  "rank_filter": ["array of ranking numbers"],
  "season": "string or null",
  "confidence": "float 0-1",
  "intent": "game_logs|player_profile|team_stats",
  "time_period": "string or null",
  "original_query": "string",
  "parsed_by": "nlp|llm"
}
```

---

## Database Schema

**Main Tables:**
- `Player_Information`: Player details and IDs
- `player_play_types`: Player playstyle statistics
- `team_play_types`: Team playstyle statistics
- `pbp_player_stats`: Play-by-play player statistics
- `pbp_opponent_stats`: Play-by-play opponent statistics
- `player_clusters`: Player similarity clustering
- `Player_Team_Table`: Player-team mappings
- `General Opponent Stats`: Team defensive rankings
- `Less Than 10 ft`: Paint protection statistics
- `Catch and Shoot`: Catch-and-shoot statistics
- `Pullups`: Pull-up shot statistics

---

## Error Handling

**Standard HTTP Status Codes:**
- `200`: Success
- `400`: Bad Request (invalid query parameters)
- `404`: Resource not found
- `500`: Internal server error

**Error Response Format:**
```json
{
  "error": "Detailed error message explaining what went wrong"
}
```

**Common Error Scenarios:**
- Invalid player name: Fuzzy matching attempts to find closest match
- LLM service unavailable: Automatic fallback to NLP processing
- Database connection issues: Graceful error handling with informative messages
- Invalid filter parameters: Clear validation error messages

---

## Performance Features

### Caching System
- **Daily NBA Data**: Game logs cached until 4 AM ET next day
- **Historical Data**: Seasonal data cached for 30 days
- **Redis Integration**: Optional Redis backend for enhanced performance
- **Intelligent Cache Keys**: Date-based keys for current season data

### Optimization
- **Fuzzy Matching**: Efficient player name matching with configurable thresholds
- **Connection Pooling**: Database connection management
- **Batch Processing**: Efficient data retrieval and processing
- **Smart Routing**: Automatic selection between NLP and LLM processing

---

## Usage Examples

### Basic Game Log Query
```bash
curl "http://localhost:5000/api/games/game_logs?player_name=LeBron James&location_filter=Home"
```

### Advanced Filtering
```bash
curl "http://localhost:5000/api/games/game_logs?player_name=Stephen Curry&self_filters[PTS]=25,50&self_filters[FG3M]=5,15&teams_against[]=OPP_FG3M&rank_filter[]=5"
```

### Natural Language Query
```bash
curl -X POST "http://localhost:5000/api/nl-query" \
  -H "Content-Type: application/json" \
  -d '{"query": "Show me Dame Lillard games with 30+ points against top 10 defenses at home"}'
```

### Teammate Filtering
```bash
curl "http://localhost:5000/api/games/game_logs?player_name=LeBron James&players_on[]=Anthony Davis&players_off[]=Russell Westbrook"
```

### Player Profile
```bash
curl "http://localhost:5000/api/players/profile?player_name=Kevin Durant&category=Playtypes"
```

---

## Development Notes

- **Environment Variables**: Configure OpenAI API key and LLM settings in `.env`
- **Database**: SQLite database (`nba_play_types.db`) with comprehensive NBA statistics
- **CORS**: Enabled for all origins in development mode
- **Logging**: Comprehensive logging for debugging and monitoring
- **Legacy Support**: Maintains backward compatibility with original API routes
