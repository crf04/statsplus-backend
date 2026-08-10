# NBA Backend API Documentation

## Overview

This Flask API exposes NBA player, team, game-log, data-refresh, health, user, DFS Board, and natural-language query endpoints. The natural-language endpoint uses deterministic NLP first and can fall back to OpenAI when configured.

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

- Required: `GET /api/games/slate`, `GET /api/games/game_logs`, `GET /api/games/matchup/selection`, `GET /api/dfs/board`, `POST /api/nl-query`, and most `/api/user/*` routes.
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
| Board too large | `board_too_large` | 400 | The post-filter DFS Board exceeds the configured market ceiling. |
| DFS Board disabled | `dfs_board_disabled` | 404 | The deployment does not publish the DFS Board. |

An error may carry an optional `details` object when a caller cannot act on the
failure without structured facts. It is present only where this document says
so, and contains only bounded, closed-vocabulary values.

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

### Get Slate

```http
GET /api/games/slate?date=YYYY-MM-DD
```

Requires Firebase bearer authentication. `date` is optional only by omission
and defaults to today's Slate Date in US Eastern time; an explicitly empty
`?date=` is malformed input. The route reads the configured current season's
persisted Event Catalog. It converts the requested day's two US Eastern
midnights to a half-open UTC query window, including across DST transitions,
so it does not read the whole season per request. Games are ordered by tip
time, then `game_id`.

```json
{
  "slate_date": "2026-01-02",
  "freshness": {
    "schedule": {
      "status": "fresh",
      "retrieved_at": "2026-01-02T10:00:00+00:00"
    },
    "pool": {
      "retrieved_at": "2026-01-02T10:04:00+00:00",
      "providers": {
        "prizepicks": {
          "status": "fresh",
          "retrieved_at": "2026-01-02T10:04:00+00:00"
        },
        "underdog": {
          "status": "missing",
          "retrieved_at": null
        }
      }
    }
  },
  "games": [
    {
      "game_id": "0022500001",
      "away_team": {
        "team_id": 1610612747,
        "tricode": "LAL",
        "name": "Los Angeles Lakers",
        "targetable_player_count": 4
      },
      "home_team": {
        "team_id": 1610612759,
        "tricode": "SAS",
        "name": "San Antonio Spurs",
        "targetable_player_count": 3
      },
      "scheduled_at": "2026-01-03T00:00:00+00:00",
      "status": {
        "state": "scheduled",
        "label": "7:00 pm ET"
      },
      "classification": null,
      "preseason": false
    }
  ]
}
```

`status.state` is `scheduled`, `postponed`, or `final`; `status.label` retains
the Event Catalog label. `classification` is `null` for an ordinary Regular
Season game and contains meaningful provider display classification for
unusual games, including reviewed event sublabels such as `Emirates NBA Cup`
and `NBA Mexico City Series`. Generic series-state/record text such as
`LAL leads 2-1` or `LAL wins series 4-2`, game-number text, and postponement
sublabels are not badges. Recognized `001` through `004` game-ID prefixes
determine canonical kind before the stored display classification is
considered, so an arbitrary badge cannot turn a known regular-season or
playoff game into an All-Star exclusion. Stored display classification is the
kind fallback only for an unknown prefix. Thus `001` games are included with
`preseason: true`, and `003` All-Star exhibitions are excluded even when the
display classification is branded differently. Postponed games remain on
their ET slate. Output is always ordered by UTC tip and then game ID,
independently of repository row order.

Each team count is the number of canonical players in the live Player Pool for
that slate game. The pool is the union of usable PrizePicks, Underdog, and
Dabble observations. A market qualifies only when it is available, standard,
full-game, and explicitly mapped to PTS, REB, AST, 3PM, TOV, STL, BLK, PRA, PA,
PR, RA, STKS, FGA, FG3A, or FG2A. Suspended, alternate, promotional,
period-specific, fantasy-points, DD2/TD3, unknown-stat, unjoined-player, and
other-slate markets do not affect counts.

`freshness.pool.providers` uses the closed vocabulary `fresh | stale-served |
missing` and retains each usable provider snapshot's actual retrieval time. A
successful empty board is fresh information and produces zero counts. The
aggregate is `fresh` only when every provider is fresh, `stale-served` when
every usable provider is stale-served, and `unavailable` when none is usable.
For any mixed state—including fresh plus stale-served, or usable plus
missing—aggregate `status` is omitted so the frontend derives its documented
partial/degraded presentation from the provider entries. The union uses every
usable observation.
Player Pool snapshots are persisted by season and exact Slate game set. A
snapshot no more than 15 minutes old is reused without another board fetch and
retains each provider's actual `retrieved_at`. This is an inclusive reuse
maximum age, not the provider cache's exclusive fresh window. The first later
request refreshes the pool lazily. A partial refresh replaces the prior union with only usable
providers and marks failures `missing`. On total board failure, the last pool
is served through six hours with aggregate and contributing-provider status
`stale-served`; beyond six hours the pool is empty and `unavailable`. No
synthetic pool is produced.
Aggregate `retrieved_at` is the oldest usable contributor snapshot, so its age
never understates any provider observation included in the union.

A stale but populated schedule remains a `200` with
`freshness.schedule.status: "stale"`. Stored
catalog rows without successful-refresh metadata remain servable and report
`freshness.schedule` as `{ "status": "missing", "retrieved_at": null }`.
Availability is determined from actual stored Event Catalog rows, not the
refresh record's informational `event_count`.
Schedule freshness uses the nightly schedule surface's independent
`SLATE_SCHEDULE_MAX_AGE_HOURS` window (30 hours by default), not the broader
Event Catalog eligibility TTL. The exact boundary is fresh; an older retrieval
is stale.

Empty and error behavior:

| Case | Response |
| --- | --- |
| Populated catalog has no games on the requested date | `200` with `games: []` and freshness blocks |
| `date` is empty or not exactly `YYYY-MM-DD` | `400 invalid_input` |
| Event Catalog dependency is unavailable at runtime | `503 provider_unavailable` |
| Catalog events are stored but successful-refresh metadata is missing | `200` with schedule freshness `missing` |
| No catalog events are stored | `503 provider_unavailable` |
| Authentication is missing or invalid | existing `401` authentication error contract |

### Get Matchup Selection

```http
GET /api/games/matchup/selection?game_id=<nba_game_id>&player_id=<canonical_player_id>
Authorization: Bearer <firebase-id-token>
```

Returns the selected Player Pool player's stored H2H games against the game's
opponent and the stored games for the player's archetype peers against that
opponent. `game_id` is one nonempty string and `player_id` is one positive
canonical integer. Unknown, repeated, empty, noncanonical, or extra parameters
are refused rather than ignored.

```json
{
  "player_id": 2544,
  "freshness": {
    "player_pool": {
      "status": "fresh",
      "retrieved_at": "2026-01-05T18:00:00+00:00",
      "providers": {}
    },
    "player_game_logs": {
      "status": "fresh",
      "retrieved_at": "2026-01-05T18:00:00+00:00"
    }
  },
  "h2h": {
    "thin": false,
    "rows": [
      {
        "row_type": "game",
        "player_id": 2544,
        "player_name": "LeBron James",
        "game_date": "2026-01-05",
        "matchup": "LAL @ BOS",
        "minutes": 36.0,
        "stats": {"PTS": 31.0, "PRA": 48.0, "FGA": 19.0, "FG3A": 7.0},
        "deltas": {"PTS": 0.083, "PRA": 0.102, "FGA": 0.018, "FG3A": 0.013}
      },
      {
        "row_type": "average",
        "player_id": null,
        "player_name": null,
        "game_date": null,
        "matchup": null,
        "minutes": 36.0,
        "stats": {"PTS": 31.0, "PRA": 48.0, "FGA": 19.0, "FG3A": 7.0},
        "deltas": {"PTS": 0.083, "PRA": 0.102, "FGA": 0.018, "FG3A": 0.013}
      }
    ]
  },
  "archetype": {"thin": true, "rows": []}
}
```

Both tables carry backend-owned `thin` booleans. A nonempty table contains
newest-first `game` rows followed by exactly one `average` row; an empty table
contains no rows. Game rows always carry ISO game date and stored team/opponent
matchup identity. The average row always carries null date and matchup.
Game rows also identify the sampled canonical player; this makes archetype peer
rows attributable. Average-row player identity is null because it is an
aggregate across the delivered sample.

Each row's `stats` and `deltas` maps contain every Market Category posted for
the selected player, including PRA/PA/PR/RA/STKS and FGA/FG3A/FG2A. A delta is
`game stat / game minutes - sample player's Regular Season stat / Regular
Season minutes`; it is a ±STAT/MIN difference, not a percentage. H2H and
archetype game chronology may include stored Regular Season and Playoffs rows,
but each baseline is derived only from that row's player's stored Regular
Season logs. Average stats and minutes are per-game means. Average deltas are
minutes-weighted against the contributing players' own rates. Numeric outputs
are rounded to six decimal places.

Thinness uses named positive-integer settings:
`MATCHUP_SELECTION_H2H_MIN_GAMES` (default `1`) and
`MATCHUP_SELECTION_ARCHETYPE_MIN_GAMES` (default `5`). A table is thin below
its threshold; an empty table is always thin. Missing logs or a missing usable
Regular Season baseline exclude rows and remain an honest `200` empty/thin
response. The request consumes persisted Player Pool facts, `player_clusters`,
and durable `player_game_logs`; it never calls NBA Stats per player and never
uses recorded test fixtures in production.

`freshness.player_game_logs.status` is `fresh`, `stale`, or `missing`, and its
`retrieved_at` is the timezone-aware durable publication timestamp when one
exists. Stale or missing logs degrade to an honest `200` empty/thin response,
so consumers can distinguish unavailable history from genuinely empty history
under a fresh publication. `freshness.player_pool` preserves the governed
Player Pool freshness document. Mixed provider truth may omit its aggregate
`status`; the per-provider states remain authoritative. The selection lookup
reads the newest reusable stored slate scope containing the game and may serve
it as `stale-served` only within the existing six-hour stale window. It never
acquires a refresh lease or invokes a DFS provider.

| Case | Response |
| --- | --- |
| Known selection with no usable H2H or archetype rows | `200` with the affected `rows: []`, `thin: true` |
| Unknown game in a nonempty Event Catalog, or player absent from a usable stored Player Pool | `404 resource_not_found` |
| Missing, empty, repeated, noncanonical, or extra query parameter | `400 invalid_input` |
| Authentication is missing or invalid | existing `401` authentication error contract |
| Event Catalog is unavailable or empty | `503 provider_unavailable` |
| No contract-valid stored Player Pool contains the valid game | `503 provider_unavailable` |
| Stored Player Pool category is absent from the current Statistic Catalog | `503 provider_unavailable` |

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

The deployment-owned `scripts/nightly_refresh.py` command is not an HTTP
endpoint. It refreshes the stats tables, current-season Event Catalog,
current-season Athlete Catalog, and then durable current-season player game logs,
retrying that ordered unit once. The player-log step uses exactly two
season-wide provider reads—one `Regular Season`, one `Playoffs`—and publishes
their normalized player/game facts plus the season sidecar as one transaction.
For the configured current season, that transaction
also advances the named `player_game_logs` stats freshness; historical
backfills retain independent season freshness and never replace or gate the
current observation. Every result requires a present, fresh, nonempty Event
Catalog whose freshness count agrees with its actual season rows; a nonempty
result also requires a present, fresh, nonempty Athlete Catalog.
`update_database` does not
publish the season-owned Athlete Catalog or its freshness, so Nightly's named
Athlete Catalog step is required. Schedule precedes that step, so an Athlete
Catalog failure skips player logs without suppressing the required schedule
refresh, and the prior player-log publication remains valid.
Failed, wholly unjoinable, malformed, or eligible-identity-removing cumulative
data preserves the last valid publication; individual well-formed unjoined
athlete, game, or team rows are excluded and counted without exposing their
identities. Rows governed as Play-In or another phase outside the explicit
`Regular Season`/`Playoffs` set are excluded under
`unsupported_phase_count`. Stable exclusions and source growth containing only
those governed unsupported phases may republish and advance freshness; they do
not represent an athlete, game, or team identity-join failure, including when
a previously observed unjoined identity remains stable. Governed event phase
is classified before athlete identity, so an unknown athlete on an exact
unsupported event remains an unsupported-phase exclusion.
Every completed, non-postponed governed `Regular Season` or `Playoffs` game
through the source observation time must have logs from its exact phase for
both exact teams and at least the configured
`PLAYER_GAME_LOG_MIN_ACTIVE_PLAYERS_PER_TEAM_GAME` distinct positive-minute
players per team (default `5`). This rejects a truncated first publication
without estimating a season total and does not require future games or DNPs.
The season sidecar stores canonical, bounded raw source-row, and
identity-relevant source-row counts. The identity count excludes only governed
unsupported phases, making prior and current denominators comparable. Stable
partial exclusions can therefore republish idempotently; source growth hidden
by an unjoined athlete, game, or team fails as incomplete canonical identity
evidence instead of republishing an unchanged cumulative snapshot as fresh,
and publication recovers when those exact governed identities arrive.
SQLAlchemy failures from prerequisite freshness/identity reads or publication
are re-raised and emit one bounded rejection aggregate with already-observed
coverage counts; publication failures first roll back.
Canonical athlete identity comes only from the fresh Athlete Catalog. A player
missing from that owner is excluded and counted; the incomplete replacement
fails while the prior publication remains readable until the catalog owner
recovers. Every publication compares the complete prior and candidate
player/game identity sets. Removed stored keys are accepted only when all of
their games were removed from the fresh Event Catalog or made ineligible by
phase/postponement; an eligible-game removal fails even if additions create net
growth. Recovery telemetry exposes the actual bounded admitted removed-key
count rather than the net row-count change.
An empty phase result requires a present schedule with no completed event in
that exact phase, so empty Playoffs is valid before the postseason but not after
a completed playoff game. Completed preseason, exhibition, All-Star, or other
unsupported-phase games do not count, and a postponed event with a terminal-
looking status is not completed evidence. Phase matching normalizes fallback
season-type case and separators when no canonical game-ID phase is available;
fallback stored/display classification spelling remains unchanged when no
closed game-ID prefix applies, while a known NBA game-ID prefix remains
authoritative. In particular, prefix `005` is
governed as Play-In and remains visibly unusual on Slate even if stored with a
misleading `Regular Season` provider label; it is never stored as a durable
player log. Empty union results cannot replace
nonempty facts. Configured-current-season reads also require the
named `stats_refreshes.player_game_logs` observation to exist and be no older
than `PLAYER_GAME_LOG_MAX_AGE_HOURS` (30 by default). Historical reads remain
governed only by their season sidecar and are not hidden by missing, stale, or
newer current-season observations.
These stored facts back future matchup rail and selection reads; this slice
adds no public matchup route and does not change `GET /api/games/game_logs`.
Internal season rates default to Regular Season only unless a caller explicitly
requests Playoffs or all phases. Last-ten minutes and H2H rows include both
stored phases in deterministic chronology. The batch query seam returns
Regular Season rates and oldest-to-newest combined-phase last tens for multiple
canonical player IDs with one player-log rows query.

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

`GET /api/data/telemetry` returns bounded, sanitized provider, board, and
application telemetry counters on the documented seams, with the most recent
50 provider events, internal board-collection events, published board request
events, and `recent_player_pool_events`. Player Pool entries contain only the
per-request `unknown_stat_label_count`, `unjoined_athlete_count`,
`unjoined_event_count`, and `team_mismatch_count`; the
corresponding `player_pool_events_total` and `player_pool_buffered_events`
metrics describe their total and bounded buffer. Board aggregates are kept in separate bounded scalar
collections and do not increment provider event or provider-failure
counters. Provider failures are counted at the provider seams and
application failures by the central error handler; neither list ever carries
credentials, URLs, bodies, or exception text.

The same endpoint includes `recent_player_game_log_events` plus
`player_game_log_events_total` and `player_game_log_buffered_events`. Each
entry contains only source/published row counts, the three unjoined-row counts,
unsupported-phase row count, plus malformed-row, rejected-publication,
exact-duplicate-row, and governed
shrink-recovery row counts;
player, game, team, and provider identities are never telemetry dimensions.

`recent_board_request_events` describes the published `GET /api/dfs/board`
route: exactly one entry per authenticated request, whatever it ended in.
`outcome` and `status_code` are one closed pair — `served`/`200`,
`not_modified`/`304`, `invalid`/`400`, `too_large`/`400`, `disabled`/`404`,
`error`/`500`, `unavailable`/`503` — and they are taken from the status the
caller actually received, so a board that was assembled but failed to render is
an `error`/`500` rather than the `served` it intended. A request refused at the
market ceiling reports the counts and provider facts its read had already
observed, matching the `observed_market_count` in its own 400 body. An
unauthenticated request records nothing,
because telemetry begins where the caller's identity does. Every label is a
closed vocabulary and every other field is a count: provider names may appear
as configured registry names, but no provider-source, athlete, event, market,
or selection ID and no upstream text can become a label. Example shape:

```json
{
  "provider_events_total": 1,
  "board_events_total": 0,
  "board_request_events_total": 1,
  "board_request_buffered_events": 1,
  "board_buffered_events": 0,
  "board_buffered_capacity": 5000,
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
  ],
  "recent_board_events": [
    {
      "started_at": "2026-08-09T20:00:00+00:00",
      "duration_ms": 12.4,
      "request_id": "a1b2…",
      "outcome_complete": 2,
      "outcome_partial": 1,
      "outcome_failed": 0,
      "failure_timeout": 0,
      "failure_deadline_exceeded": 0,
      "failure_rate_limited": 0,
      "failure_access_denied": 0,
      "failure_upstream_error": 0,
      "failure_malformed_response": 0,
      "fetched_count": 20,
      "eligible_count": 12,
      "normalized_count": 12,
      "skipped_count": 8
    }
  ],
  "recent_board_request_events": [
    {
      "started_at": "2026-08-09T20:00:30+00:00",
      "duration_ms": 12.5,
      "request_id": "a1b2…",
      "outcome": "served",
      "status_code": 200,
      "comparison_availability": "available",
      "provider_status_counts": { "complete": 2 },
      "failure_reason_counts": {},
      "freshness_counts": { "fresh": 2 },
      "cache_counts": { "hit": 1, "miss": 1 },
      "group_count": 1,
      "market_count": 2,
      "unresolved_count": 0,
      "disabled_provider_count": 1
    }
  ]
}
```

## Canonical athlete catalog operations

The canonical athlete catalog is an application-owned, persisted read model,
not an HTTP board or Event Catalog route. Operators refresh it with explicit
seasons using:

```bash
python scripts/refresh_athlete_catalog.py \
  --database-url sqlite:////tmp/statsplus.sqlite3 \
  --season 2024-25
```

The command uses the instrumented NBA Stats `player_roster` seam, requires a
writable database, and has no wall-clock season default or background timer.
`AthleteCatalogService.get_catalog(season, active_only=...)` and
`get_freshness(season)` read the persisted catalog and independent success /
failure timestamps. `ATHLETE_CATALOG_FRESHNESS_DAYS` controls the default
seven-day freshness window. Nightly Refresh invokes the same service with its
explicit current season after Event Catalog and before player-game-log
publication; player logs also gate canonicalization on the resulting freshness
fact.

Provider athlete mappings are an internal, persisted read-side seam rather
than new HTTP mutation routes. `AthleteResolver` accepts typed provider
evidence plus an explicit season and only auto-qualifies one exact normalized
official-name match among active season rows with non-conflicting team
evidence. Use the offline operator CLI for list/dry-run/history and audited
approve, override, reject, and clear actions:

```bash
python scripts/athlete_mappings.py dry-run \
  --database-url sqlite:////tmp/statsplus.sqlite3 \
  --provider prizepicks --provider-athlete-id pp-123 \
  --season 2024-25 --name "Nikola Jokic"
python scripts/athlete_mappings.py history \
  --database-url sqlite:////tmp/statsplus.sqlite3 \
  --provider prizepicks --provider-athlete-id pp-123
```

Manual commands require an operator identity and reason. Active rejections
suppress automatic mapping until cleared. The CLI never contacts an upstream
provider and rejects the bundled demo database.

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

## DFS Board

```http
GET /api/dfs/board
Authorization: Bearer <firebase-id-token>
```

One authenticated endpoint exposes the central factual DFS Board. There are no
provider-specific public routes: Dabble, PrizePicks, and Underdog are reached
only through this board.

### Availability

The board is published only when the deployment sets both `DFS_BOARD_ENABLED=true`
and a non-empty `DFS_ENABLED_PROVIDERS`. Both default to off in every
environment, so development and tests opt in explicitly. An unpublished board
answers an authenticated request with `404 dfs_board_disabled` and calls no
provider. Startup fails if the flag is set without a provider registry.

Publication is decided immediately after authentication and before the query
string is read, so *every* authenticated request to an unpublished board is
`404 dfs_board_disabled` — including one carrying an unknown, malformed, or
empty filter, which would otherwise be `400`. No parser, provider, database, or
cache is reached. Authentication still comes first: without credentials the
answer is `401`, published or not.

### Filters

Every filter names an exact identity the board itself established. There is no
fuzzy or partial name filter. Values may repeat (`providers=dabble&providers=underdog`)
or be comma-separated (`providers=dabble,underdog`), and at most 100 values are
accepted per filter. Surrounding whitespace is trimmed, and naming one identity
twice (`providers=dabble,dabble`) is accepted and collapsed to one.

| Parameter | Type | Meaning |
| --- | --- | --- |
| `season` | canonical NBA season (`2025-26`) | Season to read; defaults to the current season. |
| `providers` | `dabble`, `prizepicks`, `underdog` | Restrict retrieval; an excluded provider is never called. |
| `canonical_athlete_ids` | integer NBA player IDs | Restrict to these Canonical Athletes. |
| `canonical_event_ids` | NBA game IDs | Restrict to these Canonical Events. |
| `canonical_statistic_ids` | Statistic Catalog IDs | Restrict to these Canonical Statistics. |
| `market_statuses` | `available`, `suspended` | Restrict to these Market Statuses. |

Any other query parameter, an unsupported vocabulary value, an unparsable
identity, or a non-canonical season returns `400 invalid_input` in the shared
error shape. A misspelled filter is refused rather than ignored.

A supplied filter is never widened back to "no filter". An empty value or an
empty member of a comma-separated list is `400 invalid_input` too — `providers=`,
`providers=,`, `providers=dabble,`, `market_statuses=`, a blank canonical ID, and
`season=` are all refused, and none of them calls a provider or reads a
database. Omit the parameter entirely to accept the unfiltered board or the
default season.

### Response semantics

A `200` body states contract version `1`. Every exact decimal — thresholds,
spreads, modifiers, prices, ages — is a JSON **string** in the scale the
provider published, never a JSON number. Every timestamp is timezone-aware UTC.
Collections are deterministically ordered, so equivalent observations produce
identical JSON and identical entity tags.

The body carries `comparison_availability` with each canonical catalog's
identity, last success, age, and configured maximum age; `comparison_groups`
with exact minimum, maximum, Threshold Spread, counts, freshness, and sorted
market references; `unresolved_markets` with the closed exclusion vocabulary
that explains each; `markets` with the complete retained normalized evidence and
provenance for every observation; `provider_reports` with each provider's
status, stable failure reason, coverage counts and completion evidence, age,
freshness, and cache state; and `disabled_providers`.

Version 1 reports facts only. It produces no probability, expected value,
recommendation, average, preferred market, or entry payout.

### Statuses

| Status | When |
| --- | --- |
| `200` | At least one provider produced a *readable* snapshot — complete, partial, permitted-stale, or empty-complete. An empty complete snapshot is a valid empty board, not an outage. |
| `304` | The caller's `If-None-Match` already holds an equivalent board. |
| `400 invalid_input` | A filter cannot be parsed or is outside a supported vocabulary. |
| `400 board_too_large` | The post-filter board exceeds `DFS_COMPARISON_MAX_MARKETS` *and* at least one provider was readable. Nothing is truncated; `details` states `observed_market_count`, `market_limit`, and `supported_filters`. |
| `401` | Missing or invalid Firebase credentials. |
| `404 dfs_board_disabled` | The deployment does not publish the board. |
| `503 provider_unavailable` | No provider produced a readable snapshot, whatever the read's size. `details` states the sanitized Provider Outcomes and the disabled providers. |

A snapshot is *readable* only while the board can still compare it. A retrieval
that succeeded but is older than that provider's `DFS_<PROVIDER>_CACHE_STALE_IF_ERROR_SECONDS`
ceiling, or timestamped ahead of the board's own clock, resolves no market at
all — every market it carried is `stale_snapshot` or `future_snapshot` — so a
board carrying only those answers `503` rather than a `200` stating nothing. The
maximum age is inclusive: a snapshot exactly at the ceiling is still readable.

Readability is decided before size. A read no provider could be read from is
`503` however many markets it observed, because narrowing filters cannot make an
outage readable; `board_too_large` is only ever returned for a read at least one
provider *was* readable on.
Each sanitized Provider Outcome in the `503` body therefore also states its
`freshness` (`fresh`, `stale`, or `null`) and `future_observation`, so a caller
can tell a failed retrieval from an unreadable one.

### Caching

Responses carry a weak `ETag`. It identifies the board's stated facts: the
instant of observation and the ages derived from it are deliberately excluded,
so an unchanged board revalidates as `304` rather than resending a board that
differs only in how long ago it was read. Send the tag back verbatim in
`If-None-Match`.

An authenticated board is never shared. *Every* response from this route —
`200`, `304`, `400`, `401`, `404`, `503`, and a centrally handled `500` alike —
carries `Cache-Control: private, no-cache, max-age=0, must-revalidate`,
`Vary: Authorization`, `X-Content-Type-Options: nosniff`, and `X-Request-ID`, so
no failure for one caller can be served from a shared cache to another. `Vary`
is added rather than replaced, so a `Vary: Origin` from CORS survives beside it.

`ETag` is the exception: it identifies one board, so it appears on `200` and
`304` only and never on a failure.

### Executable response fixtures

The complete-success, mixed partial/stale, empty, oversized, unauthenticated,
disabled, total-failure, unreadable-snapshot, and unreadable-oversized responses
are recorded verbatim in
`tests/fixtures/dfs_board/` and asserted byte-for-byte by
`tests/test_dfs_routes.py`, so these examples cannot drift from behavior:

| Response | Fixture |
| --- | --- |
| Complete success | `tests/fixtures/dfs_board/complete.json` |
| Mixed partial/stale | `tests/fixtures/dfs_board/mixed_partial_stale.json` |
| Empty complete | `tests/fixtures/dfs_board/empty.json` |
| Oversized | `tests/fixtures/dfs_board/oversized.json` |
| Unauthenticated | `tests/fixtures/dfs_board/unauthenticated.json` |
| Disabled | `tests/fixtures/dfs_board/disabled.json` |
| Total failure | `tests/fixtures/dfs_board/total_failure.json` |
| Unreadable snapshot | `tests/fixtures/dfs_board/unusable_snapshot.json` |
| Unreadable and over the ceiling | `tests/fixtures/dfs_board/unreadable_oversized.json` |

Rerecord them with `RECORD_BOARD_FIXTURES=1 pytest tests/test_dfs_routes.py`
after an intended contract change, and review the diff.

### Operating the board

- **Configuration.** `DFS_BOARD_ENABLED`, `DFS_ENABLED_PROVIDERS`,
  `DFS_BOARD_DEADLINE_SECONDS`, `DFS_PROVIDER_CONNECT_TIMEOUT_SECONDS`,
  `DFS_PROVIDER_READ_TIMEOUT_SECONDS`, `DFS_COMPARISON_MAX_MARKETS`, and the
  `DFS_CACHE_*` windows. See `.env.example` for safe disabled defaults.
- **Catalog refresh.** Comparisons need fresh Athlete and Event Catalogs.
  Deployment-owned scheduling runs `scripts/refresh_athlete_catalog.py` and
  `scripts/refresh_event_catalog.py` daily with explicit seasons; API workers
  run no scheduler. A missing or over-age catalog makes
  `comparison_availability.available` false and keeps normalized markets
  visible — it never turns a board into an outage. Windows are
  `ATHLETE_CATALOG_FRESHNESS_DAYS` (7) and `EVENT_CATALOG_MAX_AGE_HOURS` (72).
- **Redis degradation.** The snapshot cache fails open. With `ENABLE_CACHE=false`
  or an unreachable Redis, providers are retrieved directly and each provider
  report states its `cache` status and any refresh failure reason. Cache
  availability is not board availability.
- **Mapping review.** Ambiguous or conflicting provider identities are governed
  offline with `scripts/athlete_mappings.py` and `scripts/event_mappings.py`
  (list, dry-run, approve, reject, override, clear, history). Manual decisions
  require an operator identity and reason. Version 1 has no mapping mutation
  HTTP API.
- **Scope.** NBA pregame Player Projection Markets only. Live, closed, settled,
  team, match, futures, entry-placement, and non-NBA offerings are out of scope.

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
