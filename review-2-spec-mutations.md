# Spec review 2 mutation proof

Snapshot: `9ccde7d9c0aac695a735b6dc13ddb86b91722b8f`. Base: `ce9c7e33eab4de80ec1f5cddfd5145456a9e1851`.

Checkout: `review-2-spec` (physical `/privatereview-2-spec`).

Whole-diff command: `git diff ce9c7e33eab4de80ec1f5cddfd5145456a9e1851 HEAD`. Commit list: `9ccde7d9 Temporary final review snapshot for issue 273`.

Authoritative requirements: supplied unedited parent #46 and child #273. Child: “Real hosted assembly with temporary database and injected provider boundaries proves zero NBA adapter construction and requests, successful metadata publication, and game-log execution.” Parent: “Each completion record advances only with its own successful publication.”

Python 3.11.9; shared .venv used only for dependencies. Application import verified inside this isolated checkout. No bootstrap/dependency installation or full gate was run because this review is limited to focused tests and must not modify the shared environment.

Baseline command: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_nightly_refresh_command.py -q -p no:cacheprovider` — **32 passed in 1.38s**.

New test file SHA-256 (never modified): `a0273cd0a54a29baed71a556b4c8f6b0f98cd3125984c9dca2000b4348da8f73`.

Each mutant is applied alone, in this checkout only. Source bytes are saved in memory and restored in a finally block before the next mutant. Each run starts a fresh pytest process; bytecode and pytest-cache writes are disabled.

## M01: Previously surviving hosted PBP provider disconnection

Command: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider -q --tb=short -p no:cacheprovider`

```diff
--- scripts/nightly_refresh.py
+++ scripts/nightly_refresh.py
@@ -243,7 +243,7 @@
             ),
         )
         player_game_log_ingest_service = PlayerGameLogIngestService(
-            pbp_provider=PBPGameLogAdapter(settings=settings),
+            pbp_provider=None if hosted_only else PBPGameLogAdapter(settings=settings),
             repository=player_game_log_repository,
             athlete_catalog=athlete_service,
             event_catalog=event_service,
```

Exit code: 1. Restored exact bytes, SHA-256 `9dc013daaa82e2cd3773ba783b6b5496983813d5c13f3dbbc8eeb315ed83518c`; `git status --short` empty after restoration.

```text
============================= test session starts ==============================
platform darwin -- Python 3.11.9, pytest-9.1.1, pluggy-1.6.0
rootdir: /privatereview-2-spec
configfile: pytest.ini
plugins: mock-3.15.1, cov-7.1.0, xdist-3.8.0, anyio-4.14.2
collected 1 item

tests/test_nightly_refresh_command.py F                                  [100%]

=================================== FAILURES ===================================
____ test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider _____
tests/test_nightly_refresh_command.py:229: in test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider
    assert nightly_refresh._run(database_url, hosted_only=True) == 0
E   AssertionError: assert 1 == 0
E    +  where 1 = <function _run at 0x12c7165c0>('sqlite:////private/var/folders/vl/c4y_fblx7bg4lgkdxnmsbj_h0000gn/T/pytest-of-chrisfu/pytest-37/test_hosted_refresh_runs_real_0/nightly.sqlite3', hosted_only=True)
E    +    where <function _run at 0x12c7165c0> = nightly_refresh._run
----------------------------- Captured stderr call -----------------------------
Nightly Refresh attempt 1 failed during player game logs refresh; retrying.
Nightly Refresh attempt 2 failed during player game logs refresh; no retries remain.
------------------------------ Captured log call -------------------------------
WARNING  app.services.data_service:data_service.py:239 legacy_write_fenced: refusing to refresh player_per36_stats; stream player_per36 is activated
WARNING  app.services.data_service:data_service.py:239 legacy_write_fenced: refusing to refresh opp_shooting_zone; stream exact_shot_zones_opponent_season is activated
WARNING  app.services.data_service:data_service.py:239 legacy_write_fenced: refusing to refresh player_shooting_zones; stream exact_shot_zones is activated
WARNING  app.services.data_service:data_service.py:239 legacy_write_fenced: refusing to refresh pbp_opponent_stats; stream assist_locations_season is activated
=========================== short test summary info ============================
FAILED tests/test_nightly_refresh_command.py::test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider
============================== 1 failed in 1.18s ===============================
```

## M02: Omit metadata stats_tables completion callback while publishing metadata

Command: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider -q --tb=short -p no:cacheprovider`

```diff
--- app/services/data_service.py
+++ app/services/data_service.py
@@ -158,7 +158,7 @@
             self.publisher.publish(
                 frames,
                 publication_fence=final_fence,
-                publication_completion=publication_completion,
+                publication_completion=None,
             )
             progress.complete()
             return True
```

Exit code: 1. Restored exact bytes, SHA-256 `123fa190cb1232897bb8c2b9e947d43e14103830128c124c2d2bfe43b93982bd`; `git status --short` empty after restoration.

```text
============================= test session starts ==============================
platform darwin -- Python 3.11.9, pytest-9.1.1, pluggy-1.6.0
rootdir: /privatereview-2-spec
configfile: pytest.ini
plugins: mock-3.15.1, cov-7.1.0, xdist-3.8.0, anyio-4.14.2
collected 1 item

tests/test_nightly_refresh_command.py F                                  [100%]

=================================== FAILURES ===================================
____ test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider _____
tests/test_nightly_refresh_command.py:254: in test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider
    assert (
E   assert None is not None
E    +  where None = StatsFreshness(last_successful_completion=None).last_successful_completion
E    +    where StatsFreshness(last_successful_completion=None) = get()
E    +      where get = <app.services.stats_freshness_repository.StatsFreshnessRepository object at 0x1233650d0>.get
E    +        where <app.services.stats_freshness_repository.StatsFreshnessRepository object at 0x1233650d0> = StatsFreshnessRepository(Engine(sqlite:////private/var/folders/vl/c4y_fblx7bg4lgkdxnmsbj_h0000gn/T/pytest-of-chrisfu/pytest-38/test_hosted_refresh_runs_real_0/nightly.sqlite3))
------------------------------ Captured log call -------------------------------
WARNING  app.services.data_service:data_service.py:239 legacy_write_fenced: refusing to refresh player_per36_stats; stream player_per36 is activated
WARNING  app.services.data_service:data_service.py:239 legacy_write_fenced: refusing to refresh opp_shooting_zone; stream exact_shot_zones_opponent_season is activated
WARNING  app.services.data_service:data_service.py:239 legacy_write_fenced: refusing to refresh player_shooting_zones; stream exact_shot_zones is activated
WARNING  app.services.data_service:data_service.py:239 legacy_write_fenced: refusing to refresh pbp_opponent_stats; stream assist_locations_season is activated
=========================== short test summary info ============================
FAILED tests/test_nightly_refresh_command.py::test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider
============================== 1 failed in 1.02s ===============================
```

## M03: Omit player_game_logs completion while publishing real game-log facts and season sidecar

Command: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider -q --tb=short -p no:cacheprovider`

```diff
--- app/services/player_game_log_repository.py
+++ app/services/player_game_log_repository.py
@@ -862,9 +862,7 @@
                     insert(refresh_table).values(season=canonical_season, **values)
                 )
             if canonical_season == self._stats_surface_season:
-                self._surface_freshness.record_success(
-                    retrieved, connection=connection
-                )
+                pass  # Mutation: omit only the current-season completion.
         return PlayerGameLogPublication(
             row_count=published_rows,
             recovered_removed_row_count=0,
```

Exit code: 1. Restored exact bytes, SHA-256 `53c5de9a9abd3c229ba766ba590e04979b26850f1a75382f2c1a1a16306ea00e`; `git status --short` empty after restoration.

```text
============================= test session starts ==============================
platform darwin -- Python 3.11.9, pytest-9.1.1, pluggy-1.6.0
rootdir: /privatereview-2-spec
configfile: pytest.ini
plugins: mock-3.15.1, cov-7.1.0, xdist-3.8.0, anyio-4.14.2
collected 1 item

tests/test_nightly_refresh_command.py F                                  [100%]

=================================== FAILURES ===================================
____ test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider _____
tests/test_nightly_refresh_command.py:247: in test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider
    assert sorted(row.game_id for row in rows) == [
E   AssertionError: assert [] == ['0022500001', '0022500004']
E     
E     Right contains 2 more items, first extra item: '0022500001'
E     Use -v to get more diff
------------------------------ Captured log call -------------------------------
WARNING  app.services.data_service:data_service.py:239 legacy_write_fenced: refusing to refresh player_per36_stats; stream player_per36 is activated
WARNING  app.services.data_service:data_service.py:239 legacy_write_fenced: refusing to refresh opp_shooting_zone; stream exact_shot_zones_opponent_season is activated
WARNING  app.services.data_service:data_service.py:239 legacy_write_fenced: refusing to refresh player_shooting_zones; stream exact_shot_zones is activated
WARNING  app.services.data_service:data_service.py:239 legacy_write_fenced: refusing to refresh pbp_opponent_stats; stream assist_locations_season is activated
=========================== short test summary info ============================
FAILED tests/test_nightly_refresh_command.py::test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider
============================== 1 failed in 1.18s ===============================
```

## Behavioral interpretation and final verdict

- **M01 KILLED:** exact prior surviving mutation, hosted `pbp_provider=None`, now fails at test line 229 (`assert 1 == 0`). Diagnostics identify both failed player-game-log attempts. Real `_stage_game` requires `self.pbp_provider.fetch_game_player_logs`; the fake service which concealed this defect is not used by the new test.
- **M02 KILLED:** omission of the metadata completion callback still permits publication and the command succeeds, but test line 254 fails because `stats_tables.last_successful_completion` is None. Provider-call, game-row, and metadata-value assertions preceding it passed.
- **M03 KILLED:** omission of the incremental repository's game-log surface completion leaves the command successful but test line 247 fails (`[]` versus the two expected game IDs). This is a required-behavior failure: `PlayerGameLogRepository._season_is_readable` at lines 1450–1454 returns False without the surface completion, and `list_player_rows` at lines 376–377 returns no rows. It fails before the later explicit game-log completion assertion; the test cannot pass when this completion is omitted.
- **3 killed, 0 survivors.** No unchanged tests were mutation-checked again.
- Final command: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_nightly_refresh_command.py -q -p no:cacheprovider` — **32 passed in 1.99s** after all restorations (32 passed before mutations as well).
- Read AGENTS.md, CONTRIBUTING.md, issue-tracker guidance, the applicable architecture/API contracts, and the code-review skill. Reviewed all seven files in the final whole diff against the supplied unedited parent #46 and child #273, including independent retry semantics, activation preflight plus retained collection/publication fences, inert metadata providers, transactional completion, unchanged legacy execution, and documentation scope.
- **Previous P2 resolved. No remaining material Spec-axis findings.** The new test runs real catalog reads, real provider-driven normalization/ingestion, real repository publication, and asserts independently governed completions.
- No full backend gate, live QA, or production acceptance was run in this focused review. Coordinator-reported QA is separate evidence; scheduled production acceptance remains pending merge/deploy.
- Source bytes restored exactly after every mutation. `git diff --exit-code HEAD` passed and final `git status --short` was empty. No fixes, commits, agents, external publication, or shared-environment modifications.
