# Re-review brief — backend issue #198 (round 2)

Repository worktree: /private/tmp/sp-be-198
Branch: t3code/issue-198-team-filters-db
Base: origin/master (3013d8a)

Round 1 findings were reported against commit 9ec4e27. Commit 1a6fb9a claims to
resolve them. Verify the fixes and hunt for defects they introduced.

Read `gh issue view 198` for the spec. Inspect:
- `git diff 9ec4e27..HEAD` — the fix commit
- `git diff origin/master...HEAD` — the whole change

## Round 1 findings and the claimed fixes

1. (Spec, High) Rankings were bound to `settings.nba.current_season`, so a
   historical `season_filter` used current-season rankings.
   Claimed fix: `GameService.filter_teams(team_filter, rank_filter, season)`
   now takes `query.season_filter`; `TeamFilterRankingService.ranked_teams`
   takes the season per call and no longer stores one.

2. (Spec, High) The Redis key omitted season and publication generation and
   cached unavailable `[]` results for two hours.
   Claimed fix: the Redis layer was removed from the team-filter read entirely.

3. (Standards High / Spec Medium) Ledger-owned traditional and assist-location
   publications were not proved to carry the canonical 30 teams, and rows that
   could not be scored were silently skipped.
   Claimed fix: `_rows` refuses unless the decoded team-id set equals
   `NBA_TEAM_ID_TO_TRICODE`; `ranked_teams` refuses the whole ranking if any
   row cannot be scored.

4. (Standards Medium) Stale documentation.
   Claimed fix: `docs/ARCHITECTURE.md` NBA Stats adapter section and the
   `tests/test_nba_stats_adapter.py` module docstring corrected.

5. (Standards Low) Duplicated `_StubRankings` fake across two test files.
   NOT fixed — deliberately. The two suites assert different things and a
   shared fixture would couple them. Say whether you accept that rebuttal.

## Attack these specifically

- Does removing the Redis cache introduce a performance or correctness problem?
  How many publication reads does one game-log request with N team filters now
  make, and is that acceptable?
- Is refusing the entire ranking when one team is unscoreable the right
  fail-closed choice, or does it make a real, reachable data shape (a play type
  with zero season possessions) silently return no opponents?
- Is the canonical-league check correct and complete? Does it wrongly reject
  any legitimate publication, or wrongly accept one?
- Does the per-call season plumbing reach every caller? Look for any remaining
  caller of `filter_teams` or `ranked_teams` with the old arity, including the
  NL executor path (`app/services/nl_service.py`) and saved filter sets.
- Are the new and updated tests real proofs, or do they assert stubs?
- Any regression against the issue's Done-when bullets introduced by the fixes?

Report concrete defects with file:line, failure scenario, and severity. Be
adversarial. If a claimed fix is not actually a fix, say so.
