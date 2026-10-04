# statsplus#107 round 3 — QA (Boundary tier) + live MCP against QA

Revisions: helper coordination b97e927; QA backend 3572c14e (clean); frontend 334f99b (clean); MCP de961e4 (clean, imports /mcp/src).
Port 5194; QA identity, real Firebase custom-token auth; three sequential sessions via tools/drive.py. SHA-256: EXECUTED-JSONL.sha256.

| Check | Verdict | Evidence |
| --- | --- | --- |
| s1 journeys/matchups.jsonl | passed | session-s1-matchups/*.png |
| s2 journeys/search.jsonl | passed | session-s2-search/*.png |
| s3 Targets create → Lab → list → phone → delete → empty | passed | session-s3-targets/*.png |
| Six routes carry `provenance` {generation, sources}; non-provenance keys equal r2 (only Target timestamps differ) | passed | api/, api-analysis.json |
| Stream-keyed legacy entries equal r2 except age_seconds (+~2125 s elapsed) | passed | api-analysis.json |
| Past-game Matchup lists retained prior-season Publication beside current snapshot | untested live | QA snapshot (2026-09-11) has only 2025-26 Publications (37/37), so no 2026-27 snapshot exists to sit beside |
| Errors / /api/players carry no block | passed | api/x-*.json |
| MCP four tools, Apps and non-Apps, isError false, footer present | passed | mcp-calls/mcp-apps.json, mcp-noapps.json |
| get_slate size margin | defect (latent) | repro-size-*.log, tools/make-age-spread.py |

## Defect: get_slate matchup_generation dedup is keyed on the whole entry, including age_seconds
Live Apps get_slate 2026-04-10: 15 distinct (stream, season, publication_id) but 101 matchup_generation entries (70,319 of 84,430
structured chars). The only varying field is age_seconds (7 values, 2592136..2592142): the 15 Matchup reads land in different seconds.
If the reads span ≥14 distinct seconds (slower backend), the result exceeds the cap again:
`python3 -B tools/make-age-spread.py && SP107_API_DIR=$PWD/api-age-spread/ <mcp>/.venv/bin/python -B tools/sp107-repro-size.py <mcp> 1`
→ `ValueError: tool result is 167876 chars, over the 150000 limit`. Same bodies with real ages → 69,855 chars OK.
