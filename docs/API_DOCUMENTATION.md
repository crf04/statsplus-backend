# NBA Backend API Documentation

## Overview

This Flask API exposes NBA player, team, game-log, data-refresh, health, user, and natural-language query endpoints. The natural-language endpoint uses deterministic NLP first and can fall back to OpenAI when configured.

Base URL for local development:

```text
http://localhost:5000/api
```

All POST and PUT examples use `Content-Type: application/json`.

## Authentication

Firebase Admin verifies `Authorization: Bearer <firebase-id-token>` on protected
routes. Protected routes fail closed when Firebase Admin cannot initialize:
the service returns `503 Service Unavailable`. Missing or invalid tokens return
`401 Unauthorized`.

Authentication levels:

- Required: `GET /api/games/game_logs`, `POST /api/nl-query`, and most `/api/user/*` routes.
- Admin-only: `GET /api/user/admin/stats`, every `/api/data/*` endpoint, and `PUT /api/players/fetch`.
- Optional: player and team read routes, plus `POST /api/user/activity/ping`.
- Admin claims: an authenticated token must contain `admin=true`, `role=admin`,
  or `roles` containing `admin` for admin-only routes. Missing claims return
  `403 Forbidden`.

For local, credential-free development only, set
`FIREBASE_ADMIN_DISABLED=true`. This explicitly enables a synthetic `dev-user`
for local requests. The bypass is rejected when `FLASK_ENV=production`; never
enable it in a deployed environment. It is accepted only in an explicit
development or test environment.

## Health Endpoints

### Database Health

```http
GET /api/health/db
```

Runs `SELECT 1` against the configured SQLAlchemy engine and returns the dialect, driver, timestamp, and status.

### NBA API Health

```http
GET /api/health/nba-api
```

Checks connectivity to the pbpstats NBA totals endpoint. This endpoint depends on external network access.

### Detailed Health

```http
GET /api/health/detailed
```

Combines database and NBA API checks. Returns `503` when a dependency is degraded.

## Natural Language Query

### Parse Natural Language Query

```http
POST /api/nl-query
```

Request:

```json
{
  "query": "Show me LeBron James games with 30+ points against top 5 defenses"
}
```

Response shape:

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
  "date_filter": null,
  "self_filters": [
    {
      "stat_column": "PTS",
      "operator": "gte",
      "value": 30
    }
  ],
  "rank_filter": ["5"],
  "season": "2025-26",
  "confidence": 0.95,
  "intent": "game_logs",
  "time_period": null,
  "original_query": "Show me LeBron James games with 30+ points against top 5 defenses",
  "parsed_by": "nlp"
}
```

`parsed_by` can be `nlp`, `llm`, or `hybrid`.

Common query types:

- Player performance: "LeBron last 10 games with 30+ points"
- Opponent rankings: "Curry vs top 5 defenses"
- Location: "Dame home games"
- Teammates: "LeBron with Anthony Davis on court"
- Dates: "Curry games since January 1"
- Stat thresholds: "25+ points and 10+ assists"

## Game Endpoints

### Get Game Logs

```http
GET /api/games/game_logs
```

Returns filtered game logs, filtered averages, season averages, and the next opponent. The `game_logs`, `averages`, and `season_averages` fields are JSON strings produced by pandas.

Query parameters:

| Parameter | Required | Notes |
| --- | --- | --- |
| `player_name` | Yes | Player name; service uses fuzzy matching where available |
| `minutes_filter` | No | Comma-separated min,max minutes. Default `0,48` |
| `players_on[]` | No | Teammates required on court |
| `players_off[]` | No | Teammates required off court |
| `date_filter` | No | Date string passed to NBA/team filter logic |
| `teams_against[]` | No | Opponent filter names such as `OPP_PTS` |
| `rank_filter[]` | No | Rank for each opponent filter; positive means top defenses, negative means weakest |
| `location_filter` | No | `Home`, `Away`, or `Both`. Default `Both` |
| `game_filter` | No | Last N games |
| `season_filter` | No | Season. Default `2025-26` |
| `playstyle_RTG_min` | No | Default `0` |
| `playstyle_RTG_max` | No | Default `200` |
| `self_filters[STAT]` | No | Stat range as `min,max`, for example `self_filters[PTS]=25,60` |

Example:

```bash
curl "http://localhost:5000/api/games/game_logs?player_name=LeBron%20James&minutes_filter=25,48&location_filter=Home&self_filters[PTS]=25,60" \
  -H "Authorization: Bearer <firebase-id-token>"
```

## Player Endpoints

### Get Players

```http
GET /api/players
```

Returns a JSON array of player names from the local player play-type table.

### Get Player Profile

```http
GET /api/players/profile
```

Query parameters:

- `player_name` is required.
- `category` is required by the service. Supported values include `Playtypes`, `assists`, `Archetype`, `Shooting Type`, and `Zone Shooting`.
- `opp_team` is used by `Archetype`.

Example:

```bash
curl "http://localhost:5000/api/players/profile?player_name=LeBron%20James&category=Playtypes"
```

### Fetch or Update Player Data

```http
PUT /api/players/fetch
```

Requires an admin claim. Fetches current NBA player metadata through
`nba_api` and replaces the local `player_information` table.

## Team Endpoints

### Get Teams

```http
GET /api/teams
```

Returns a JSON array of NBA team full names from `nba_api`.

### Get Team Stats

```http
GET /api/teams/stats
```

Query parameters:

- `team`: full team name, such as `Los Angeles Lakers`.
- `category`: `Traditional`, `Playtypes`, `Assists`, `Zone Shooting`, or `Shooting Type`.
- `date`: optional date filter for categories that can query live NBA data.

Example:

```bash
curl "http://localhost:5000/api/teams/stats?team=Los%20Angeles%20Lakers&category=Traditional"
```

## Data Management Endpoints

These endpoints require an authenticated Firebase token with an admin claim.
They can call external NBA/PBP APIs or replace local tables.

```http
POST /api/data/update_database
PUT /api/data/player_PBP
PUT /api/data/opponent_PBP
POST /api/data/fetch_players_with_teams
GET /api/data/fetch_playtypes
```

Use the bundled database for read-only demo exploration before running refresh endpoints.

## User Endpoints

Most user endpoints require Firebase auth:

```http
GET /api/user/profile
PUT /api/user/profile
GET /api/user/stats
POST /api/user/deactivate
GET /api/user/admin/stats
POST /api/user/sync
```

`GET /api/user/admin/stats` additionally requires an admin claim (`admin=true`,
`role=admin`, or `roles` containing `admin`).

Optional-auth endpoint:

```http
POST /api/user/activity/ping
```


## Filtering Reference

### Opponent Filters

Common opponent filters include:

- Traditional: `OPP_PTS`, `OPP_REB`, `OPP_AST`, `OPP_STOCKS`, `OPP_FTA`, `OPP_TOV`, `OPP_BLK`, `OPP_STL`, `OPP_FG3M`, `OPP_FG3A`
- Shooting: `C&S PTS`, `C&S 3s`, `C&S 3A`, `PU PTS`, `PU 2s`, `PU 3s`, `Less Than 10 ft`
- Play types: `PRBallHandler`, `PRRollMan`, `Transition`, `Isolation`, `Spotup`, `Cut`, `Handoff`, `OffScreen`, `Postup`, `OffRebound`, `Misc`

Ranking convention:

- `rank_filter[]=5` means top 5 defenses for the selected filter.
- `rank_filter[]=-8` means bottom 8 defenses for the selected filter.

### Self Filters

Format:

```text
self_filters[STAT]=min,max
```

Common stats include `MIN`, `PTS`, `REB`, `AST`, `FGM`, `FGA`, `FG_PCT`, `FG3M`, `FG3A`, `FTM`, `FTA`, `OREB`, `DREB`, `TOV`, `STL`, `BLK`, `PF`, `PLUS_MINUS`, `PRA`, `PA`, `PR`, `RA`, `STKS`, and `FD_PTS`.

## Error Format

Most route errors use:

```json
{
  "error": "Detailed error message"
}
```

Authentication errors include an `error` and `message` field.

## Development Notes

- The app uses `DATABASE_URL` and defaults to `sqlite:///nba_play_types.db`.
- CORS is enabled globally.
- `OPENAI_API_KEY` is optional; without it the NL endpoint uses deterministic parsing only.
- Redis is optional; cache initialization should not be required for local development.
- Legacy routes from earlier versions are not currently registered in `app/__init__.py`; use the blueprint paths documented here.
