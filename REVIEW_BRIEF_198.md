# Re-review brief — backend issue #198 (round 4)

Repository worktree: /private/tmp/sp-be-198
Branch: t3code/issue-198-team-filters-db
Base: origin/master (3013d8a)

Rounds 1-3 each found real defects. Round 3's findings were reported against
dc7fa49; commit 23c65a6 claims to resolve them.

GitHub may be unreachable from the sandbox. If `gh issue view 198` fails, the
issue's requirements are reproduced verbatim below — use them, do not guess.

## Issue #198 requirements (verbatim "What to build" and "Done when")

What to build: Replace both team-filter read branches with one
publication-backed Season Rankings read for every Team Filter category. Remove
the dated live-provider branch and its daily Redis cache path. Stop injecting
the NBA Stats adapter into the game service so request-time provider calls are
impossible by dependency construction (mirror the projection-read cutover
pattern). Serve last-good rankings silently when the newest publication is
stale.

Done when:
- Every Team Filter category ranks from seeded Season publications: ranking
  order, top-N/bottom-N selection, and derived columns are proven at the
  service seam.
- A date-plus-Team-Filter request trims the player's logs by date while
  rankings remain season-wide.
- Route tests prove the wire contract is unchanged for previously valid Filter
  Set URLs, including legacy date-plus-team combinations.
- Staleness tests prove last-good rankings serve when the newest publication is
  stale or a refresh failed.
- No provider client is reachable from the game-log dependency graph
  (structural test), and the dated live-call branch and its cache path are
  removed.
- `./scripts/check.sh` passes.

Contract slice: Team Filters (`teams_against`) always rank by Season Rankings:
whole-Regular-Season opponent aggregates read from the durable window-aware
team matchup publications. No governed-window parameter; no new error case.
`date_filter` only trims the player's own game logs; it never reshapes
rankings. Wire contract (parameters, response shape, errors) is byte-compatible
with today.

## Inspect

- `git diff dc7fa49..HEAD` — the round-3 fix commit
- `git diff origin/master...HEAD` — the whole change

## Round 3 findings and claimed fixes

1. (High) Zero-denominator handling accepted contradictory evidence
   (`PTS>0 / POSS=0` silently dropped).
   Claimed fix: `TeamFilterRanking.value` raises unless the numerator is also
   zero; only `0/0` returns `None`. Test corrected; refusal test added.
2. (Medium) Docs promised "all thirty opponents or nothing" while a skipped
   team shrank the universe for negative ranks.
   Claimed fix: exclusion kept but specified in `docs/ARCHITECTURE.md` and the
   module docstring; a test pins exclusion from both ends.
3. (Low) Derived sums/quotients could overflow to infinity.
   Claimed fix: `_finite` revalidates the sum and the quotient, and now also
   rejects booleans and non-numbers.
4. (Low) The one-snapshot claim was proved only by a stub counter.
   Claimed fix: a real two-stream publication test asserts exactly one
   publication query via a SQLAlchemy cursor listener.
5. (Low) `game_service.py` module docstring called the service "database-only".
   Claimed fix: reworded.

Round 3 accepted the historical-season rebuttal (a stream has one pointer, so a
request for an unpublished season ranks nothing). Do not re-lit igate it unless
you have new evidence it is unsafe.

## Attack these specifically

- Verify each round-3 fix is complete and introduced nothing.
- Is `0/0` genuinely the only legitimate unscoreable shape now? Consider every
  filter definition, not just play types — can any non-rate filter reach the
  `None` path, and would that be wrong?
- Does the both-ends exclusion interact badly with the intersection of multiple
  Team Filters in one request?
- Re-check the whole change against every Done-when bullet above, especially
  the structural "no provider client reachable" bullet — the implementer scoped
  that test to `GameService` holding no NBA Stats adapter, on the grounds that
  the injected game-log source legitimately retains a live PBP path. Say
  whether that scoping is defensible or a spec miss.
- Any remaining test that asserts only stub behavior where a real seam exists?

Report concrete defects with file:line, failure scenario, and severity. If a
claimed fix is not a fix, say so. If the change is ready to ship, say that
plainly rather than inventing findings.
