UNEDITED AND AUTHORITATIVE SPEC: crf04/statsplus#46
Title: Refresh remaining legacy metadata alongside hosted nightly game logs

## Outcome

Refresh the remaining legacy player metadata alongside PBP player game logs in Railway's hosted nightly, without contacting NBA Stats or interrupting the working game-log refresh.

Keep `python scripts/nightly_refresh.py --hosted-only` as the production command. Extend its hosted-owned work; do not switch Railway to the six-step legacy operator mode.

## Current behavior and evidence

Scope reconciled 2026-09-13 against backend `ce9c7e33eab4de80ec1f5cddfd5145456a9e1851`.

- Hosted mode runs only PBP game-log ingestion, using stored canonical catalogs and an inert NBA provider. It never runs `DataService.update_all_data`.
- `update_all_data` already refuses activated tables before collection and retains publication-time race protection. With the four relevant replacement streams activated, its only remaining frame is `player_information`, built from the bundled offline player list.
- The six-step legacy mode still invokes catalog and team-stat collectors and stops on its first failure. Removing `--hosted-only` is not the solution.
- Player Diet pre-collection fencing and nightly fence wiring are already implemented, including partial activation and `player_assist_locations`; they are not remaining work here.
- Residential catalog ingestion already updates canonical catalog tables and freshness records used by existing readers. No catalog reader migration or catalog activation is required for this narrowed outcome.

The latest production evidence in this issue's comments is from 2026-09-08: the production cron is restored, hosted game logs refresh, and `stats_tables` completion remains outstanding. These are historical observations, not a new production health check. Recheck them during rollout. The original title's claim that every table is currently frozen is superseded.

## Completed prerequisites

- [x] Original publication-fence abort fixed: crf04/statsplus-backend#221 / PR #222.
- [x] Activated tables refused before provider collection: crf04/statsplus-backend#261 / PR #264.
- [x] NL player-name lookup moved to the athlete catalog and its legacy nightly frame removed: crf04/statsplus-backend#265 / PR #266.
- [x] Residential exact player shot zones implemented and activated: crf04/statsplus-backend#267 / PR #272, with production acceptance linked in this issue's comments.
- [x] Production cron restoration recorded in the 2026-09-07 operational follow-up.

## Required behavior

1. Hosted mode runs both remaining legacy metadata refresh and PBP game-log ingestion, with zero NBA Stats adapter construction or requests.
2. The metadata step uses the existing activation-aware `update_all_data` publication path. Its expected surviving frame is `player_information`; replaced tables remain untouched.
3. Hosted metadata refresh requires the replacement streams for its other four collector frames to be activated: `player_per36`, `exact_shot_zones_opponent_season`, `exact_shot_zones`, and `assist_locations_season`. A disabled/missing stream or unreadable activation state fails this step before provider collection; it never triggers a hosted fallback request or advances `stats_tables`.
4. The two hosted steps have independent bounded execution: attempt each even if the other fails; retry a failed step once without rerunning an already successful step. Exit nonzero if either remains failed, and identify the failed step in diagnostics.
5. Each completion record advances only with its own successful publication. A failed step retains its previous complete data and completion record.
6. `stats_tables` completion means the remaining legacy refresh succeeded. It must not be presented as proof that residential player/team statistics were refreshed. Game-log completion and residential publication health remain separate evidence.

## Ownership and boundary

Backend-only implementation in `crf04/statsplus-backend`, plus verification of its existing Railway job. No public route, authentication, response-shape, frontend, or provider-ownership change. Existing freshness record names remain unchanged.

- [ ] crf04/statsplus-backend#273 — implement and verify the two-step hosted refresh.

## Acceptance and rollout

- [ ] Backend completion gate `./scripts/check.sh` passes, including provider-invocation assertions, independent failure/retry cases, missing/disabled activation, and unchanged legacy-mode behavior.
- [ ] Isolated QA with the intended activation state proves the metadata publication and real game-log path independently; preserve last-good data on failures.
- [ ] Record deployed revision, current production activation state, cron schedule, and start command before rollout. Keep `--hosted-only` and the existing schedule.
- [ ] Observe an actual scheduled production execution succeeding, with separate evidence that `stats_refreshes['stats_tables']` and the game-log completion record advance. Account for offseason/no-new-game behavior using the existing ingestion contract rather than inventing new freshness semantics.
- [ ] Record residential publication health separately and verify it was not modified by the hosted job.
- [ ] Record rollback to the prior deployed revision and its game-log-only hosted behavior if acceptance fails.

Keep this parent open until production acceptance is recorded; a merged backend PR alone does not close it.

## Out of scope

Converting the six-step legacy command into the hosted scheduler; event/athlete catalog activation; team-refresh fencing refactors; additional Player Diet work; legacy table cleanup; frontend changes; and unrelated stale-data investigations. Those require their own outcome if pursued.

This scope supersedes earlier comments proposing removal of `--hosted-only` or broader step-skipping work. Related context: #19, #39, #45, #49 and crf04/statsplus-backend#87.
