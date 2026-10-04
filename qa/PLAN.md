# QA re-verification plan — statsplus#107 round 3 (Boundary tier, isolated QA)

Revisions: coordination b97e927 (helper + journeys), QA backend /Users/chrisfu/statsplus-107/backend feat/107-provenance-block
3572c14e (clean), frontend 334f99b (unchanged, clean), MCP /Users/chrisfu/statsplus-107/mcp feat/107-as-of-footer de961e4 (clean;
`statsplus_mcp` imports from /mcp/src). Port 5194. Real Firebase auth (QA identity). Tools reused from qa-r2-20261004T205640/tools
(drive.py one command per result line; `_hook` lines run locally; mcp_call.py gains a non-Apps mode; repro serves per-game bodies).

Changed since r2: backend 964528b4+3572c14e (Matchup / Targets resolve / preview `generation` also list a retained prior-season
Publication a past game's Matchup read); MCP 54ff118/94c042c/de961e4 (get_slate: top-level `matchup_generation` deduped, per-game
`matchup_provenance` {generation indices, sources}; empty-branch footers; null-status source wording).

| Changed behavior | Plausible regression | Trigger | Observable outcome that fails on regression |
| --- | --- | --- | --- |
| Matchup service returns a third value (retained reads) and merges it into `generation` | historical participants/players lost; Matchup 500 | s1 journeys/matchups.jsonl: Slate → NOP @ BOS detail, controls, reload, phone, empty/invalid/recovered | Defense Sheet headings, OPP REB/Cut PTS toggles, GET /api/games/matchup 200; desktop+phone proofs |
| Slate unchanged backend-side | — | s1 | NOP @ BOS on Slate, GET slate 200 |
| Targets resolve/preview compose through new focal path | preview 500 / Lab or list broken | s3: /targets Sample → Save → Lab → list → phone → delete → empty | `Backtest summary`, `Backtest up to date.`, POST preview 200, backtests 200, DELETE 200 |
| Past-game Matchup `generation` lists retained 2025-26 Publication beside snapshot | missing retained entry / duplicate / unsorted | s3 API hooks r2 (0022501174), r2b (0022501171) + all 15 games | (stream_key, season, publication_id) list; sorted stream_key then season; non-provenance keys equal r2 |
| Six routes: pre-existing fields unchanged | reshaped body | s3 API hooks r1..r6 | deep diff of non-`provenance` keys vs r2 JSON |
| MCP get_slate new shape fits cap | isError for Apps on 15-game Slate | s3 MCP hooks, Apps and non-Apps | isError false; chars < 150000; `provenance`, `matchup_generation`, games[].matchup_provenance present |
| MCP footers (get_matchup, query_game_logs, get_targets) | footer missing / wrong | s3 MCP hooks | text ends with `Data as of …` lines |
| Offline size replay | — | tools/sp107-repro-size.py against /mcp with this run's per-game bodies | OK served, chars reported |

Search (s2 journeys/search.jsonl) rerun because cheap and read-only; game_logs body unchanged by r3 commits.
Known limits: QA snapshot is frozen 2026-09-11 (pre-2026-27 activation) so a retained prior-season read may not be observable;
Search fails `assert-layout` at 390 px (pre-existing; journey omits it). Token in /tmp/sp107-qa-secrets (0700), deleted after.
