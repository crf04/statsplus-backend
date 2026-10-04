# Remote connector (claude.ai "Statsplus MCP") post-deploy checks — 2026-10-04 ~22:15–22:25Z

Client: Claude Code session, claude.ai remote connector "Statsplus MCP" (real user OAuth; Streamable HTTP to the
deployed remote). Text content only is surfaced by this client (no MCP Apps structuredContent).
Deployed: backend 58d0407 (Railway deploy 16d875cb SUCCESS 22:13:16Z), MCP 98f8812 (deploy ac7378a5 SUCCESS).
Proof the connector hits new code: the "Data as of / Stale / Unavailable" footer exists only in MCP 98f8812 and
requires the backend `provenance` block (backend 58d0407).

| # | Call | Result | Footer |
|---|------|--------|--------|
| 1 | get_slate(date=2026-04-10) — 15-game Slate fan-out | OK | F1 + Stale(+schedule) + "Unavailable: injuries, player pool." |
| 2 | get_matchup(game=DAL @ SAS, date=2026-04-10, player=Khris Middleton) | OK | same as 1 |
| 3 | get_matchup(game=DAL @ SAS, date=2026-04-10) team view | OK | same as 1 |
| 4 | get_matchup(player=Tyrese Maxey, opponent=BOS) Unscheduled | OK | F1, Stale without schedule, no Unavailable line (sources {}) |
| 5 | query_game_logs(Khris Middleton, date=2026-04-10, vs=[row sheet:shot_zones:Mid-Range:FGA 1-10], FGA, last_n 5) row chain from #2 | OK, 5 games CLE/DEN/MIN/ORL | F1 minus per-36 rates; no sources lines |
| 6 | query_game_logs(Tyrese Maxey, season 2025-26, last_n 3) no filter | OK | same as 5 |
| 7 | get_targets(date=2026-04-10) | OK, 11 Targets 11 live | same as 1 |
| 8 | get_slate() today 2026-10-04, offseason, 0 games | OK | no generation → only "Stale: schedule (retrieved 25 days ago)…" + "Unavailable: injuries, player pool." |
| 9 | repeat #1 and #2 immediately (60 s/1 h cache window) | identical text incl. footer | identical |
| 10 | resources/list + read statsplus://vocabulary | "As of / stale" entry present | — |
| 11 | server instructions (initialize) | contain "Answers end with a 'Data as of' line; when it names a stale or unavailable group, say so in the same sentence…" | — |

F1 (verbatim):
Data as of the 2025-26 season's 2026-09-09 play-type Diets, shot-zone Diets and shot-type Diets, the 2025-26 season's 2026-09-06 shot-zone defense, the 2025-26 season's 2026-08-24 play-type defense and shot-type defense, and the 2025-26 season's 2026-08-21 game logs, per-36 rates, assist-location Diets, team defense and assist-location defense.
Stale: game logs, per-36 rates and assist-location defense (42 days old), play-type Diets, shot-zone Diets and shot-type Diets (25 days old), assist-location Diets (22 days old), team defense (30 days old), play-type defense (41 days old), shot-zone defense (27 days old), shot-type defense (40 days old), schedule (retrieved 25 days ago). State this in the same sentence as any fact drawn from them.
Unavailable: injuries, player pool.

Observations:
- Season named ("2025-26 season's") because today's season is 2026-27 — spec's past-season rule.
- Offseason: every daily stream stale — the spec's documented offseason-noise note, expected.
- Game logs list opponent-defense streams even without `vs`: backend game_service.get_filtered_logs captures the
  Season Defense Sheet for PLAYTYPE_RTG on every read (comment at the provenance_block call). Consistent with the spec's
  "streams the read used"; CONTRACT.md's "team-window streams only with teams_against" line is inaccurate wording.
- Unscheduled Matchup header (pre-existing #8 rendering) says "Season defense 2026-08-24"; the footer gives team defense
  2026-08-21 and shot-zone defense 2026-09-06 (per-stream coverage_cutoff). Different granularity, not contradictory facts.
- Cache hit vs miss is not externally distinguishable through this client; repeat equality is shown, lifetime/expiry is
  covered by the merged mutation-tested unit tests (not waited out live).
