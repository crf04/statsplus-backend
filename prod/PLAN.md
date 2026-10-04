# Production compatibility plan — statsplus#107 (read-only)

Environment: `--environment production` (local frontend 334f99b → deployed Railway backend; proxy rejects non-GET).
The #107 backend (additive top-level `provenance` on slate/matchup/unscheduled/game_logs/targets resolve+preview)
is NOT deployed, so this verifies that the deployed backend + unchanged frontend still render the affected
journeys. It is a separate verdict from isolated QA.

| Behavior | Plausible regression | Trigger | Observable outcome |
|---|---|---|---|
| Slate renders (`/api/games/slate`) | decoder rejects/ignores response shape | open `/matchups?date=2026-04-10` | `NOP @ BOS` heading, slate 200; desktop + phone proofs |
| Slate empty / invalid / recovery | error contract change | `2026-08-01`, `not-a-date`, refill date | empty text + 200; alert + 400; heading back + 200 |
| Matchup detail (`/api/games/matchup`) | top-level key collision with legacy stream-keyed `provenance` | click NOP/BOS row; reload | `BOS Defense Sheet`, matchup 200, PTS control effect at desktop and phone |
| Search / game logs (`/api/games/game_logs`) | decoder breaks on added key | shared link, filter edit, reload, refused link, browse, return | badges, `.sample-size-text` counts, 200s; desktop + phone proofs |
| Targets list (read-only) | list/backtest GETs fail | open `/targets` | GET `/api/user/targets` 200, page heading; desktop + phone proofs |

Untestable in production: POST `/api/user/targets/preview` (proxy rejects writes; Lab/samples need it),
`/api/user/targets/resolve` and `/api/matchups/unscheduled` (not called by this frontend's driven journeys).
Response bodies are not exported by the helper, so `provenance` presence is checked separately if a
read-only path exists, otherwise reported as a limit.

## Results (2026-10-04, frontend 334f99b, production Railway)
- matchups.jsonl: passed (41/41 steps); slate 200×3, 400×1 (invalid date), matchup 200×2.
- search.jsonl: passed; game_logs 200×7, players 200×3.
- prod-targets-readonly.jsonl: failed at step 4 (my guessed heading `Targets` does not exist; page heading is a paragraph). Journey defect, not app.
- prod-targets-readonly-v2.jsonl: passed but proofs captured while cards showed `Reading the season…`; superseded.
- prod-targets-readonly-v3.jsonl: passed; waits for first two cards' backtests; targets/backtests/resolve 200.
- provenance in response bodies: not inspectable (helper exports statuses only).
