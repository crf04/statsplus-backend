# Post-deploy production browser results — statsplus#107

Run 2026-10-04 (after Railway deployment 16d875cb-5518-4169-9690-95d16dc7e21b, backend merge 58d0407556eabd54854fedd4fee83d1c0cf856c9).
Environment: production (local frontend -> https://statsplus-backend-production.up.railway.app; proxy blocks non-GET).
Frontend: /Users/chrisfu/statsplus-107/frontend HEAD 334f99b7e3623de460cb5142313ca54286d932ca (clean; fingerprint 63a49055a37fb5e1...).
Coordinator: t3code/de11429f b97e927. Backend source context (credential source only, not the deployed revision): ~/statsplus-backend master ac61b66.
Manifest `deployedBackendRevision` is "unknown" by design; deploy identity comes from the Railway deployment record.

Command (per journey J):
  node .agents/skills/verify-statsplus/scripts/control-statsplus.mjs --environment production \
    --frontend /Users/chrisfu/statsplus-107/frontend --backend /Users/chrisfu/statsplus-backend \
    --port 5193 --out <this dir>/J < <this dir>/J.jsonl

| Journey | Verdict | Steps | Affected-route assertions (all 200 unless noted) |
|---|---|---|---|
| matchups.jsonl (stock + 5 resolve steps) | PASSED, exit 0 | 46/46 | slate date=2026-04-10 x2, 2026-08-01; slate not-a-date 400 (expected); matchup game_id=0022501174 x2 (click + reload); targets/resolve date=2026-04-10 and 2026-08-01 + `.slate-targets` visible, unavailable state hidden |
| search.jsonl (stock, unmodified) | PASSED, exit 0 | 42/42 | game_logs x4 asserted (game_filter 5, 3, reload 3, browse); players 200 |
| prod-targets-readonly.jsonl (v3 + 4 resolve steps) | PASSED, exit 0 | 17/17 | targets 200, targets/backtests 200, targets/resolve {} 200; "No Targets active today" visible; unavailable/loading text hidden; first two cards' backtests rendered |

Visible content: NOP @ BOS slate, "15 games 11 Targets active", BOS Defense Sheet with the existing matchup
provenance header lines intact, PTS filter effect at desktop and phone, empty/invalid/recovered slate,
Search badges and sample counts, Targets cards with backtests. ARIA diff vs pre-deploy run
(../../prod-check-20261004T122716): matchup desktop/phone, invalid slate, search shared/filter/return,
targets desktop/phone identical; other diffs are only "25d ago"->"26d ago" freshness and more data loaded at proof time.

Observed responses: no non-GET requests. Only non-200s: slate 400 + resolve 400 for date=not-a-date (expected),
players/next-opponent 404 x1 (offseason; also present pre-deploy).

Screenshots: matchups/{slate-desktop,matchup-desktop,matchup-phone,slate-empty-phone,slate-invalid-phone,slate-recovered-phone}.png,
search/{search-shared-desktop,search-filter-applied-desktop,search-reloaded-phone,search-refused-phone,search-browse-desktop,search-return-desktop}.png,
prod-targets-readonly/{targets-list-desktop,targets-list-phone}.png; video under each */video/.
Cleanup: all three session.json cleanedUp=true, runtime dirs absent, port 5193 free.

Limits:
- POST /api/user/targets/preview untested: Target Lab uses it and the production proxy rejects non-GET (not worked around).
- GET /api/matchups/unscheduled untested in browser: the frontend never calls it (MCP-only).
- Response bodies are not exported, so `provenance` presence is not shown here; this proves only that the
  unchanged frontend decodes and renders the deployed responses.
- Helper does not capture browser console messages; console errors are unobserved (no error/alert UI seen beyond the expected invalid-date alert).
- Search phone assert-layout omitted (known pre-existing overflow).
