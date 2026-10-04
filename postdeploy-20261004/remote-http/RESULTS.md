# statsplus#107 post-deploy: remote MCP over Streamable HTTP (authenticated)

- Deployed: MCP main `98f881255b19719e7cfc271021c918cc3849e2f1`, Railway deployment `ac7378a5-...` SUCCESS 22:11:43Z.
  `git diff edd873a 98f8812` is empty (identical tree `0d830c2e`; squash merge of PR #19).
- Endpoint: `https://statsplus-mcp-production.up.railway.app/mcp`, python `mcp` 2.3.0 `streamable_http_client`.

## Authentication (no browser)

The remote is its own OAuth 2.1 server. Used its supported path: dynamic client registration with an http loopback redirect
(`http://127.0.0.1:53682/callback`, the Claude Code path), `/authorize` with PKCE S256 + `resource`, then the same
`POST /login/callback {flow, id_token, refresh_token}` the `/login` page sends after Google sign-in, with a genuine Firebase
session for the owner's account (custom token from the backend's service account), then `/token`. state and RFC 9207 `iss`
verified. Side effects in the MCP's Postgres: one OAuth client (`ef92f68d...`) and one grant for the owner's uid; the server
advertises no revocation endpoint, so the grant remains until its 30-day idle expiry. Token/session files lived in
/tmp/sp107-pd-secrets (0600) and were deleted.

## Calls (each twice back-to-back, 3.6 s apart; `apps.json` with `io.modelcontextprotocol/ui`, `noapps.json` without)

| Tool | text chars | structured chars | backend block compared |
|---|---|---|---|
| get_slate(date=2026-04-10), 15 games | 5838 | 23245 (cap 150,000, `results.MAX_RESULT_CHARS`) | slate-2026-04-10 + 15 Matchups |
| get_matchup(game="DET @ CHA", date=2026-04-10) | 4310 | 28164 | matchup-0022501171 |
| get_matchup(player="Cade Cunningham", opponent="CHA") | 7841 | 33128 | unscheduled-cade-vs-cha |
| query_game_logs(Cade, vs=[{row:"sheet:play_types:OffScreen:PTS" (from the DET@CHA drivers), ranks:[1,10]}], date=2026-04-10) | 3293 | 11836 | game-logs-cade-mcp-equivalent |
| get_targets(date=2026-04-10) | 7913 | 22970 | targets-resolve-2026-04-10 |

## Assertions (`tools/analyze.py` -> `analysis.txt`): 64 checks, 0 failures

- Footer: every answer's tail is the footer block; its first line starts `Data as of`. Because every stream is stale and pool/
  injuries are unavailable, the footer has 2-3 lines, so the text's LAST line is `Stale: ...` or `Unavailable: injuries, player
  pool.`, not `Data as of` (spec: a single line only when all fresh). get_slate/get_matchup(game)/get_targets footer:
  1. `Data as of the 2025-26 season's 2026-09-09 play-type Diets, shot-zone Diets and shot-type Diets, the 2025-26 season's 2026-09-06 shot-zone defense, the 2025-26 season's 2026-08-24 play-type defense and shot-type defense, and the 2025-26 season's 2026-08-21 game logs, per-36 rates, assist-location Diets, team defense and assist-location defense.`
  2. `Stale: game logs, per-36 rates and assist-location defense (42 days old), play-type Diets, shot-zone Diets and shot-type Diets (25 days old), assist-location Diets (22 days old), team defense (30 days old), play-type defense (41 days old), shot-zone defense (27 days old), shot-type defense (40 days old), schedule (retrieved 25 days ago). State this in the same sentence as any fact drawn from them.`
  3. `Unavailable: injuries, player pool.`
  Unscheduled Matchup: lines 1-2 without the schedule clause, no line 3 (sources `{}`). Game logs: same minus "per-36 rates".
- Text footer == `structuredContent.data_as_of` for all five tools.
- `structuredContent.provenance` == backend block: get_slate byte-equal; others equal after removing age_seconds, whose offset
  is a single constant equal to the wall time between my backend GET and the MCP's read (e.g. 224/225 s vs 224 s for the Matchup),
  i.e. the same Publications, nothing else differs. Matchup blocks keep their legacy stream-keyed entries.
- get_slate: `matchup_generation` 15 entries / 15 distinct (stream_key, season, publication_id); per game
  `matchup_provenance` = {generation, sources} only; all 15 games' indices valid, unique, and resolve to exactly that game's
  backend Matchup identities; per-game sources == backend Matchup sources; union == the 15 backend identities.
- Repeat: footer and full text identical on every repeat. get_slate/get_matchup structuredContent identical; query_game_logs and
  get_targets (per-user, never cached) identical except age_seconds advancing by the 4-5 s gap.
- Non-Apps: no structuredContent; text identical to the Apps run.
- initialize `instructions` contains: "Answers end with a 'Data as of' line; when it names a stale or unavailable group, say so in
  the same sentence as any fact drawn from that group (e.g. 'these play-type shares are 9 days old')."
- Resources: 3 `ui://statsplus/*.html` cards + `statsplus://vocabulary`; vocabulary has the "As of / stale" entry
  (Unavailable = missing not old; sources stated with retrieval age; "freshness not reported").

## Cache hit evidence (`call-logs.json`, from Railway deploymentLogs structured attributes; hashed uid dropped)

- get_slate 2026-04-10: first built 22:14:10Z by an earlier caller (16 backend calls); all four of my calls `cache_hit=true`,
  0 backend calls, so the hit footer is rendered from provenance stored 6 minutes earlier and still matches.
- get_matchup DET @ CHA (past date): 22:20:16 miss (2 backend calls), then 3 hits.
- Unscheduled (today, 60 s TTL): 22:20:25 miss, :28 hit, 22:21:22 hit (57 s), 22:21:27 miss (62 s): 60 s lifetime observed.
- query_game_logs, get_targets: always `cache_hit=false` (by design).
- The 1-hour past-date expiry was not waited out.
