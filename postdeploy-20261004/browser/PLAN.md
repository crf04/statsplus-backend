# Post-deploy production browser plan — statsplus#107 (read-only)

Deployed backend: crf04/statsplus-backend#337 merge 58d0407556eabd54854fedd4fee83d1c0cf856c9,
Railway deployment 16d875cb-5518-4169-9690-95d16dc7e21b (SUCCESS 2026-10-04T22:13:16Z).
Change: additive top-level `provenance` ({generation, sources}) on slate, matchup, unscheduled,
game_logs, targets resolve, targets preview. Frontend unchanged (local checkout /Users/chrisfu/statsplus-107/frontend).
Risk: frontend decoders/rendering break or ignore-fail on the new key.

Environment: `--environment production` (local frontend -> Railway; local proxy rejects non-GET/HEAD/OPTIONS).
Helper: coordinator /Users/chrisfu/.t3/worktrees/statsplus/t3code-de11429f, `--backend ~/statsplus-backend`
(Railway-linked credential source only), `--port 5193`. No mutations; no QA-only journeys.

| Behavior | Plausible regression | Trigger | Observable outcome (fails on regression) |
|---|---|---|---|
| Slate renders (`GET /api/games/slate`) | decoder rejects new top-level key -> error state | open `/matchups?date=2026-04-10` | `NOP @ BOS` heading; slate 200; desktop proof |
| Slate Targets resolve (`GET /api/user/targets/resolve?date=`) | resolve decoder rejects -> "Targets unavailable" | same open; empty slate `2026-08-01` | resolve 200 + `.slate-targets:not(.is-unavailable)` visible (added steps) |
| Slate empty/invalid/recovery | error contract change | `2026-08-01`, `not-a-date`, refill | empty text + 200; alert + 400; heading + 200 |
| Matchup detail (`GET /api/games/matchup`) | collision of new top-level `provenance` with existing matchup provenance field | click NOP@BOS; PTS filter desktop+phone; reload | `BOS Defense Sheet`, matchup 200, PTS effect; phone proof |
| Search / game logs (`GET /api/games/game_logs`) | decoder breaks on added key | shared link, filter edit, reload, refused link, browse, return | badges, sample counts, game_logs 200; desktop + phone proofs |
| Targets list resolve (`GET /api/user/targets/resolve`, no date) | resolve decoder rejects -> "Today's activity unavailable" | open `/targets` | resolve 200 (added), `No Targets active today` visible, loading/unavailable text hidden; card backtests rendered; desktop + phone proofs |

Untestable in production browser:
- `POST /api/user/targets/preview` (Target Lab `/targets/:id`): proxy rejects non-GET by design; not worked around.
- `GET /api/matchups/unscheduled`: not called by the frontend (MCP-only consumer).
- `provenance` presence in bodies: helper exports statuses only, never bodies.
Search phone `assert-layout` is a known pre-existing overflow (stock search journey omits it).
