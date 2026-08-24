# Review brief — backend issue #198

Repository: crf04/statsplus-backend (worktree at /private/tmp/sp-be-198)
Branch: t3code/issue-198-team-filters-db
Base: origin/master (3013d8a)
Commit under review: 9ec4e27

Read the issue first: `gh issue view 198`.
Read `AGENTS.md`, `docs/ARCHITECTURE.md`, and `docs/API_DOCUMENTATION.md` as needed.
Inspect the diff with `git diff origin/master...HEAD`.

## What was asked (issue #198, abbreviated)

Serve game-log Team Filters (`teams_against`) from Season Rankings
publications, database-only:

- Every Team Filter category ranks by whole-Regular-Season opponent aggregates
  read from the durable window-aware team matchup publications. No governed-
  window parameter, no new error case.
- `date_filter` only trims the player's own game logs; it never reshapes
  rankings. Date-plus-Team-Filter requests stay valid and season-ranked.
- Wire contract (parameters, response shape, errors) byte-compatible with today.
- Remove the dated live-provider branch and its daily Redis cache path.
- Stop injecting the NBA Stats adapter into the game service so request-time
  provider calls are impossible by dependency construction.
- Serve last-good rankings silently when the newest publication is stale.
- `./scripts/check.sh` passes.

## Where to look

- `app/services/team_filter_rankings.py` (new): the one map from Team Filter to
  publication base + per-48 metric keys, and the read/rank service.
- `app/services/game_service.py`: `filter_teams` / `_filter_teams_uncached`,
  constructor, `_fetch_game_logs_from_api`, `ALLOWED_TABLES`.
- `app/dependencies.py`: game service assembly.
- Tests: `tests/services/test_team_filter_rankings.py`,
  `tests/services/test_game_service.py`, `tests/test_game_logs.py`,
  `tests/test_dependencies.py`, `tests/test_nba_stats_adapter.py`.

## Specific things to attack

1. Metric mapping correctness. Are the per-48 metric keys for each Team Filter
   actually the ones the publications carry? Cross-check
   `app/services/ledger_derivations.py` (TEAM_METRICS, ASSIST_DERIVED_METRICS)
   and `app/domain/team_matchup_taxonomy.py` (NBA_PUBLICATION_METRIC_KEYS).
   Is any filter silently mapped to the wrong stat or a stat that will never
   be present?
2. Ranking direction and top-N/bottom-N selection versus the legacy pandas
   `sort_values(ascending=False)` + `head(n)` / `tail(-n)` semantics, including
   `rank_filter == 0` and `|rank_filter| > league size`.
3. Deriving points from made-shot counts (3*FG3M + 2*FG2M) and play-type
   points-per-possession (PTS/POSS) — are these sound given per-48 scaling?
4. Staleness / last-good behavior: does the read actually serve a stale
   publication, and does it correctly refuse an invalid or missing one without
   inventing a new error?
5. Redis caching: the cache key no longer includes the date. Any correctness
   or cache-poisoning problem? Is the TTL policy sensible?
6. Removal of the NBA Stats adapter from GameService: anything now unreachable,
   broken, or silently degraded (including the demo/read-only database path,
   where `publication_reader` is None and rankings return empty)?
7. Season handling: rankings read `settings.nba.current_season`, not
   `query.season_filter`. Is that defensible, and is it consistent with the
   issue and prior behavior?
8. Test quality: do the tests actually prove the issue's "Done when" bullets,
   or do they assert stubs proving nothing? Are any of them tautological?
9. Docs accuracy versus the code.

Be adversarial. Report concrete defects with file:line, the failure scenario,
and severity. Do not restate the diff. If you believe a "Done when" bullet is
not actually met, say so explicitly.
