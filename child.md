UNEDITED AND AUTHORITATIVE SPEC: crf04/statsplus-backend#273
Title: Add activation-safe metadata refresh to the hosted nightly

Part of crf04/statsplus#46. The parent owns the outcome and production acceptance.

## Outcome

Extend `python scripts/nightly_refresh.py --hosted-only` to refresh remaining legacy player metadata alongside PBP game logs. Retain the hosted prohibition on NBA Stats construction/calls and keep the six-step legacy operator mode unchanged.

## Current behavior and entry points

Verified against backend `ce9c7e33eab4de80ec1f5cddfd5145456a9e1851` on 2026-09-13. Owner: `crf04/statsplus-backend`.

- `scripts/nightly_refresh.py`: `run_hosted_refresh` runs only game logs; `_run` returns before constructing `DataService`; `_run_refresh_steps` stops at the first failure and retries the entire unit. Retain that behavior for legacy mode, not for the new independent hosted steps.
- `app/services/data_service.py`: `_frame_collectors`, `_collect_all_frames`, `_refuses_table`, and `update_all_data` already provide pre-collection refusal plus publication-time fencing and transactional stats completion. The surviving frame under the required activation state is the offline `player_information` list.
- `app/services/database_first_activation.py`: `LegacyWriteFence.is_activated` fails closed for missing/unreadable registry state.
- `app/services/stats_freshness_repository.py`: existing per-surface completion records.
- `app/services/player_game_log_ingest.py`: existing PBP ingestion and successful/no-new-game behavior.
- `tests/test_nightly_refresh_command.py`: hosted no-NBA construction test, assembly tests, and legacy retry contracts.
- `tests/services/test_data_service.py`: inspect existing collection/publication coverage and extend the relevant owning tests.
- `docs/ARCHITECTURE.md`: hosted versus legacy operator-mode contract; update hosted documentation and CLI help.

Corrections to the prior issue: PlayerDietService already receives its write fence and skips activated bases before collection. Catalog ingestion already maintains canonical reader tables and freshness. Neither is implementation work for this issue.

## Parent contract slice

> Hosted mode runs both remaining legacy metadata refresh and PBP game-log ingestion, with zero NBA Stats adapter construction or requests.
>
> Hosted metadata refresh requires the replacement streams for its other four collector frames to be activated: `player_per36`, `exact_shot_zones_opponent_season`, `exact_shot_zones`, and `assist_locations_season`. A disabled/missing stream or unreadable activation state fails this step before provider collection; it never triggers a hosted fallback request or advances `stats_tables`.
>
> The two hosted steps have independent bounded execution: attempt each even if the other fails; retry a failed step once without rerunning an already successful step. Exit nonzero if either remains failed, and identify the failed step in diagnostics.
>
> Each completion record advances only with its own successful publication. A failed step retains its previous complete data and completion record.

## Implementation requirements

1. Assemble the activation-aware stats service for hosted mode using providers that cannot contact NBA Stats. Keep setup/check failures belonging to the metadata step inside that step's failure boundary so they cannot prevent PBP ingestion from being attempted.
2. Preflight the four required streams before collecting metadata. Reuse existing DataService refusal and final publication fences; do not replace them with a preflight-only check. Missing/disabled state is not permission to refresh legacy NBA data. Do not activate streams automatically.
3. Run the two hosted steps independently with one retry per failed step and an aggregate exit status. Preserve the existing PBP ingestion implementation and the six-step legacy-mode retry contract.
4. Publish through `update_all_data` so metadata and `stats_tables` completion remain transactional. Do not stamp residential surfaces as refreshed.
5. Update help text, architecture documentation, and offline tests. Keep the production command and schedule unchanged.

## Done when

- [ ] Real hosted assembly with temporary database and injected provider boundaries proves zero NBA adapter construction and requests, successful metadata publication, and game-log execution.
- [ ] Metadata failure (False or exception) still attempts game logs; game-log failure still allows metadata success. Only failed steps retry, at most once, and any exhausted failure returns nonzero.
- [ ] Each of the four activation prerequisites disabled or absent, and unreadable activation state, fails metadata before provider collection without preventing game-log execution or advancing stats completion.
- [ ] Failed publication preserves last-good metadata/completion; success advances the correct completion only. Existing publication-time activation protection remains covered.
- [ ] Legacy six-step execution/retry and Player Diet partial-activation contracts remain unchanged.
- [ ] `./scripts/check.sh` passes from the backend repository root.
- [ ] Return exact implementation/QA revisions, commands, and evidence for the parent's isolated QA and scheduled-production acceptance. Keep QA and production verdicts separate.

## Rollout and dependencies

No unresolved implementation start blocker. The prior prerequisite children #221, #261, #265, and #267 are complete. Live activation and scheduler facts must be rechecked during rollout; September 8 tracker observations are not current production proof.

Deploy the implementation while retaining `python scripts/nightly_refresh.py --hosted-only`. Do not remove the flag or activate catalogs. Observe the next scheduled job and record the parent's separate completion evidence. Roll back to the prior revision if needed to restore game-log-only hosted execution.

PR linkage: `Closes #273` and `Part of crf04/statsplus#46`. Do not auto-close the parent.

The revised parent supersedes the former plan to skip all residential-owned steps in the six-step command.
