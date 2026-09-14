# Spec-review mutation ledger

Snapshot: `88787c98aa7aa7b2a2bdaa8f326264713d756108`
Base: `ce9c7e33eab4de80ec1f5cddfd5145456a9e1851`
Checkout: `review-1-spec`
Diff: `git diff ce9c7e33eab4de80ec1f5cddfd5145456a9e1851 HEAD`
Baseline: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py -q` — 31 passed. Python 3.11.9. Shared dependencies used as explicitly instructed; bootstrap not run because it would modify shared dependencies. Full completion gate delegated to implementation as requested.

Each mutation changes runtime code, never assertions. Every source is restored byte-for-byte before the next mutant. Full pytest failure evidence is linked below.

## M01: KILLED

Omit hosted metadata execution/publication.

Runtime file: `scripts/nightly_refresh.py`

Change:
```diff
- refresh_metadata=lambda: _run_hosted_metadata(engine, settings)
+ refresh_metadata=lambda: True
```

Command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_hosted_refresh_publishes_activation_gated_metadata_and_game_logs -q --tb=short`

Covered IDs:
- `tests/test_nightly_refresh_command.py::test_hosted_refresh_publishes_activation_gated_metadata_and_game_logs`

Exit: 1; [full evidence](review-1-spec-mutation-logs/M01.txt). Restored original SHA-256: `9dc013daaa82e2cd3773ba783b6b5496983813d5c13f3dbbc8eeb315ed83518c`.

## M02: KILLED

Remove the activation preflight; let collection start before validating all prerequisites.

Runtime file: `scripts/nightly_refresh.py`

Change:
```diff
-     data_service.require_hosted_metadata()

+ 
```

Command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_disabled[player_per36] tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_disabled[exact_shot_zones_opponent_season] tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_disabled[exact_shot_zones] tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_disabled[assist_locations_season] tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_missing[player_per36] tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_missing[exact_shot_zones_opponent_season] tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_missing[exact_shot_zones] tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_missing[assist_locations_season] tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_activation_state_is_unreadable -q --tb=short`

Covered IDs:
- `tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_disabled[player_per36]`
- `tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_disabled[exact_shot_zones_opponent_season]`
- `tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_disabled[exact_shot_zones]`
- `tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_disabled[assist_locations_season]`
- `tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_missing[player_per36]`
- `tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_missing[exact_shot_zones_opponent_season]`
- `tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_missing[exact_shot_zones]`
- `tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_a_required_stream_is_missing[assist_locations_season]`
- `tests/test_nightly_refresh_command.py::test_hosted_metadata_fails_closed_when_activation_state_is_unreadable`

Exit: 1; [full evidence](review-1-spec-mutation-logs/M02.txt). Restored original SHA-256: `9dc013daaa82e2cd3773ba783b6b5496983813d5c13f3dbbc8eeb315ed83518c`.

## M03: KILLED

Swallow completion-write failure and commit metadata as successful.

Runtime file: `app/services/table_publisher.py`

Change:
```diff
-                     publication_completion(connection)
+                     try:
                        publication_completion(connection)
                    except Exception:
                        pass
```

Command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_failed_hosted_metadata_publication_preserves_last_good -q --tb=short`

Covered IDs:
- `tests/test_nightly_refresh_command.py::test_failed_hosted_metadata_publication_preserves_last_good`

Exit: 1; [full evidence](review-1-spec-mutation-logs/M03.txt). Restored original SHA-256: `b4095c9aab8be75039150e44d7623e26b8f139f3cf03e686ff566e6f89c72ce5`.

## M04: KILLED

Retry both hosted steps after any failure, including already successful steps.

Runtime file: `scripts/nightly_refresh.py`

Change:
```diff
-     for step, refresh in failed:
        succeeded[step] = _refresh_succeeded(refresh)
+     for step, refresh in steps:
        succeeded[step] = _refresh_succeeded(refresh)
```

Command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_hosted_metadata_succeeds_even_when_game_logs_fail tests/test_nightly_refresh_command.py::test_hosted_refresh_retries_only_the_failed_metadata_step tests/test_nightly_refresh_command.py::test_hosted_refresh_retries_only_the_failed_game_log_step -q --tb=short`

Covered IDs:
- `tests/test_nightly_refresh_command.py::test_hosted_metadata_succeeds_even_when_game_logs_fail`
- `tests/test_nightly_refresh_command.py::test_hosted_refresh_retries_only_the_failed_metadata_step`
- `tests/test_nightly_refresh_command.py::test_hosted_refresh_retries_only_the_failed_game_log_step`

Exit: 1; [full evidence](review-1-spec-mutation-logs/M04.txt). Restored original SHA-256: `9dc013daaa82e2cd3773ba783b6b5496983813d5c13f3dbbc8eeb315ed83518c`.

## M05: KILLED

Skip game-log execution while reporting success.

Runtime file: `scripts/nightly_refresh.py`

Change:
```diff
-             ("player game logs", refresh_player_game_logs),
        )
    )


def _run_independent
+             ("player game logs", lambda: True),
        )
    )


def _run_independent
```

Command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_hosted_refresh_runs_both_steps_once_on_success -q --tb=short`

Covered IDs:
- `tests/test_nightly_refresh_command.py::test_hosted_refresh_runs_both_steps_once_on_success`

Exit: 1; [full evidence](review-1-spec-mutation-logs/M05.txt). Restored original SHA-256: `9dc013daaa82e2cd3773ba783b6b5496983813d5c13f3dbbc8eeb315ed83518c`.

## M06: KILLED

Return success after both retries exhaust.

Runtime file: `scripts/nightly_refresh.py`

Change:
```diff
-     return 1 if exhausted else 0
+     return 0
```

Command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_hosted_refresh_returns_failure_after_independent_retries -q --tb=short`

Covered IDs:
- `tests/test_nightly_refresh_command.py::test_hosted_refresh_returns_failure_after_independent_retries`

Exit: 1; [full evidence](review-1-spec-mutation-logs/M06.txt). Restored original SHA-256: `9dc013daaa82e2cd3773ba783b6b5496983813d5c13f3dbbc8eeb315ed83518c`.

## M07: KILLED

Construct the forbidden NBA adapter inside hosted metadata assembly.

Runtime file: `scripts/nightly_refresh.py`

Change:
```diff
-     write_fence = LegacyWriteFence(engine)
    data_service
+     NBAStatsAdapter(settings=settings)
    write_fence = LegacyWriteFence(engine)
    data_service
```

Command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_hosted_refresh_publishes_activation_gated_metadata_and_game_logs -q --tb=short`

Covered IDs:
- `tests/test_nightly_refresh_command.py::test_hosted_refresh_publishes_activation_gated_metadata_and_game_logs`

Exit: 1; [full evidence](review-1-spec-mutation-logs/M07.txt). Restored original SHA-256: `9dc013daaa82e2cd3773ba783b6b5496983813d5c13f3dbbc8eeb315ed83518c`.

## M08: SURVIVED

Disconnect only hosted PBP ingestion from its required provider, leaving legacy wiring unchanged.

Runtime file: `scripts/nightly_refresh.py`

Change:
```diff
-             pbp_provider=PBPGameLogAdapter(settings=settings),
+             pbp_provider=None if hosted_only else PBPGameLogAdapter(settings=settings),
```

Command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py -q --tb=short`

Covered IDs:
- `tests/test_nightly_refresh_command.py`

Exit: 0; [full evidence](review-1-spec-mutation-logs/M08.txt). Restored original SHA-256: `9dc013daaa82e2cd3773ba783b6b5496983813d5c13f3dbbc8eeb315ed83518c`.

## M09: KILLED

Stamp the game-log surface instead of stats_tables for metadata publication.

Runtime file: `scripts/nightly_refresh.py`

Change:
```diff
-         stats_freshness=StatsFreshnessRepository(engine),
+         stats_freshness=StatsFreshnessRepository(engine, surface="player_game_logs"),
```

Command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_hosted_refresh_publishes_activation_gated_metadata_and_game_logs -q --tb=short`

Covered IDs:
- `tests/test_nightly_refresh_command.py::test_hosted_refresh_publishes_activation_gated_metadata_and_game_logs`

Exit: 1; [full evidence](review-1-spec-mutation-logs/M09.txt). Restored original SHA-256: `9dc013daaa82e2cd3773ba783b6b5496983813d5c13f3dbbc8eeb315ed83518c`.

## M10: KILLED

Advance stats_tables on the failed-publication cleanup path, despite rolling back metadata.

Runtime file: `app/services/table_publisher.py`

Change:
```diff
-         except BaseException:
            self._cleanup_staging(staging_names)
+         except BaseException:
            from app.services.stats_freshness_repository import StatsFreshnessRepository
            from datetime import datetime, timezone
            StatsFreshnessRepository(self.engine).record_success(datetime.now(timezone.utc))
            self._cleanup_staging(staging_names)
```

Command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_failed_hosted_metadata_publication_preserves_last_good -q --tb=short`

Covered IDs:
- `tests/test_nightly_refresh_command.py::test_failed_hosted_metadata_publication_preserves_last_good`

Exit: 1; [full evidence](review-1-spec-mutation-logs/M10.txt). Restored original SHA-256: `b4095c9aab8be75039150e44d7623e26b8f139f3cf03e686ff566e6f89c72ce5`.

## M11: KILLED

Commit clearing existing target rows before entering atomic replacement. A subsequent completion failure must preserve previous rows.

Runtime file: `app/services/table_publisher.py`

Inserted runtime code before staging:
```python
        # Defect: commit destructive clearing before the atomic replacement.
        from sqlalchemy import inspect
        with self.engine.begin() as clearing:
            for table in targets:
                if inspect(clearing).has_table(table):
                    quoted = clearing.dialect.identifier_preparer.quote(table)
                    clearing.execute(text(f"DELETE FROM {quoted}"))
```

Command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_failed_hosted_metadata_publication_preserves_last_good -q --tb=short`

Covered ID: `tests/test_nightly_refresh_command.py::test_failed_hosted_metadata_publication_preserves_last_good`

Exit: 1; meaningful failure: `assert [] == ['old']`. [Full evidence](review-1-spec-mutation-logs/M11.txt). Restored exact original bytes.

## Final verification and verdict

- 11 runtime mutants: 10 killed with meaningful assertions, 1 survivor (M08).
- All 16 assigned changed/new test cases have at least one targeted killed mutant: happy assembly (M01/M07/M09), disabled prerequisites x4 and missing prerequisites x4 plus unreadable registry (M02), failed publication (M03/M10/M11), metadata success despite log failure and each single-step retry test (M04), both-step success (M05), exhausted independent failures (M06).
- M08 survived the entire 31-test module. This is material against child #273: “Real hosted assembly with temporary database and injected provider boundaries proves zero NBA adapter construction and requests, successful metadata publication, and game-log execution.” The helper at tests/test_nightly_refresh_command.py:125 replaces the ingestion service and ignores its constructor kwargs, rather than exercising the real ingestion path. Hosted-only `pbp_provider=None` is therefore undetected; the real `_stage_game` requires `self.pbp_provider.fetch_game_player_logs`.
- Baseline and final command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py -q` — 31 passed both times.
- `git diff --exit-code HEAD` succeeded; `git status --short` was empty. All tracked files exactly match snapshot `88787c98aa7aa7b2a2bdaa8f326264713d756108`.
- Read repository/global AGENTS.md, CONTRIBUTING.md, applicable architecture/API contracts, and code-review smell baseline. Review stayed on the assigned spec axis; no additional runtime mismatch found.
- No implementation-checkout source changes, agents, external writes, live QA, deployments, or full-suite execution. Production acceptance remains unverified and separate.
