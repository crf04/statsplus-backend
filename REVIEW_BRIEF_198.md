# Re-review brief — backend issue #198 (round 3)

Repository worktree: /private/tmp/sp-be-198
Branch: t3code/issue-198-team-filters-db
Base: origin/master (3013d8a)

Rounds 1 and 2 each found real defects. Round 2's findings were reported
against 1a6fb9a; commit dc7fa49 claims to resolve them. Verify those fixes and
hunt for anything they introduced or still miss.

Read `gh issue view 198` for the spec. Inspect:
- `git diff 1a6fb9a..HEAD` — the round-2 fix commit
- `git diff origin/master...HEAD` — the whole change

## Round 2 findings and the claimed fixes

1. (High) One unscoreable row emptied the whole ranking; a legitimate
   zero-possession play-type row made every request for that filter empty.
   Claimed fix: `TeamFilterRanking.value` now raises on a missing/non-finite
   metric (refuse the surface) but returns `None` for a zero denominator, and
   `_rank` skips only that team.

2. (Medium) N Team Filters caused N independent reads and could tear across
   generations.
   Claimed fix: `TeamFilterRankingService.rank_all` resolves every filter in a
   request from one `read_many` snapshot; `GameService._season_rankings` is the
   single entry point.

3. (Medium) The canonical-league check validated team IDs but not tricodes.
   Claimed fix: `_league_rows` now also requires each row's tricode to equal
   `NBA_TEAM_ID_TO_TRICODE[row.team_id]`.

4. (High) Historical seasons rank nothing because a stream has one pointer.
   NOT changed — deliberately. Position: attributing the published season's
   rankings to a different year is fabricating evidence, and the issue forbids
   a new error case, so an empty opponent set is the only honest outcome. It is
   now proved against the real reader (asserting `publication_season_mismatch`)
   and documented in `docs/ARCHITECTURE.md` and the `teams_against[]` row of
   `docs/API_DOCUMENTATION.md`. Say whether you accept this rebuttal; if you
   reject it, name the specific alternative you would ship.

## Attack these specifically

- Is the missing-metric versus zero-denominator split correct at every call
  site? Can a NaN, an infinity, a negative, a bool, or a string in `per48` now
  produce a silently wrong ranking instead of a refusal?
- Does skipping a zero-denominator team distort top-N/bottom-N in a way that
  misleads? Consider `rank_filter` negative (weakest N) when teams are skipped.
- Is `rank_all` genuinely one generation? Check that `GameService` has no path
  left that reads rankings outside that snapshot, and that `filter_teams`
  (still used by tests) cannot diverge from the request path.
- Does `read_many` exist with that signature on the production reader, and does
  passing a non-registered stream key behave correctly?
- Re-check the whole change against every "Done when" bullet in issue #198.
- Are the tests real proofs? Call out any that assert only stub behavior.

Report concrete defects with file:line, failure scenario, and severity. Be
adversarial. If a claimed fix is not actually a fix, say so.
