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

- Required: `GET /api/games/slate`, `GET /api/games/matchup`, `GET /api/games/game_logs`, `GET /api/games/matchup/selection`, `GET /api/dfs/board`, `POST /api/nl-query`, and most `/api/user/*` routes.
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
| Collection operation conflict | `operation_conflict` | 409 | A collection fence, immutable cycle, retry state, or idempotency key conflicts with durable current state. |
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
      "state": "live",
      "observed_at": "2026-01-02T10:04:00+00:00",
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
      "projection_state": {
        "state": "live",
        "observed_at": "2026-01-02T10:04:00+00:00"
      },
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
When a prior Matchup read has stored a still-fresh or permitted-stale injury
snapshot, a matched Out player is also removed from the corresponding Slate
count. This is Slate's existing stored-snapshot count contract: Slate never
calls the injury provider, and an expired or unavailable injury snapshot
cannot change counts. Matchup Injury Reports retain their separate
live/snapshot contract.

`freshness.pool.providers` uses the closed vocabulary `fresh | stale-served |
missing` and retains each usable provider snapshot's actual retrieval time. A
successful empty board is fresh information and produces zero counts. The
aggregate is `fresh` only when every provider is fresh, `stale-served` when
every usable provider is stale-served, and `unavailable` when none is usable.
For any mixed state—including fresh plus stale-served, or usable plus
missing—aggregate `status` is omitted so the frontend derives its documented
partial/degraded presentation from the provider entries. The union uses every
usable observation. Every configured provider required by the archive reader
is represented; one with no eligible current row is emitted as
`{ "status": "missing", "retrieved_at": null }` rather than omitted.
The Player Pool is read entirely from stored projection evidence; a request
never fetches a provider board or refreshes a legacy snapshot. A confirmed
offering no more than 15 minutes old is served as `fresh` and retains each
provider's actual `retrieved_at`. This is an inclusive reuse maximum age, not
the provider cache's exclusive fresh window. Partial coverage marks absent
providers `missing`. On total failure, the last confirmed evidence is served
through six hours with aggregate and contributing-provider status
`stale-served`; beyond six hours the pool is empty and `unavailable`. No
synthetic pool is produced.
Aggregate `retrieved_at` is the oldest usable contributor snapshot, so its age
never understates any provider observation included in the union.

`PROJECTION_ARCHIVE_READ_ENABLED` is the operator-controlled activation switch
that selects one database-only reader for Slate, Matchup, and Matchup Selection.
The #110 cutover removed the legacy request-time reader, so when the operator
enables the gate this reader is the sole source with no per-request fallback.
While the gate is off, Slate and Matchup return zero targetable players with no
`projection_state` and Matchup Selection returns `503 provider_unavailable`. The
empty legacy `player_pool_snapshots` table is retired by construction — nothing
writes it — pending its #111 removal.
Each Slate game then adds `projection_state` with `state: live | closing | missing` and a
timezone-aware `observed_at` or null. `freshness.pool` adds the same `state` and
`observed_at` fields. Current archived Latest Player Projections produce
`live` while their per-offering confirmation is no more than 15 minutes old.
When governed Event Catalog status first says a game is in progress or final,
the Event Catalog persists that observation time. The scheduled projection
collector—not a GET request—uses it to freeze one immutable Closing Projection
Set per provider, exact query, and game from the materialized pre-start state.
Legacy started events without an observed transition time use `scheduled_at`
as the fence. Slate and Matchup then report `closing` with the oldest
observation time in that set; a contributing provider reports status
`closing`, never live `fresh`. Closing membership points at immutable
Projection Observations, is never aged by the live 15-minute or six-hour
windows, and is not changed by a late-arriving pregame poll. A started or final
game whose collector has not created a set yet, or whose set has no members,
reports `missing` and zero targetable players. Reads use only the requested
game IDs and never create or freeze a set.
After a failed provider poll, prior confirmed offerings may be served as
`stale-served` only through the inclusive six-hour fallback. Partial polls
update and confirm only included references; omissions retain their prior
evidence and confirmation. Complete empty snapshots retire only the same
provider/query scope and remain fresh successful provider evidence. When every
required provider is current and Complete-empty, the database-first pool is
`live`/`fresh` with zero players rather than unavailable, and each requested
Slate game's `projection_state` is `live` with that accepted evidence time and
zero targetable players. Empty evidence from a disabled/non-required provider
is still reported at provider level but cannot make missing required coverage
aggregate `live`; with every provider disabled it expires after the inclusive
15-minute live window. A direct Matchup
Selection request for a player outside that derived empty pool returns the
existing `404 resource_not_found`; never-polled or failed-without-successful-
evidence scopes remain missing and keep the documented `503 provider_unavailable`.
Changes to canonical statistic
resolution or category authority can retire or add eligible Latest rows without
duplicating unchanged provider evidence. The response `observed_at` and
provider `retrieved_at` come from each selected Latest row's `observed_at`, never
from its replay/promotion `confirmed_at`; an ordinary newer promoted poll
advances that read-model timestamp, while replay retains the source observation
time. Aggregate times are the oldest included source observations. Only absent current evidence produces `missing` with zero
targetable players. For a multi-game request with both live and missing games,
`freshness.pool.status` is explicitly `partial`, and each game's
`projection_state` remains authoritative; the pool retains `state: live`
because it contains live rows. Aggregate and per-provider observation times
are the oldest included times, so neither understates the age of evidence in
that live union. When a request spans live, closing, and missing phases, live
evidence alone controls aggregate and provider freshness, closing time never
ages that live aggregate, and aggregate `status` is omitted. Without live
evidence, a non-empty closing pool takes precedence over missing games.
An unchanged provider poll is recognized from canonical market, coverage, and
query content even though its retrieval time is newer; it confirms existing
Latest references without duplicating observations while the immutable snapshot
remains the content authority. Enabled providers are unioned; an unpolled or
disabled provider expires independently and cannot erase another contribution.
An approved athlete, event, or statistic mapping may replay matching unresolved
observations into a deterministic materialization generation, or reactivate an
existing generation when a decision returns to an earlier materialized state.
This is an internal database operation: it does not add a route, change any
Slate, Matchup, or Matchup Selection payload, call a provider, or rewrite the
source snapshot and its observations. It preserves the provider-reported player
name, recomputes an ID-less market reference from the rematerialized identities,
and retains the source `observed_at`/`retrieved_at` for public freshness. Replay
does not grant a new live window: affected rows expire with the provider board,
while `confirmed_at` remains an internal eligibility clock. A snapshot fetched
before that decision whose observations still carry the pre-decision unresolved
identity cannot promote after replay and erase the recovered state; evidence
with a newer source retrieval identity may promote normally. Until replay
succeeds, unresolved observations remain archive evidence only and do not
contribute to Latest or Player Pool.
Late valid polls remain archived but do not refresh eligibility or mask a newer
failure; the failure attempt's actual start time (or its completion time when
the start is unavailable) fences evidence retrieved earlier, even when that
evidence arrives later or waited behind that failure's database fence after
capturing an earlier acceptance time. Confirmation timestamps never move
backward. A success retrieved after the failed attempt may promote and recover
provider health,
including when that success is accepted before the older failed attempt finishes.
The request does not fall back to the legacy Player Pool or call a projection
provider. Enabling the gate with the read-only demo database is refused at
startup. With the gate left at its default `false`, the existing response and
legacy reader remain unchanged during expansion.
Matchup Selection uses the Closing Projection Set for an in-progress or final
game and keeps the market categories from that set without applying live
freshness. A selected player outside the derived closing pool returns the
existing `404 resource_not_found`; a scheduled game with no usable stored pool
retains the existing `503 provider_unavailable` contract. Slate and Matchup
still expose the explicit missing projection state described above.

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

### Get Matchup

```http
GET /api/games/matchup?game_id=<nba_game_id>
Authorization: Bearer <firebase-id-token>
```

Returns one complete stored matchup document. `game_id` is one nonempty NBA
string. Missing, empty, repeated, whitespace-padded, or extra parameters return
`400 invalid_input`; an unknown game in a populated current-season Event
Catalog returns `404 resource_not_found`. If no schedule is stored at all, the
route returns `503 provider_unavailable`.

The request makes no NBA Stats, PBP Stats, or DFS Board call. It composes the
Event Catalog, newest reusable stored Player Pool containing the game, durable
player-log summaries, Season Player Diet facts, and the newest stored team
Season and exact team Last-15 windows no later than the Slate Date. Before tip,
the separately gated injury service reuses a league observation through five
minutes and otherwise may make one RotoWire request. At or after tip it makes no
injury-provider call and retains the final stored observation.
A missing/degraded pool or stats surface remains a truthful `200` with empty or
nullable data and explicit freshness/availability; no request-time provider
fallback, fabricated zero, Season-for-Last-15 substitution, or estimate is
used. This rule also governs past games: only a Player Pool snapshot still
servable under its landed stored-pool rules may populate `players`.

Top-level fields are required:

```text
game, experience, league, teams, players, injuries, freshness
```

`experience` is the additive Historical Matchup declaration (#208, #209). It is
always present, so a client never infers the mode from tip dates, empty arrays,
or freshness markers:

```json
{
  "mode": "historical",
  "player_source": "game_logs",
  "sections": {
    "schedule": {
      "status": "available",
      "source": "event_catalog",
      "context": "completed_season_catalog",
      "unavailable_reason": null,
      "collected_at": "2026-03-30T04:10:00+00:00"
    },
    "participants": {
      "status": "available",
      "source": "player_game_logs",
      "context": "completed_season",
      "unavailable_reason": null
    },
    "season_defense": {
      "status": "available",
      "source": "team_matchup_publication",
      "context": "completed_season",
      "unavailable_reason": null
    },
    "last_15_defense": {
      "status": "unavailable",
      "source": null,
      "context": null,
      "unavailable_reason": "no_point_in_time_snapshot"
    },
    "injuries": {
      "status": "unavailable",
      "source": null,
      "context": null,
      "unavailable_reason": "no_pregame_snapshot"
    }
  }
}
```

`mode` is `historical` for a completed, non-postponed Regular Season game whose
governed Player Pool contributes no player for either team; it is `current`
otherwise. A closing projection set with memberships always contributes
players, so this is exactly "a final Regular Season game with no archived
closing projections". A final game with governed archived projection evidence,
or with any still-servable stored Player Pool, therefore keeps the
evidence-appropriate existing experience, as does every scheduled or live game.
`player_source` is `game_logs` in historical mode and `player_pool` otherwise.

`sections` always contains exactly `schedule`, `participants`,
`season_defense`, `last_15_defense`, and `injuries`. Every section carries its
own `status` (`available | unavailable | missing`), `source`, `context`, and
`unavailable_reason`; no section's evidence governs another's. `source` is one
of `event_catalog`, `player_game_logs`, `player_pool`,
`team_matchup_publication`, `rotowire`, or `null`. `context` is one of
`completed_season_catalog`, `current_season_catalog`, `completed_season`,
`pregame`, `posted_markets`, `current`, or `null`; `completed_season` is the
hindsight label a client renders beside a completed-season window.

The Schedule section is always `available` and additionally carries
`collected_at`, the same Event Catalog collection time reported by
`freshness.schedule`. Completed-season Schedule evidence is immutable, so that
age never makes the section stale; the unchanged `freshness.schedule` surface
keeps its existing age-based `fresh`/`stale`/`missing` status.

A Defense Sheet section is `available` whenever any of its five governed Bases
is available for that window, so an unavailable Last-15 window, a missing
Player Pool, missing participants, unavailable injuries, and a missing legacy
`stats_tables` freshness marker can none of them suppress an available Season
Defense Sheet. `league.surface_availability` remains the sole per-Base
authority, and a section that is not available repeats the first governed Base
reason. In historical mode a Last-15 window with no available Base reports
`no_point_in_time_snapshot`, because no point-in-time snapshot was captured for
a completed game.

The Participants section reports `game_logs_incomplete` when the game's
canonical player-log synchronization is not complete and `no_game_log_rows`
when it is complete but names nobody on either team; in current mode it reports
`player_pool_unavailable` when no stored pool player belongs to the game. In
historical mode the Injuries section never presents current injury data. A
stopped game serves only its retained pre-tip snapshot, so a retained snapshot
is `available` with context `pregame` and anything else is
`unavailable/no_pregame_snapshot`.

This block is purely additive. Every other field, value, and error contract on
this route is unchanged in both modes, so a frontend that ignores `experience`
behaves exactly as it did before backend-first rollout.

`game` is the same header as a Slate card. `teams` is ordered away then home,
and its targetable counts reflect the returned stored pool players. Canonical
NBA player IDs are JSON integers; NBA game IDs remain strings.

`league.defense_sheet` and every `teams[].defense_sheet` contain exactly these
five Bases:

```text
play_types, shot_zones, shot_types, assist_locations, traditional
```

Each league row is `{ key, season, last_15 }`; an available window is
`{ average_allowed_per_48, sigma }`. Each team row is
`{ key, label, markets, season, last_15 }`; an available window is
`{ allowed_per_48, percent_vs_league_average, sigma_deviation, rank }`.
Keys match exactly between team and league rows. Values, population sigma,
sigma deviation, and rank are backend-derived from the stored 30-team raw fact
set. `league.defensive_columns` and `teams[].defensive_columns` contain exactly
`OPP_TOV`, `OPP_STL`, and `OPP_BLK`; their league windows use
`{ average_per_48, sigma }` and their team windows use
`{ per_48, percent_vs_league_average }`.

`league.surface_availability[base][season|last_15]` is the sole team-window
status authority:

```json
{
  "status": "available | unavailable | missing",
  "unavailable_reason": null
}
```

When the status is not `available`, every metric value for that Base/window is
`null`. In particular, exact Synergy play types Last-15 is always `null` with
`status: "unavailable"` and `unavailable_reason: "provider_window_unsupported"`;
Season values are never substituted. Independently published Season and
Last-15 scopes can contain different metric identities; the affected
Base/window becomes `unavailable/legacy_surface_incomplete` rather than making
the request fail or inventing the absent metric. An event team outside the
governed franchise fact set makes otherwise available Base/windows
`missing/team_not_in_governed_roster`, while the known game header and other
stored response sections still return.

There is exactly one row-level exception while a Base/window remains
`available`: a pre-OPP_REB traditional Season or Last-15 snapshot may omit only
`OPP_REB`. If the other window supplies that identity, the league and team
`OPP_REB` row windows are `null` only for the legacy scope, while traditional
availability and the `OPP_TOV`, `OPP_STL`, and `OPP_BLK` rows and defensive
columns stay available. The matching REB score window is
`components: {}` / `blend: null`; the other REB window remains computable.
Every other missing or divergent metric identity still downgrades the entire
affected Base/window to `unavailable/legacy_surface_incomplete` and nulls all
of its row windows. The expected traditional identity set is the union across
the independently stored windows (plus the required defensive columns), with
only `OPP_REB` excluded for the compatibility carveout; for example, an
`OPP_PF` row present in only one window downgrades the other window locally.

Shot-zone row markets are constrained by the slice as well as the statistic.
Restricted Area, In The Paint (Non-RA), and Mid-Range FGA rows target only FGA
and FG2A; Corner 3 and Above the Break 3 FGA rows target only FGA and FG3A.
Two-point-zone FGM targets PTS, while three-point-zone FGM targets PTS and 3PM.
Those five slices are the complete nonoverlapping shot-zone response vocabulary;
stored Left/Right Corner 3 children, Backcourt, and unknown duplicate slices are
not emitted or aggregated. If any of the five aggregate slices is absent, the
affected Base/window is `unavailable/legacy_surface_incomplete`.

Shot-type response keys canonicalize the stored lookup vocabulary to the same
three slice names used by Player Diets: `Catch and Shoot`, `Pullups`, and
`Less Than 10 ft`. Team and league keys retain their stat suffix after that
canonical slice. A missing or unknown stored shot type makes only that
Base/window `unavailable/legacy_surface_incomplete`; divergent keys never leak.

Each stored pool player has this shape:

```json
{
  "canonical_id": 2544,
  "name": "LeBron James",
  "team_id": 1610612747,
  "tricode": "LAL",
  "player_source": "player_pool",
  "stat_categories": ["FGA", "PTS"],
  "focal_game_line": null,
  "posted_markets": ["FGA", "PTS"],
  "provenance": {
    "prizepicks": ["FGA", "PTS"],
    "underdog": ["PTS"]
  },
  "season_scoring": 25.4,
  "last_10_minutes": [35.0, 36.0],
  "diet_shares": {
    "play_types": [
      {
        "key": "Transition",
        "season": {
          "share": 0.19,
          "volume": 95.0,
          "games_played": 20,
          "volume_unit": "possessions",
          "league_average_share": 0.321,
          "sigma_deviation": 1.08
        }
      }
    ],
    "shot_zones": [],
    "shot_types": [],
    "assist_locations": []
  },
  "scores": {
    "PTS": {
      "season": {
        "components": {
          "play_types": { "value": 0.08, "thin": false },
          "shot_zones": { "value": 0.12, "thin": false }
        },
        "blend": { "value": 0.10, "thin": false },
        "missing_inputs": ["player_diet:shot_types"]
      },
      "last_15": {
        "components": {
          "shot_zones": { "value": -0.03, "thin": true }
        },
        "blend": { "value": -0.03, "thin": true },
        "missing_inputs": ["team_defense:play_types", "player_diet:shot_types"]
      }
    },
    "FGA": {
      "season": {
        "components": {
          "shot_zones": { "value": 0.04, "thin": false }
        },
        "blend": { "value": 0.04, "thin": false },
        "missing_inputs": ["player_diet:shot_types"]
      },
      "last_15": {
        "components": {
          "shot_zones": { "value": -0.02, "thin": false }
        },
        "blend": { "value": -0.02, "thin": false },
        "missing_inputs": ["player_diet:shot_types"]
      }
    }
  },
  "injury_badge_ref": null
}
```

`player_source`, `stat_categories`, and `focal_game_line` are additive and
always present. `player_source` is `player_pool` for a stored pool player and
`game_logs` for a Historical Matchup participant. `stat_categories` is always
exactly the key set of `scores`; in current mode it equals `posted_markets`,
and in historical mode it is the governed Statistic Catalog crossed with the
score-input contract rather than any DFS archive. `focal_game_line` is `null`
in current mode.

Player Diet facts are unthresholded raw Season shares and volumes; `share` and
`volume` are never filtered or floored. Each fact additionally carries
`league_average_share` and `sigma_deviation`, backend-derived against a
per-Base league population and always present together: `league_average_share`
is the arithmetic mean of `share` across the population for that (Base,
slice), and `sigma_deviation` is `(share - league_average_share)` divided by
the population standard deviation (`statistics.pstdev`), mirroring the team
Defense Sheet convention -- `0.0` when the population sigma is zero. Both
fields are `null` together when the population has fewer than two players. A
player's own fact belongs to its (Base, slice) population when that fact's
own `games_played` clears `PLAYER_DIET_BASELINE_MIN_GAMES` (default 5), and
their total Base volume per game -- the sum, over every stored fact of that
player in the Base, of that fact's own `volume ÷ games_played` -- clears the
Base's floor: `PLAYER_DIET_BASELINE_PLAY_TYPES_MIN_VOLUME_PER_GAME` (default
6.0 possessions), `PLAYER_DIET_BASELINE_SHOT_ZONES_MIN_VOLUME_PER_GAME`
(default 6.0 FGA), `PLAYER_DIET_BASELINE_SHOT_TYPES_MIN_VOLUME_PER_GAME`
(default 6.0 FGA), and `PLAYER_DIET_BASELINE_ASSIST_LOCATIONS_MIN_VOLUME_PER_GAME`
(default 2.0 assists). Summing each fact's own `volume ÷ games_played`, rather
than dividing total volume by one shared games-played value, keeps this total
equal to total volume divided by games played when games played is uniform
across the Base, and keeps it independent of fact order and of which slice is
being baselined when it is not. The population is the whole stored season
fact set for the Base -- the activated publication payload when a Base is
served from a publication, otherwise the `player_diet_facts` rows for the
season, never mixed within a Base -- so a delivered fact is scored against the
full population even when its own player fails that Base's floors. There is no
player Last-15 field and no manufactured traditional Diet Base. Missing player
logs yield `season_scoring: null` and an empty minutes series rather than zero.

`scores` has exactly one row for every `stat_categories` value and no other
row. In current mode `stat_categories` equals `posted_markets`, so a stored
pool player still has exactly one row per posted market and no unposted row; a
Historical Matchup participant carries `posted_markets: []` and is keyed by the
governed categories instead. Each row always carries independent `season` and
`last_15` windows. A
component cell's `value` is the fractional difference from a league-average
matchup (`0.08` renders as `+8%`), calculated as the sum across that Base's
slices of:

```text
player Season Diet Share ×
  (opponent allowed-per-48 / league average allowed-per-48 - 1)
```

Equivalently, the score subtracts the actual applied Diet `weight_total`, not a
hard-coded `1`. Any unobserved residual in an admitted provider-rounded
partition is neutral: it contributes zero matchup difference. The backend
preserves the raw shares, does not normalize a materially partial Diet, does not
fabricate concession evidence for that residual, and makes no request-time
estimate. Provider shares are rounded, so
Base-specific completeness bounds admit the observed complete partitions:
play types `0.995..1.005`, shot types `0.900..1.010`, and derived shot-zone and
assist-location shares within `0.000001` of one. Unknown/duplicate slices and
missing governed slices or shares outside those bounds fail closed. A non-defensive Blend is the simple
mean of its computable Base components. An unavailable Base is omitted, not
emitted as a null cell. Thus provider-unsupported play-types Last-15 never
receives the Season component, while other available Last-15 Bases still score.
Within an otherwise complete Base, a slice whose league and opponent values are
both exactly zero is a structural zero and contributes a neutral zero matchup
difference; supported slices still score at their raw shares. A non-positive
league value paired with nonzero opponent evidence fails the component closed,
and a component with only structural-zero slices remains unavailable rather
than fabricating numeric zero.

For every blendable offensive window, `components: {}` and `blend: null`
truthfully mean that zero components were computable. Whenever at least one
offensive component is present, `blend` is a score cell with `value` and
`thin`; the backend never fabricates a numeric Blend merely to avoid `null`.

The stored Diet/sheet intersection supports PTS through play types, shot zones,
and shot types; FGA through shot zones and shot types; AST through assist
locations; and 3PM, FG2A, and FG3A through shot zones and shot types. PTS
shot-type concessions derive stored points as `2 × FG2M + 3 × FG3M`, and FGA
derives `FG2A + FG3A`. The attempt markets use their matching stored shot stat;
3PM uses stored `FGM` in the two three-point zones and `FG3M` by shot type.
For zone-specific attempt/make markets, the player's stored FGA volumes derive
the exact conditional Diet across the applicable two- or three-point zones;
this is not normalization of missing evidence. REB uses the stored traditional
`OPP_REB` aggregate with implicit share one. Its required offensive Blend is
the same numeric cell as its single `traditional` component. Legacy traditional
windows without `OPP_REB` keep their valid OPP_TOV/OPP_STL/OPP_BLK surface and
defensive scores; only REB and rebound-containing combos degrade locally. This
is the sole available-Base row-level null exception described above. Because
the REB primitive consumes neither Player Diet nor player Season sample
evidence, its component and Blend are not thin merely because the player has
fewer than `MATCHUP_SCORE_MIN_GAMES`; combos that consume REB still apply the
combo-level Season game minimum described below.

PRA, PA, PR, and RA combine PTS/REB/AST part scores using the player's stored
Season per-game volumes. Every component and Blend uses the fixed denominator
of all required parts with positive Season volume. An unavailable part supplies
a neutral zero-delta numerator without renormalizing the surviving parts; if
some parts remain computable, the truthful partial numeric result is retained
and every delivered combo component and Blend is thin. `blend` remains null
when no part contributes. TOV, STL, and BLK
have only a `traditional` component against their matching `OPP_*` column.
STKS Season-volume-weights the stored OPP_STL and OPP_BLK comparisons into one
`traditional` component. These defensive windows omit `blend` (a JSON `null`
is also contract-equivalent) so the response never pretends a one-Base result
is a Blend.

Every score window additionally carries `missing_inputs`, an always-present
list naming the score-contract inputs that window could not consume. Its values
are `team_defense:<base>` when that Base/window is not available,
`player_diet:<base>` when an available Base had no complete stored player Diet,
and `player_season_rate` when a combo or STKS window lacked the player's stored
Season rate. `<base>` is one of the five governed Bases. An empty list means
every required input was present.

In current mode `missing_inputs` only names the gaps: every score formula,
threshold, and blend rule above is unchanged, and a current window's cells are
identical to what they were before. In historical mode the Blend is
additionally **withheld** whenever `missing_inputs` is nonempty, so `blend` is
`null` and a mean of the surviving Bases is never presented as a complete
blended score. Component evidence still ships, and the named gaps explain the
withheld Blend. Defensive windows omit `blend` in both modes. Combos withhold
their own Blend under the same rule rather than inheriting a partial one
through their parts. The score formulas themselves are unchanged: a historical
window whose `missing_inputs` is empty carries exactly the cells it always
did.

Every numeric cell carries `thin`. The backend marks a Diet component thin when
the player's Season sample is below `MATCHUP_SCORE_MIN_GAMES` (default `5`) or
its total Base volume per game is below the matching named floor: play types
`1`, shot zones `1`, shot types `4`, and assist locations `1` by default. A
Blend is thin when any contributor is thin; every combo component and Blend
also uses the Season-rate game minimum. Thin cells retain their numeric values. Team
window unavailability omits a component rather than mislabeling it thin.

Injury collection is disabled by default. Disabled and enabled-without-
permission states remain present-but-unavailable and make no provider request:

```json
{
  "status": "unavailable",
  "unavailable_reason": "disabled",
  "retrieved_at": null,
  "source": "rotowire",
  "source_url": "https://www.rotowire.com/basketball/injury-report.php",
  "teams": []
}
```

Set both `INJURY_REPORT_ENABLED=true` and
`ROTOWIRE_PERMISSION_GRANTED=true` only when written permission or explicit
legal approval covers automated collection and display. Enabled without the
permission assertion returns `unavailable_reason: "permission_required"`.
There is no fallback provider.

Before tip, a stored injury snapshot is reused through an inclusive five
minutes. The first later matchup request refreshes it lazily. If refresh fails,
the preceding snapshot is served through an inclusive 30 minutes with
`status: "stale"`; past that it becomes
`unavailable/fetch_failed` and cannot change Player Pool membership. At tip or
when the game is final, collection stops and the last snapshot is retained as
historical evidence, but wall-clock freshness still applies: it is `fresh`
through five minutes, `stale` through 30 minutes, and then
`unavailable/fetch_failed`. An expired historical snapshot remains stored but
cannot apply an Out override or badge. If none was collected, the historical
block is `unavailable/fetch_failed` and no post-tip request is attempted.

The RotoWire table is league-wide. One append-only raw source observation is
shared by every game read inside its five-minute reuse window; each game stores
only its reconciled entries plus a reference to that source observation. This
prevents another full-table fetch and raw-payload copy for each matchup while
preserving the exact evidence referenced by a game's retained snapshot.
Concurrent refresh suppression is local to one application worker; waiters
recheck the shared durable observation, and no cross-worker single-flight is
claimed. Referenced observations are retained indefinitely as game evidence.
Of observations no game references, only the newest 12 per provider are kept.

An entry's `source_url` is either an HTTPS URL on `rotowire.com` or one of its
subdomains, or the fixed RotoWire injury-report URL used when the provider value
is absent or fails that origin policy.

A usable report has exactly the away and home teams, in that order:

```json
{
  "status": "fresh",
  "unavailable_reason": null,
  "retrieved_at": "2026-01-03T00:55:00+00:00",
  "source": "rotowire",
  "source_url": "https://www.rotowire.com/basketball/injury-report.php",
  "teams": [
    {
      "team_id": 1610612747,
      "tricode": "LAL",
      "submission_state": "unknown",
      "entries": [
        {
          "entry_id": "rotowire:6504",
          "source_player_id": "6504",
          "source_player_name": "LeBron James",
          "canonical_player_id": 2544,
          "team_id": 1610612747,
          "tricode": "LAL",
          "canonical_status": "Questionable",
          "raw_status": "Questionable",
          "reason": "Left ankle soreness",
          "source_url": "https://www.rotowire.com/basketball/player/lebron-james-2344"
        }
      ]
    },
    {
      "team_id": 1610612759,
      "tricode": "SAS",
      "submission_state": "unknown",
      "entries": []
    }
  ]
}
```

`canonical_status` is strictly `Probable | Questionable | Doubtful | Out |
null`. An unfamiliar value remains visible in `raw_status` with a null
canonical value; `GTD` is never invented. `submission_state` is always the
literal `unknown`. An unmatched or ambiguous athlete stays visible with
`canonical_player_id: null` and cannot override the pool. A matched Out entry
removes that player from `players` and decrements the corresponding returned
team's targetable count; other
canonical statuses retain the player and set `injury_badge_ref` to the entry
ID. Neither path changes scores, Diet Shares, scoring history, or projected
roles.

Slate targetable counts use the same stored-snapshot reader and freshness
rules. Slate never starts injury collection: it subtracts matched Out players
only when a prior matchup read left a still-usable snapshot, so Slate and
Matchup agree without turning one Slate request into per-game provider calls.
RotoWire team dialects `GS`, `NY`, `SA`, `PHO`, and `NO` normalize to the NBA
tricodes `GSW`, `NYK`, `SAS`, `PHX`, and `NOP`. A source row whose team cannot
be resolved cannot truthfully be assigned to either matchup team; it remains
in durable raw evidence, is excluded from overrides, and increments bounded
unresolved-team telemetry instead of disappearing silently.

`freshness` retains the existing `schedule`, `pool`, `stats`, and `injuries`
surfaces and additionally reports `player_game_logs`, per-Base
`player_diets.surfaces`, and Season/Last-15
`team_matchups[window].surfaces`. A valid stored observation carries its actual
stored timestamp even when its routine status is `unavailable` (for example,
unsupported Synergy L15). Missing evidence and invalid/corrupt publication
evidence carry null; request time is never synthesized as freshness. Switching
between equivalent legacy and ledger sources preserves the established public
matchup freshness timestamp byte-for-byte where the compatibility contract
requires it. Source-specific publication timestamps and lineage remain in the
additive provenance envelope.
### Historical Matchup participants

In historical mode `players` is not a Player Pool. It is populated from the
complete canonical player-log rows for that exact game, one row per player who
appeared, and carries no inferred DNP or inactive roster. Each participant has
the same shape as a stored pool player, with these differences:

- `player_source` is `game_logs`, `posted_markets` is `[]`, and `provenance` is
  `{}`. There is no posted-market claim, and both
  `game.away_team.targetable_player_count` and
  `game.home_team.targetable_player_count` are `0`.
- `team_id` and `tricode` are the identity recorded for that game. A later
  trade or a newer Athlete Catalog team cannot override it, so
  `players[]` filtered by `team_id` deterministically renders the offense
  opposing a selected Defense Sheet.
- `focal_game_line` is that participant's actual line in the focal game:
  `{ game_id, game_date, matchup, minutes, stats }`, where `stats` covers the
  governed Stat Categories. It is display-only.
- crf04/statsplus#47 supersedes the earlier focal-free scoring rule from #42.
  Display and score inputs now read the participant's completed-season
  summary once, together: `season_scoring`, `last_10_minutes`, and Matchup
  Score inputs all come from the same read and all include the focal game.
  Hindsight is disclosed by the `completed_season` label, not by excluding the
  focal game from an input. The selection route's comparison samples and delta
  baseline are unaffected by #47 and keep dropping the focal row, so a
  participant's result never grades itself there.
- A historical Matchup Score input is the same completed-season evidence the
  page already displays:
  - **Team defense.** Scores read the same season Defense Sheet window
    `league` and `teams` display — the newest stored window honoring the #41
    completed-season exemption. When that window is unavailable or missing,
    the window reports `team_defense:<base>` in `missing_inputs` and computes
    no component; `league.surface_availability` is unchanged.
  - **Last 15.** There is still no point-in-time Last-15 snapshot for a
    completed season, so a historical Last-15 score reports
    `team_defense:<base>` in `missing_inputs` and computes no component,
    exactly as before #47.
  - **Player Diet.** `player_diet_facts` is the one completed-season aggregate
    per `(season, player, base, slice)` the `diet_shares` field already
    displays, and it is now consumed as a Matchup Score input too, exactly as
    a current-mode pool player's Diet would be. Genuinely absent Diet evidence
    still reports `player_diet:<base>` in `missing_inputs` and contributes no
    component.
  - A historical participant whose completed-season evidence is complete gets
    a complete Blend for a governed Stat Category, exactly as a current-mode
    pool player with the same evidence would.
- Injury evidence never removes or badges a participant: a canonical row means
  the player appeared.
- Participants with unavailable scores stay in the response, sort after
  complete scores under the unchanged ordering, and name their gaps through
  `missing_inputs`. A historical Blend is withheld whenever `missing_inputs`
  is nonempty, so no historical participant is ever shown a partial blended
  score.

If the game's canonical log synchronization is not complete, `players` is `[]`
and only the Participants section is unavailable; every other section,
including an available Season Defense Sheet, still returns.

Pool freshness and per-provider status are passed through from the selected
stored snapshot. Under the projection archive gate, `freshness.player_pool`
also carries `state: live | closing | missing` and `observed_at`. For a started
or final game, `closing` is historical evidence and does not expire under the
live freshness windows. A request for a player outside that closing pool is
`404 resource_not_found`; a scheduled game without a usable stored pool remains
`503 provider_unavailable`.
When the projection-archive reader is activated, this block additionally
contains `state: live | closing | missing` and `observed_at`; the Matchup
`game` header does not duplicate the Slate-only `projection_state` block.
The stats-table surface is `stale` when its last successful publication
predates the newest completed, non-postponed stored game; it is `missing` when
no successful publication exists. Team facts for a started or past game are
bounded by its Eastern Slate Date; a future tip requests the latest current
stored team scopes without passing a future cutoff.

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
  "experience": {
    "mode": "current",
    "player_source": "player_pool",
    "focal_game": null,
    "samples": {"context": "season_to_date", "excludes_focal_game": false},
    "baseline": {"context": "season_to_date", "hindsight": false}
  },
  "freshness": {
    "player_pool": {
      "status": "fresh",
      "state": "live",
      "observed_at": "2026-01-05T18:00:00+00:00",
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

`experience` is the additive selection-side declaration and is always present.
In current mode it is exactly the block above. In historical mode — a
completed, non-postponed Regular Season game whose governed Player Pool names
nobody — it becomes:

```json
{
  "mode": "historical",
  "player_source": "game_logs",
  "focal_game": {
    "game_id": "0022501082",
    "game_date": "2026-03-29",
    "matchup": "LAC @ MIL",
    "minutes": 34.5,
    "stats": {"PTS": 24.0, "REB": 5.0, "AST": 7.0}
  },
  "samples": {"context": "pregame", "excludes_focal_game": true},
  "baseline": {"context": "completed_season", "hindsight": true}
}
```

In historical mode a canonical participant of the focal game is selectable with
no Player Pool membership at all, and `404 resource_not_found` means only that
the player has no canonical row in a game whose logs are complete. Selection
reads the same completeness evidence the Matchup route does: while the focal
game's canonical log synchronization is absent or not complete, it fails closed
with `503 provider_unavailable` rather than resolving a participant from rows
that may still be partial, and rather than claiming a player did not appear in
a game nobody has finished collecting. The requested Market Categories
come from the governed Statistic Catalog rather than a stored pool. `h2h` and
`archetype` rows are restricted to games strictly before the focal game's date,
so the focal game never appears in either table and its result cannot leak into
a pregame sample; `focal_game` presents that line separately. Each row's delta
baseline is the sampled player's stored Regular Season rate with the focal game
excluded, and `baseline.hindsight` marks it completed-season evidence rather
than pregame evidence. When no stored Player Pool exists at all,
`freshness.player_pool` is the truthful
`{"status": "unavailable", "state": "missing", "observed_at": null,
"retrieved_at": null, "providers": {}}` document instead of a `503`.

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
| Historical Matchup selection for a player with no canonical row in a game whose logs are complete | `404 resource_not_found` |
| Historical Matchup selection while the focal game's canonical log synchronization is absent or not complete | `503 provider_unavailable` |
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

The request-time game-log source is database-only. A complete, valid durable
`player_game_logs` publication supplies the response; a season without one
returns the normal successful empty result and never calls NBA Stats or PBP
Stats. Historical seasons are not a supported Log Workspace outcome and may
therefore return empty. The HTTP parameters, success payload, filter
vocabulary, authentication, and error schema are unchanged. Player game logs
do not pass through Redis.

The durable implementation resolves the active immutable publication
and queries its publication-keyed player projection; a cold request does not
load or decode the season-wide publication payload. This is an internal read
optimization and does not change the endpoint parameters or response schema.
The stored rows retain the whole-minute presentation and composite/fantasy
averages established by durable ingestion.

Play-type matchup rating (#37): every `game_logs` row carries `PLAYTYPE_RTG`,
the play-type matchup between the player's Season Synergy Diet Share and that
game's opponent's Season play-type window, rounded to one decimal place. The
scale is centered on the league: `100` is a league-average matchup, above `100`
is a favorable one, and below `100` an unfavorable one. It uses the same
definition as the Matchup page's play-types `PTS` score, so
`PLAYTYPE_RTG = 100 x (1 + m)` for that score `m`. A row whose player or
opponent has no usable play-type facts carries `null`; the read-only demo
database has none, so every row carries `null` there. `playstyle_RTG_min` /
`playstyle_RTG_max` filter on this column, and a non-default range excludes
`null` rows rather than treating them as neutral.

Explicit contract amendment (#66): plus/minus is removed from the game-log
contract. `PLUS_MINUS` is no longer a supported `self_filters[STAT]`, the
averages no longer include a `PLUS_MINUS` cell, and response rows carry no
`+/-` value. This is a deliberate simplification of the previously pinned
contract so the durable PBP per-game path (whose upstream boxscore seam exposes
no plus/minus) is database-first without fabricating evidence.

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
| `player_name` | Yes | Player name; resolved against the requested `season_filter`'s Athlete Catalog first (exact case-insensitive match, then fuzzy), preferring the namesake active for that season when multiple catalog rows share a display name, falling back to the legacy `player_information` table when no catalog exists for that season |
| `minutes_filter` | No | Comma-separated min,max minutes. Default `0,48` |
| `players_on[]` | No | Teammates that must have a game-log appearance in the same game for the same team; this is game-level played/didn't-play evidence, not lineup-stint evidence |
| `players_off[]` | No | Teammates that must have no game-log appearance in the same game for the same team; multiple names exclude the union of their appearances |
| `date_filter` | No | `YYYY-MM-DD` start date that trims the player's own game logs. It never reshapes Team Filter rankings, which are always whole-Regular-Season |
| `teams_against[]` | No | Opponent filter names such as `OPP_PTS`. Every filter ranks opponents from the durable Season publications for the requested `season_filter` (#198); a request-time provider call is no longer made for any combination of filters. A season with no Season publication ranks no opponents, so the filter resolves to an empty result rather than borrowing another season's rankings |
| `rank_filter[]` | No | Rank for each opponent filter; positive means top defenses, negative means weakest |
| `location_filter` | No | `Home`, `Away`, or `Both`. Default `Both` |
| `game_filter` | No | Last N games |
| `season_filter` | No | Canonical NBA season in `YYYY-YY` form, with `YY` equal to the following calendar year's final two digits (for example, `2024-25`). Whitespace is trimmed. Default is the current season |
| `playstyle_RTG_min` | No | Finite numeric lower bound on `PLAYTYPE_RTG`. Default `0` |
| `playstyle_RTG_max` | No | Finite numeric upper bound on `PLAYTYPE_RTG`. Default `200` |
| `self_filters[STAT]` | No | Ordered inclusive stat range as `min,max` (normalized to a typed `between` filter); repeat the parameter to combine multiple constraints for one stat. Supported stats include `MIN`, `PTS`, `REB`, `AST`, `FGM`, `FGA`, `FG_PCT`, `FG3M`, `FG3A`, `FTM`, `FTA`, `OREB`, `DREB`, `TOV`, `STL`, `BLK`, `PF`, `PRA`, `PA`, `PR`, `RA`, `STKS`, and `FD_PTS`. `PLUS_MINUS` is not supported per the #66 contract amendment |

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
- `date`: accepted and ignored.  Rankings are always whole-season.

Example:

```bash
curl "http://localhost:5000/api/teams/stats?team=Los%20Angeles%20Lakers&category=Traditional"
```

Every category is served from the durable Season team matchup publications
(`traditional_opponent_season`, `synergy_play_types_opponent_season`,
`assist_locations_season`, `exact_shot_zones_opponent_season`, and
`grouped_shot_types_opponent_season`).  There is no request-time NBA Stats
call and no legacy ranking table read, so a picked date cannot reshape the
answer: it is accepted for compatibility with the panel and ignored.

Values are per-48 on nominal minutes, the same unit the Matchups Defense
Sheet reports, so "opponent points allowed" is the same number on both
surfaces.  Each column carries a `_RANK` computed over all thirty published
rows -- ascending, so rank 1 allows the fewest, and tied teams share a rank --
and, except for `Playtypes` and `Assists`, a `_vs_avg_pct` of
`(value / league average - 1) * 100`.  `Playtypes` and `Assists` carry their
values as a ratio to the league average instead, because the panel's charts
are centred on `1.0`.

`Traditional` derives `OPP_STL+BLK`, `OPP_FG_PCT`, and `OPP_FG3_PCT` from the
published counts; `Assists` derives `AssistPoints` as
`2 x TwoPtAssists + 3 x ThreePtAssists`; `Shooting Type` returns one object
per shot type with a derived `PTS` of `2 x FG2M + 3 x FG3M`.  `OPP_OREB` and
`OPP_DREB` are absent, because the publications carry no rebound split; the
panel renders them as `N/A`.  A rate a team has no denominator for -- a play
type it faced zero possessions of -- is absent the same way, for that team
only: it is neither ranked as the stingiest defense nor counted in the league
average the other teams are measured against.

An unknown `category` is `400 invalid_input`.  A season with no published
generation -- including every request against the read-only demo database,
which carries no publication tables -- serves nothing, which the route
reports as `404 resource_not_found`.  A stale publication still serves its
last-good values.

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

The deployment-owned `scripts/nightly_refresh.py --hosted-only` command is not
an HTTP endpoint. It refreshes only durable current-season player game logs,
retrying that PBP-backed unit once. It reads the existing governed catalogs
from Postgres and makes zero NBA Stats calls; NBA-owned surfaces remain the
residential collector's responsibility. The player-log step uses the PBP-based
incremental ingestion: it discovers governed completed `Regular Season` and
`Playoffs` games and requests one PBP per-game player observation per missing
game, plus a bounded recent-game reconciliation window
(`PLAYER_GAME_LOG_RECONCILIATION_DAYS`, default three days) that atomically
replaces a game's rows when upstream stat corrections change its checksum.
Each game must cover both exact teams with at least
`PLAYER_GAME_LOG_MIN_ACTIVE_PLAYERS_PER_TEAM_GAME` positive-minute players;
every target game is fetched, normalized, and validated before anything is
written, and a single transaction then replaces the staged games' rows and
advances season publication. An unchanged game is idempotently skipped. A
malformed, incomplete, identity-removing (including any unjoined athlete or
contradictory team), or provider-failed game fails the whole refresh, so
prior fact rows and the last complete publication are preserved exactly.
The season publication and the configured current season's
`player_game_logs` stats freshness advance only inside that same final
transaction, so a failed or incomplete refresh never stamps a partial union
fresh or leaks a staged correction; historical
backfills retain independent season freshness and never replace or gate the
current observation. Every result requires a present, fresh, nonempty Event
Catalog whose freshness count agrees with its actual season rows; a nonempty
result also requires a present, fresh, nonempty Athlete Catalog.
The season sidecar carries an explicit `complete`/`in_progress`
`publication_status`; legacy NBA-derived publications remain `in_progress`
until real PBP backfill verifies them, and a season serves database-first
game-log reads only when its publication is complete and valid.
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

### Canonical Game Ledger rehearsal (#86)

The #86 ledger is an inactive scheduled/materialization seam, not an HTTP
surface. `LedgerBackfillService` reads only final, non-postponed Regular
Season Event Catalog games through an explicit cutoff and fetches PBP
`/get-game-stats` observations newest first with bounded concurrency. Missing
games are fetched before daily rechecks (through day seven); weekly rechecks
continue through day 30; older games require an explicit historical repair.
The service stores resumable cursor/completed/failed progress and reports an
incomplete season as unavailable rather than exposing a partial publication.

Each accepted observation is one atomic Canonical Game Ledger unit: one game
identity, two team fact sets, and exactly the FullGame players named by the
governed participant evidence, including zero-minute participants.
Repeated normalized checksums are idempotent. A correction with the same
canonical game identity replaces the complete game and all dependent facts in
one transaction. Invalid counts, missing active participants, contradictory
phase/team identity, or unresolved athletes fail closed and retain the prior
valid game.

Traditional opponent, assist-location, and player per-36 facts are derived
from ledger count primitives. Traded players aggregate counts by canonical
identity while retaining team-at-game evidence; provider percentages are never
summed. Season and exact Regular Season L15 team windows require League
Complete evidence for all 30 teams, normalize team count totals to per-48 from
the nominal game-length denominator, use population sigma and deterministic
competition ranks, and stay unavailable before the L15 15-game floor. These
streams persist complete derived payloads and inactive candidate publications
with normalized provenance; they do not activate a public Matchups reader or
change frontend behavior. Corrections atomically enqueue affected slices.
PBP-versus-legacy symmetric identity and semantic differences are retained as
adjudication evidence, and zero or unequal comparison sets cannot claim exact
parity.
PBP retrievals are accepted into immutable collection observations before a
ledger replacement; inactive publication provenance must resolve exactly to
those accepted rows and to one authorizing canonical-ledger manifest/cutoff.
Collection stops at that manifest's `collect_before`; later composition of
evidence accepted before the deadline remains allowed for repair and
rehearsal. Player game logs Season, traditional opponent Season and
L15, assist locations Season and L15, and player per-36 each have independent
candidate payloads. A missing assist primitive cannot suppress the other
streams. Difference-free traditional/assist/per-36 parity artifacts become
`exact` automatically. Every difference artifact remains
`pending_adjudication` until an audited operator decision and prevents
activation while pending. Hard blockers may be audited as rejected but can
never be approved.
An invalid staged PBP response creates no accepted observation, ledger row, or
composition job. Successful initial games and corrections atomically enqueue
every governed slice at the active manifest cutoff. Traditional-opponent,
assist-location, and per-36 activation require an exact or operator-approved
parity artifact;
approval or rejection records actor, reason, time, and an audit event.
Parity-gated activation identifies the exact artifact, season, cutoff, and
candidate publication. The JSON body uses `artifact_id`,
`candidate_publication_id`, `season`, `cutoff`, and `reason`; the artifact's
bound publication ID and payload checksum must match that candidate. A
rejected artifact always blocks, including when its
raw comparison was exact; another season or cutoff is never reused. Unrelated
ledger streams do not require parity adjudication.

### Database-first Matchups activation (#87)

The first activation is additive. `POST
/api/admin/collection/streams/<stream_key>/activate` records an operator
reason and enables only that stream; `POST
/api/admin/collection/streams/<stream_key>/rollback` advances the same fenced
pointer to its immediately prior Publication. A stale composition or legacy
writer cannot overwrite a newer pointer. Activation evidence is retained in
the `publication_activations` table and never contains raw observations.

The authenticated Matchup and Matchup Selection routes read the durable
Regular Season catalog, Player Pool, game-log, Diet, injury, and team-window
seams. Activated statistical streams decode their immutable PublicationVersion
payloads independently for player game logs, per-36, each Player Diet Base,
and each Season/L15 team-window surface; an inactive stream alone permits its
legacy fallback. These reads make zero request-time NBA Stats, PBP, or DFS
calls. Injury
Reports retain their existing live/snapshot contract; statistical activation
does not change injury behavior. Existing fields remain
backward compatible. Additive `provenance` stream entries identify the exact
Publication ID, UTC Coverage Cutoff, age, source, and `fresh`/`stale` state for
player and diet streams. Team-window entries expose the selected stored
source's truthful availability, freshness, and retrieval metadata in the public
Matchups response; inactive streams explicitly report `legacy_database`
fallback with null publication, cutoff, age, and retrieval fields. Active or
stale publications report their exact Publication ID, authority, cutoff, age,
and stored creation timestamp. Consumers comparing legacy and ledger facts
should ignore this additive provenance envelope rather than treating
source-specific metadata as a semantic fact. Additive
`coverage.mixed_cutoff` and `coverage.mixed_freshness` flags remain independent
when one contributor is older or unavailable. A stale active Publication is
served as the last good fact with its real age; a failed partial attempt never
replaces it. Synergy L15 is always `unavailable/provider_window_unsupported`
and Playoff/Play-In requests are outside this first Regular Season activation.

The isolated `scripts/database_first_rehearsal.py` command requires a concrete
non-production environment, raw-facts collection/composition command,
completed-season Synergy candidate/facts command, and exactly seven ordered
dates; it derives parity from isolated publications and writes a validation
report without changing production pointers. Operator evidence requires an
explicit separate production snapshot database. `scripts/database_first_drills.py`
records deterministic outage, duplicate delivery, Outbox replay, expired
credential, provider failure, alert recovery, and restore/replay checks using
temporary control-plane state; SQLite restore is explicitly a local unit
adapter. Every URL-backed drill first opens a read-only target preflight and
requires an out-of-band `statsplus_disposable_control` marker nonce; the
`isolated` flag is not isolation evidence. A production gate additionally
requires a dedicated Postgres schema, expected IDs/checksums, direct restored
database verification, and explicit replay/repair evidence. The
PBP repair expectation names only its known preconditions (`season`,
`manifest_id`, `game_id`, and the expected ledger `checksum`). The drill
captures restored observation and composition-job identities before invoking
the repair, then reports only newly created IDs after binding the accepted PBP
ledger observation and every invalidated derived stream to that manifest's
cutoff. The observation must independently satisfy the runtime acceptance
contract for environment, canonical scope, authorized schema version, and
collection deadline; a pre-existing or merely inserted row cannot satisfy the
drill. The
marker is provisioned out-of-band (for example, a row in
`statsplus_disposable_control(marker_nonce, purpose, schema_name)` with
`purpose = 'database_first_drill'`) and the preflight rejects any existing
domain rows before migration. The
`scripts/benchmark_matchups.py` command requires a production-like fixture,
game identity, and an explicitly disposable database. It loads and validates
the fixture, invokes the complete legacy and activated MatchupService paths,
retains measured p95 latency and bounded indexed publication/ledger query
plans, and fails without plan evidence, instrumented zero provider calls, a
sub-second p95, and a
database-first p95 no greater than 110% of baseline. These artifacts claim no
formal recovery SLA.
Internal season rates default to Regular Season only unless a caller explicitly
requests Playoffs or all phases. Last-ten minutes and H2H rows include both
stored phases in deterministic chronology. The batch query seam returns
Regular Season rates and oldest-to-newest combined-phase last tens for multiple
canonical player IDs with one player-log rows query.

The internal `PlayerDietService` is not an HTTP endpoint. Its
`refresh(season)` operation joins only through the fresh Season Athlete
Catalog and publishes raw shares plus raw volumes for `play_types`,
`shot_zones`, `shot_types`, and `assist_locations`. Its bulk read,
`get_for_players(season, player_ids)`, returns stored facts without display
thresholds, together with the latest per-Base `available | unavailable |
missing` observations and timezone-aware retrieval times. Player Diets are
Season-only: they have no Last-15 values and no traditional Base. Degraded
Bases remain explicit and never synthesize zero facts; request-time reads call
no provider. Invalid provider domains or duplicate fact identities are
`unavailable/provider_invalid_response` for only the affected Base. Because
shot zones carry no `GP`, a valid zone response becomes
`unavailable/missing_games_played_evidence` when the shot-type Base supplying
that evidence is unavailable.

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
events, `recent_player_pool_events`, and `recent_injury_events`. Player Pool entries contain only the
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

The injury event stream and its `injury_events_total` /
`injury_buffered_events` metrics contain only `unmatched_entry_count`,
`unresolved_team_entry_count`, and `board_conflict_count`. The RotoWire provider invocation itself remains in the
ordinary provider event stream as provider `rotowire`, operation
`get_injuries`; raw report rows, names, IDs, URLs, statuses, and reasons never
enter telemetry.

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
  `DFS_ENABLED_PROVIDERS` and the `providers` query filter accept exactly the
  names `app.providers.registry` admits; the disabled-provider list a board
  reports is the registered set minus the enabled one. See
  [ARCHITECTURE.md](ARCHITECTURE.md) for the registry and its admission rules.
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

## Collection control-plane endpoints

The control plane is additive and does not alter existing public readers.
Collector routes use a machine-secret exchange for a short-lived signed bearer
token; tokens are bound to the deployment environment, audience, operation,
collector owner, provider set, and explicit surface set. `poll`, `ingest`, and
`catalog_publish` are operation capabilities only; they never authorize an
unrelated provider or surface. Discovery, manifest reads, bootstrap status,
catalog publication, and observation ingestion all re-check the persisted
identity binding.
Operator mutations require Firebase administrator authentication.
An invalid machine secret or bearer token is `401 invalid_token`; malformed
token input such as a non-integer TTL is `400 invalid_input`; a valid token
without the required collector scope is `403 forbidden`. Durable collection
state conflicts (stale publication/lease fences, immutable cycles, duplicate
idempotency keys, or non-retryable jobs) are `409 operation_conflict`.

```http
POST /api/collector/token
POST /api/collector/status
POST /api/collector/rehearsal-evidence
POST /api/collector/rehearsal-manifest
GET /api/collector/discovery
GET /api/collector/bootstrap
GET /api/collector/bootstrap/<request_id>
GET /api/collector/bootstrap/<request_id>/status
POST /api/collector/catalog/<request_id>
GET /api/collector/manifest/<manifest_id>
POST /api/collector/observations
POST /api/collector/credential-deliveries/<delivery_id>/claim
POST /api/admin/collection/seasons/<season>
POST /api/admin/collection/streams/<stream_key>/rollback
POST /api/admin/collection/streams/<stream_key>/activate
POST /api/admin/collection/compositions/<job_id>/retry
POST /api/admin/collection/cycles/start
POST /api/admin/collection/repair
POST /api/admin/collection/cycles/<cycle_id>/finish
POST /api/admin/collection/cycles/<cycle_id>/not-applicable
POST /api/admin/collection/bootstrap
POST /api/admin/collection/collectors/<identity_id>/revoke
POST /api/admin/collection/collectors/<identity_id>/rotate
GET /api/admin/collection/credential-deliveries/<delivery_id>
GET /api/admin/collection/reconciliation
GET /api/admin/collection/diagnostics
POST /api/admin/collection/reconciliation/<item_id>/resolve
```

`POST /api/collector/observations` accepts one complete normalized envelope
and payload as a gzip-compressed JSON document (`Content-Encoding: gzip`). The
server validates exact envelope fields, registered provider ownership, accepted
schema/window, manifest, environment, scope, checksum, finite/non-negative
payload values, size, and `collect_before` deadline before one atomic insert.
Repeating the same
collector/client observation ID and checksum returns the original receipt;
reusing the ID with a different checksum is rejected. Operator actions return
bounded durable identifiers and require a human-readable reason where they
mutate publication state. Raw observations and player-level
payloads are never returned by these routes. Collector limits return `429
rate_limited`, a bounded `retry_after_seconds`, and a `Retry-After` header.
The token exchange may request a subset of the identity's `providers` and
`surfaces` in addition to its operation `scopes`; omitting them uses the
identity's persisted binding, while an empty or unauthorized set is rejected.
`GET /api/collector/discovery` (also available as `GET /api/collector/bootstrap`)
is the machine-authenticated, bounded polling seam: it returns pending bootstrap
requests and active manifests authorized for the caller's owner/provider/surface
binding, in deterministic newest-first order.
Each returned manifest also contains additive `scope_descriptors`. Every
descriptor is bound to one authorized frozen scope and fixes its subject,
category, all-30-team opponent identity, Season/exact-L15 window, and
cutoff-derived `date_to`; the collector does not invent those parameters.
Bootstrap status is a bounded response containing request state, season,
catalog type, cutoff, expiry, and version; it never returns catalog payload
facts. A collector with the bootstrap/catalog scope publishes one catalog using
the same gzip-compressed Observation Envelope contract at
`POST /api/collector/catalog/<request_id>`. The envelope carries the request's
catalog observation type, provider/environment, scope, season/cutoff, schema,
retrieval time, client observation ID, and checksum. The accepted observation
and governed catalog publication commit together; repeating the same ID and
checksum returns the original publication, while expired or already-completed
requests are rejected. Railway then creates the
immutable cutoff manifest only after both Event and Athlete Catalog
publications pass their governed freshness checks. Event Catalog rows must have
unique canonical game IDs, exactly two canonical teams, Regular Season phase,
recognized status, and a scheduled date. Completeness is exact equality with
the governed Active Season/Event Catalog schedule at the cutoff, not a mutable
environment floor; Playoffs, Play-In, mixed-phase, partial, and empty catalogs
remain `complete: false` and cannot authorize a manifest or no-game cycle.
Athlete Catalog rows must have unique canonical identities, team and
season-coverage evidence, and exactly cover the active governed roster plus
identities derived from accepted Event/Railway evidence. Caller-supplied
identity lists and `completed_game_count` values do not establish completeness.
Catalog publication also performs keyed reconciliation into the governed
EventCatalog/AthleteCatalog tables in the same transaction as the publication.
Additions and corrections are accepted into the next complete snapshot;
omitted rows are never destructively removed unless the payload explicitly
sets `complete_snapshot: true` and names matching `tombstones`. An incomplete
attempt is retained as `complete: false` and leaves governed rows, manifests,
and cycles unchanged. A changed event identity or completed-game set
supersedes affected active manifests/cycles rather than mutating their frozen
cutoff facts. Manifest selection orders only complete publications and skips
newer incomplete attempts; Athlete Catalog selection additionally requires all
Event-derived identities within its seven-day freshness window.

Observation ingestion uses a database-backed per-collector lease. PostgreSQL
acquires the identity row with `SELECT ... FOR UPDATE`; the short lease expires
after a bounded interval so a crashed Railway worker can be recovered. A live
lease returns `429 rate_limited` with an explicit `retry_after_seconds` and does
not rely on a process-local semaphore for correctness. The acquired owner and
monotonic fence are checked under the same row lock immediately before the
observation and composition enqueue commit; a worker taken over after expiry
fails closed with `stale_lease`. Collector usage counters reset the existing
locked row in place after 24 hours and retain the same row-lock discipline.

Every completed publication has normalized `publication_observations` rows
pointing to the exact accepted Observation IDs used for completeness. Retention
joins those references through active, previous, and rollback pointers; it
never searches arbitrary rendered payload JSON. History pruning removes old
rendered facts while retaining compact immutable provenance and audit metadata.
Unresolved identity rejection atomically appends a bounded, deduplicated
Reconciliation Item before returning `identity_unresolved`.

Lifecycle audit events are append-only and contain only bounded safe fields.
Successful token issuance/use, rotation, revocation, and same-ID/different-
checksum observation rejection are recorded. Maintenance emits one first-failure
alert, one stale-threshold alert, a six-hour `cycle_attention` alert, and one
`recovery` alert when state clears; queued/running work suppresses failure and
stale false positives.

`POST /api/collector/status` accepts the authoritative `release_version` and
`release_checksum` plus an optional closed `state`/stable `reason` pair. The version is 1-64 characters from the bounded release
identifier vocabulary (`A-Z`, `a-z`, digits, `.`, `_`, `+`, `-`), and the
checksum is exactly 64 hexadecimal characters. The authenticated identity's
`last_seen_at` and release evidence are persisted. Lifecycle reports append an
immutable transition, including an explicit recovery after retry/failure;
payloads, secrets, player data, and arbitrary status fields are rejected.
`POST /api/collector/rehearsal-evidence` is machine-authenticated and requires
the complete poll/ingest/catalog capability set. Its short-lived response binds
identity, environment, endpoint, audience, release version/checksum,
season/cutoff, contract version, operations, and issuance/expiry. Promotion
obtains this evidence directly; caller-authored evidence files are not trusted.
Railway first issues a ten-minute `rehearsal_validation` manifest in a
non-production environment. The collector submits one sanitized compressed
Observation Envelope twice. Normal observation persistence and the unique
collector/client ID constraint produce the durable receipt and replay receipt;
the validation scope is explicitly excluded from publication composition.
Evidence operations are derived from the persisted manifest audit, status
transition, and observation receipt rather than asserted by the caller.

`GET /api/admin/collection/diagnostics` returns bounded arrays (at most 50
rows per category). Its additive stream, collector, and usage rows have this
exact shape; projection collection diagnostics are also bounded and contain no
raw payloads or source identifiers. Absent evidence is JSON `null`:

```json
{
  "streams": [{
    "stream_key": "synergy_play_types",
    "provider": "nba",
    "owner": "residential_collector",
    "enabled": true,
    "available": true,
    "activation_status": "active",
    "freshness_rule": "cutoff_current",
    "publication_id": "publication-id",
    "coverage_cutoff": "2026-08-12T00:00:00+00:00",
    "fence": 4,
    "freshness_status": "fresh",
    "age_seconds": 30
  }],
  "collectors": [{
    "identity_id": "collector-id",
    "environment": "production",
    "revoked": false,
    "last_seen_at": "2026-08-12T00:00:00+00:00",
    "release_version": "collector-1.2.3",
    "release_checksum": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }],
  "usage": [{
    "collector_id": "collector-id",
    "poll_count": 2,
    "envelope_count": 1,
    "byte_count": 1024,
    "concurrency_count": 0,
    "limits": {
      "poll_count": 100,
      "envelope_count": 1000,
      "byte_count": 52428800,
      "concurrency_count": 1
    },
    "window_started_at": "2026-08-12T00:00:00+00:00",
    "window_resets_at": "2026-08-13T00:00:00+00:00",
    "retry_after_seconds": 3600,
    "concurrency_retry_after_seconds": 0
  }],
  "projections": {
    "providers": [{
      "provider": "dabble",
      "last_poll_at": "2026-08-12T00:00:00+00:00",
      "last_changed_snapshot_at": "2026-08-12T00:00:00+00:00",
      "freshness_seconds": 30,
      "failure": {"last_at": null, "reason": null, "consecutive": 0},
      "backoff": {"active": false, "until": null},
      "active_count": 18,
      "unresolved_count": 0
    }],
    "active_count": 18,
    "unresolved_count": 0,
    "lease": {"active": false, "fence": 4, "expires_at": null}
  }
}
```

Projection collection is not an HTTP refresh operation. The dedicated Railway
service constructs settings, dependencies, provider executors, and Redis/cache
clients once, then wakes the same coordinator every five minutes. The
coordinator applies the configured 30-minute/5-minute adaptive cadence,
governed event-status cutoff, database-time lease/renewal and provider-state
timestamps, provider backoff, and archive persistence path. The one-shot
`scripts/collect_projections.py` command uses the same run-once path and exits
nonzero when every due provider fails at board collection. Stale cache fallback,
omitted outcomes, and collector defects are health failures rather than
successful polls. API and browser reads remain database-only.

`freshness_status` is closed to `fresh`, `stale`, `missing`, or
`unavailable`. Age is the non-negative bounded age of the active publication;
`cutoff_current`, `daily_recheck`, and `seven_day` use one-hour, 24-hour, and
seven-day thresholds respectively, with the threshold instant still fresh. A
missing active pointer is `missing`. Registry `never_schedule` streams are
`unavailable`, report `available: false`, and remain rejected by activation.
Usage retry timing is the remaining 24-hour counter window; concurrency retry
timing is the remaining database lease.

The rotation endpoint returns only a durable job/identity status; the new
long-lived machine secret is never returned by an admin GET. During the
explicit overlap window, the rotated machine presents its old secret over the
machine-authenticated `POST /api/collector/credential-deliveries/<delivery_id>/claim`
route (with a short-lived token carrying `credential` or `ingest`) and receives
the replacement once. The delivery is encrypted at rest, expires, and is
invalidated atomically on retrieval. `GET
/api/admin/collection/credential-deliveries/<delivery_id>` returns metadata only.

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

### Saved Filter Sets

A Saved Filter Set is an account-private bookmark of a Log Workspace URL: a
user-chosen name plus the bare query string that addresses the Filter Set. No
game-log data is stored. Every route requires Firebase auth and operates only
on the caller's own rows.

```http
GET    /api/user/saved-filter-sets
POST   /api/user/saved-filter-sets
PATCH  /api/user/saved-filter-sets/<id>
DELETE /api/user/saved-filter-sets/<id>
```

`GET` returns the caller's items newest-first:

```json
{
  "success": true,
  "saved_filter_sets": [
    {
      "id": 7,
      "name": "Jokic at home",
      "query_string": "player=Nikola+Jokic&location_filter=Home",
      "created_at": "2026-08-23T12:00:00+00:00",
      "updated_at": "2026-08-23T12:00:00+00:00"
    }
  ]
}
```

`POST {"name", "query_string"}` returns `201` with
`{"success": true, "saved_filter_set": {...}}`. `PATCH {"name"}` renames one
item and returns `200` with the same single-item envelope; the saved
`query_string` is immutable. `DELETE` returns
`{"success": true, "message": "Saved filter set deleted"}`.

Validation and conflicts:

- `name` is required and is 1–100 characters after trimming.
- `query_string` is required, at most 2048 characters, and must be a bare URL
  query string: no scheme, host, path, leading `?`, `#` fragment, or
  whitespace. Parameter names inside it are not validated; a query string that
  is no longer recognised surfaces the client's existing URL-entry error when
  it is opened.
- `400 invalid_input` for either validation failure.
- `404 resource_not_found` for an id that does not exist or belongs to another
  account. Foreign ids are never reported as `403`.
- `409 operation_conflict` for a duplicate name within the account (compared
  case-insensitively) and for exceeding the cap of 100 saved items per
  account.


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

Supported stats include `MIN`, `PTS`, `REB`, `AST`, `FGM`, `FGA`, `FG_PCT`, `FG3M`, `FG3A`, `FTM`, `FTA`, `OREB`, `DREB`, `TOV`, `STL`, `BLK`, `PF`, `PRA`, `PA`, `PR`, `RA`, `STKS`, and `FD_PTS`. `PLUS_MINUS` is not supported per the #66 contract amendment.

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
