# statsplus#107 post-deploy: production backend API (read-only GETs)

- Run: 2026-10-04 ~22:15-22:25Z against `https://statsplus-backend-production.up.railway.app` as the owner's production account
  (Firebase custom token minted from Railway `FIREBASE_SERVICE_ACCOUNT_JSON`, exchanged via signInWithCustomToken; secrets
  kept in /tmp/sp107-pd-secrets (0600), deleted afterwards; artifacts scanned for JWT/API key/refresh-token patterns: none).
- Deployed: backend master `58d0407556eabd54854fedd4fee83d1c0cf856c9`, Railway deployment `16d875cb-...` SUCCESS 22:11:10Z.
  `git diff 3572c14e 58d0407` is empty (identical tree `b62e981b`): 58d0407 is the squash merge of #337 onto master c1f40b54.
- Deploy proof (behavioural): every route below now returns a top-level `provenance` block; before #337 they did not.
- Not run: `POST /api/user/targets/preview` (no POSTs to production allowed).

## Requests (`tools/get.py LABEL PATH`; responses saved verbatim as `LABEL.json`)

| Label | Request | generation | sources |
|---|---|---|---|
| slate-2026-04-10 | `/api/games/slate?date=2026-04-10` (15 games) | `[]` | injuries unavailable, pool unavailable, schedule stale (retrieved 2026-09-09T09:00:11Z) |
| slate-2026-04-12 | same, 15 games | `[]` | same |
| slate-2026-10-04 | today, 0 games (offseason) | `[]` | same |
| matchup-0022501171 (+14 others on 04-10) | `/api/games/matchup?game_id=` | 15 streams, all stale | injuries, pool, schedule |
| unscheduled-cade-vs-cha | `/api/matchups/unscheduled?player_name=Cade Cunningham&opponent=CHA` | 15 streams | `{}` |
| game-logs-cade | no filters | 10 streams | `{}` |
| game-logs-cade-sheet-filter / -named-filter / -mcp-equivalent | `teams_against[]=sheet:play_types:PRBallHandler:PTS` / `OPP_PTS` / `sheet:play_types:OffScreen:PTS`, `rank_filter[]=1,10` | 10 streams | `{}` |
| targets-resolve-2026-04-10 | `/api/user/targets/resolve?date=2026-04-10` (11 Targets) | 15 streams (identities == DET@CHA Matchup's) | injuries, pool, schedule |

## Assertions (`tools/analyze.py` -> `analysis.txt`, `analysis.json`): 303 checks, 0 failures

Per response: status 200; block present; non-Matchup routes have exactly `generation`+`sources`; Matchup/Unscheduled keep all
17 legacy stream-keyed entries byte-equal (except freshness/age) beside `generation`/`sources`; generation sorted by
(stream_key, season); every entry has exactly the 17 `PublicationRead.to_dict` keys; freshness in {fresh, stale, unavailable};
`synergy:l15` and `synergy_play_types_opponent_l15` (provider_window_unsupported) omitted from generation but present as legacy
entries; sources keys per contract (slate/matchup/resolve: injuries,pool,schedule; unscheduled/game logs: {}); slate/matchup
sources equal the response's own `freshness.{schedule,pool,injuries}` {status, retrieved_at}; slate generation `[]`.

Freshness cross-check against backend source: every stream is `cutoff_current` (registry `_SURFACE_REGISTRY_RAW`), threshold
3600 s (`PUBLICATION_FRESHNESS_SECONDS`); every entry's age_seconds (23.0-42.7 days) > 3600 and freshness `stale`, and
age_seconds == request time - retrieved_at within the request's elapsed time + 5 s.

## Observation (not a failure)

Game logs list the five Season Defense Sheet streams with or without `teams_against` (10 streams either way). CONTRACT.md's
original line said opponent team-window streams appear "only when the query has teams_against filters"; the backend's
docs/API_DOCUMENTATION.md documents the implemented rule: always listed, because PLAYTYPE_RTG and Team Filters read the Season
window. Only the play-type Season window is strictly needed when unfiltered.
