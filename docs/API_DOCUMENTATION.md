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
- Admin-only: `GET /api/user/admin/stats`, every `/api/data/*` endpoint (including `GET /api/data/jobs/<job_id>`), and `PUT /api/players/fetch`.
- Optional: player and team read routes, plus `POST /api/user/activity/ping`.
- Admin claims: an authenticated token must contain `admin=true`, `role=admin`,
  or `roles` containing `admin` for admin-only routes. Missing claims return
  `403 Forbidden`.

## Correlation headers

Every response carries an `X-Request-ID`. If the request sends a safe inbound
`X-Request-ID` (letters, digits, `_`, `.`, `:`, `-`, up to 128 characters) it is
echoed back and used as the correlation ID; otherwise the server generates one.
Provider-telemetry events raised during the request carry the same ID, so a
response header, a duration, and a provider event share one key.
`X-Request-ID` is always set, including on error responses.

## Error responses

Expected application failures use one stable JSON shape:

```json
{
  "error": {
    "code": "invalid_input",
    "message": "The request contains invalid input."
  }
}
```

The public error categories and HTTP statuses are:

| Category | Code | Status | Use |
| --- | --- | ---: | --- |
| Invalid input | `invalid_input` | 400 | Query parameters or request bodies cannot be parsed or validated. |
| Missing resource | `resource_not_found` | 404 | The requested player, profile, or other resource does not exist. |
| Provider unavailable | `provider_unavailable` | 503 | A required NBA or other upstream provider cannot be reached. |
| Invalid configuration | `invalid_configuration` | 500 | Server configuration cannot safely support the request. |
| Authentication required | `authentication_required` | 401 | Credentials are missing or malformed. |
| Invalid token | `invalid_token` | 401 | The supplied Firebase token cannot be verified. |
| Forbidden | `forbidden` | 403 | The authenticated user lacks the required permission. |
| Operation failed | `operation_failed` | 500 | A requested application operation could not be completed. |
| Duplicate active operation | `duplicate_active_operation` | 409 | A data refresh for the same operation is already queued or running. |

Unexpected failures return `internal_error` with status `500`. Internal
exception details are logged for operators and are never included in the
response. Game-log records and averages are ordinary JSON arrays, not nested
pandas JSON strings.

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

Checks connectivity to `stats.nba.com` through `NBAStatsAdapter`. This endpoint
depends on external network access.

### PBP Stats Health

```http
GET /api/health/pbp-api
```

Checks connectivity to `api.pbpstats.com` through its own adapter and
connect/read timeout settings. `/api/health/pbp-stats` is an equivalent
compatibility path.

### Detailed Health

```http
GET /api/health/detailed
```

Combines database, `stats.nba.com`, and `api.pbpstats.com` checks. The response
distinguishes them as `checks.nba_api` and `checks.pbp_stats` and returns `503`
when a dependency is degraded.

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

Returns filtered game logs, filtered averages, and season averages. The
`next_game` field remains `null` under the existing contract. The `game_logs`
array contains one object per game, and `averages` / `season_averages` are
arrays holding a single averages object; all three fields are ordinary JSON
arrays, never JSON strings.

### Contract and migration note (#9)

- Filters are validated into one typed `GameLogQuery` before the service runs.
  Malformed values (non-numeric `minutes_filter`, an unparsable `date_filter`,
  `game_filter` below 1, or `rank_filter[]` not matching `teams_against[]` one
  per one) return a `400` error with code `invalid_input` and message:
  `One or more game log filters are invalid.`
- `game_logs`, `averages`, and `season_averages` are ordinary JSON arrays.
  Earlier versions nested pandas JSON strings in these fields; callers that
  parsed those strings must instead read the arrays directly. `next_game`
  remains `null` under the existing contract.
- Empty result sets return empty arrays (`[]`) for `game_logs` and `averages`;
  `season_averages` still carries the season aggregate when the full season has
  games. `next_game` may be `null`.

Query parameters:

| Parameter | Required | Notes |
| --- | --- | --- |
| `player_name` | Yes | Player name; service uses fuzzy matching where available |
| `minutes_filter` | No | Comma-separated min,max minutes. Default `0,48` |
| `players_on[]` | No | Teammates required on court |
| `players_off[]` | No | Teammates required off court |
| `date_filter` | No | `YYYY-MM-DD` start date passed to NBA/team filter logic |
| `teams_against[]` | No | Opponent filter names such as `OPP_PTS` |
| `rank_filter[]` | No | Rank for each opponent filter; positive means top defenses, negative means weakest |
| `location_filter` | No | `Home`, `Away`, or `Both`. Default `Both` |
| `game_filter` | No | Last N games |
| `season_filter` | No | Canonical NBA season in `YYYY-YY` form, with `YY` equal to the following calendar year's final two digits (for example, `2024-25`). Whitespace is trimmed. Default is the current season |
| `playstyle_RTG_min` | No | Finite numeric lower bound. Default `0` |
| `playstyle_RTG_max` | No | Finite numeric upper bound. Default `200` |
| `self_filters[STAT]` | No | Ordered inclusive stat range as `min,max` (normalized to a typed `between` filter); repeat the parameter to combine multiple constraints for one stat. Supported stats include `MIN`, `PTS`, `REB`, `AST`, `FGM`, `FGA`, `FG_PCT`, `FG3M`, `FG3A`, `FTM`, `FTA`, `OREB`, `DREB`, `TOV`, `STL`, `BLK`, `PF`, `PLUS_MINUS`, `PRA`, `PA`, `PR`, `RA`, `STKS`, and `FD_PTS` |

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
`nba_api` and replaces the local `player_information` table.  Returns
`202 Accepted` with a durable job (`operation: "fetch_players"`); the refresh
runs after the response is sent.  Its completion can be observed through
`GET /api/data/jobs/<job_id>`.

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
They can call external NBA/PBP APIs or replace local tables.  All mutating
refreshes are durable jobs: the route returns `202 Accepted` immediately with
`job_id` and the queued job state, and the refresh runs in the background.  A
second request for an already active operation (queued or running) returns
`409 Conflict` with code `duplicate_active_operation`.

Queue execution is at-least-once.  A crashed worker or expired lease can
cause the registered handler to run again; `attempt_count` identifies each
claim.  Before replacing any live table, the handler renews that claim as a
fencing check inside the same database transaction as the table swap.  A
stale attempt is rejected and cannot overwrite the newer attempt, although
provider calls already in flight when a lease expires are not cancellable by
this mechanism.

The `../api/data/jobs/<job_id>` endpoint returns the current durable state of
one job, including `status` (`queued`, `running`, `succeeded`, `failed`),
`progress`, timestamps, and a sanitized `failure_summary` (provider or
exception text is never written there). It also includes the captured
`request_id`, `attempt_count`, and latest `heartbeat_at`; lease owner and
expiry remain internal queue metadata.

```http
POST /api/data/update_database
PUT /api/data/player_PBP
PUT /api/data/opponent_PBP
POST /api/data/fetch_players_with_teams
GET /api/data/jobs/<job_id>
GET /api/data/fetch_playtypes
GET /api/data/telemetry
```

`GET /api/data/telemetry` returns bounded, sanitized provider and application
telemetry counters on the documented provider seams, with the most recent 50
provider events. Provider failures are counted at the provider seams and
application failures by the central error handler; neither list ever carries
credentials, URLs, bodies, or exception text. Example shape:

```json
{
  "provider_events_total": 1,
  "provider_failures": { "nba_stats": { "timeout": 1 } },
  "application_failures": { "internal_error": 2 },
  "cache": { "nba_stats": { "hit": 3, "miss": 1 } },
  "buffered_events": 5,
  "buffered_capacity": 5000,
  "recent_provider_events": [
    {
      "provider": "nba_stats",
      "operation": "player_game_logs",
      "outcome": "malformed",
      "started_at": "2025-03-04T05:00:00+00:00",
      "duration_ms": 4.2,
      "retry_count": 2,
      "cache_status": "miss",
      "request_id": "a1b2…",
      "status_code": 200
    }
  ]
}
```

Example start response (`202 Accepted`):

```json
{
  "job_id": "9f8c…",
  "operation": "update_database",
  "status": "queued",
  "progress": 0.0,
  "progress_note": null,
  "created_at": "2025-01-15T12:00:00+00:00",
  "started_at": null,
  "finished_at": null,
  "failure_summary": null,
  "request_id": "a1b2…",
  "attempt_count": 0,
  "heartbeat_at": null
}
```

`GET /api/data/fetch_playtypes` is unchanged: it still returns the defensively
shaped play-type table directly with `200 OK`.

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
- Shooting: `C&S PTS`, `C&S 3s`, `C&S 3A`, `PU PTS`, `PU 2s`, `PU 3s`, `Less Than 10 ft` (legacy `<10 Ft` is accepted and normalized)
- Play types: `PRBallHandler`, `PRRollMan`, `Transition`, `Isolation`, `Spotup`, `Cut`, `Handoff`, `OffScreen`, `Postup`, `OffRebound`, `Misc`

Ranking convention:

- `rank_filter[]=5` means top 5 defenses for the selected filter.
- `rank_filter[]=-8` means bottom 8 defenses for the selected filter.

### Self Filters

Format (repeat the parameter when combining constraints for the same stat):

```text
self_filters[STAT]=min,max
self_filters[STAT]=min,max
```

Supported stats include `MIN`, `PTS`, `REB`, `AST`, `FGM`, `FGA`, `FG_PCT`, `FG3M`, `FG3A`, `FTM`, `FTA`, `OREB`, `DREB`, `TOV`, `STL`, `BLK`, `PF`, `PLUS_MINUS`, `PRA`, `PA`, `PR`, `RA`, `STKS`, and `FD_PTS`.

The query-string form is retained for compatibility and means an inclusive
`between` comparison. Natural-language and typed executor inputs use an
ordered list of canonical comparison models with `operator` set to one of
`gte`, `gt`, `lt`, `lte`, `eq`, or `between`; `value2` is required only for
`between`. For example, `PTS >= 20` and `PTS < 30` are represented as two
list entries and are applied sequentially.

## Development Notes

- The app uses `DATABASE_URL` and defaults to `sqlite:///nba_play_types.db`.
- CORS is enabled globally.
- `OPENAI_API_KEY` is optional; without it the NL endpoint uses deterministic parsing only.
- Redis is optional; cache initialization should not be required for local development.
- Legacy routes from earlier versions are not currently registered in `app/__init__.py`; use the blueprint paths documented here.
- The recorded provider fixtures under `tests/fixtures/` are parsed through the
  production seams with no network. Live provider-contract tests in
  `tests/live/` are marked `live` and excluded from the default gate; opt in
  with `LIVE_CONTRACT_TESTS=true` plus `-m live`.
