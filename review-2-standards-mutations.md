# Standards review: final real-assembly test mutation proof

Snapshot: `9ccde7d9c0aac695a735b6dc13ddb86b91722b8f`
Base: `ce9c7e33eab4de80ec1f5cddfd5145456a9e1851`
Checkout: `review-2-standards`
Diff: `git diff ce9c7e33eab4de80ec1f5cddfd5145456a9e1851 HEAD`

Python: 3.11.9. Shared .venv used only for dependencies; no bootstrap or dependency installation. Tests run from this isolated checkout. Full gate is owned by implementation, as requested.

Baseline: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py tests/services/test_data_service.py -q` — 54 passed (32 nightly, 22 DataService). No DataService mutations repeated.

## M01: KILLED

Disconnect hosted ingestion from its PBP provider (previous surviving M08).

Runtime file: `scripts/nightly_refresh.py`

Before:
```python
            pbp_provider=PBPGameLogAdapter(settings=settings),
```
After:
```python
            pbp_provider=None if hosted_only else PBPGameLogAdapter(settings=settings),
```

Command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider -q --tb=short`

Exit: 1

```text
============================= test session starts ==============================
platform darwin -- Python 3.11.9, pytest-9.1.1, pluggy-1.6.0
rootdir: /privatereview-2-standards
configfile: pytest.ini
plugins: mock-3.15.1, cov-7.1.0, xdist-3.8.0, anyio-4.14.2
collected 1 item

tests/test_nightly_refresh_command.py F                                  [100%]

=================================== FAILURES ===================================
____ test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider _____
tests/test_nightly_refresh_command.py:229: in test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider
    assert nightly_refresh._run(database_url, hosted_only=True) == 0
E   AssertionError: assert 1 == 0
E    +  where 1 = <function _run at 0x12171b1a0>('sqlite:////private/var/folders/vl/c4y_fblx7bg4lgkdxnmsbj_h0000gn/T/pytest-of-chrisfu/pytest-34/test_hosted_refresh_runs_real_0/nightly.sqlite3', hosted_only=True)
E    +    where <function _run at 0x12171b1a0> = nightly_refresh._run
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
============================== 1 failed in 0.80s ===============================

```

Restored exact source bytes; SHA-256: `9dc013daaa82e2cd3773ba783b6b5496983813d5c13f3dbbc8eeb315ed83518c`.

## M02: KILLED

Construct the prohibited NBA adapter in hosted metadata assembly.

Runtime file: `scripts/nightly_refresh.py`

Before:
```python
    write_fence = LegacyWriteFence(engine)
    data_service = DataService(
```
After:
```python
    NBAStatsAdapter(settings=settings)
    write_fence = LegacyWriteFence(engine)
    data_service = DataService(
```

Command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider -q --tb=short`

Exit: 1

```text
============================= test session starts ==============================
platform darwin -- Python 3.11.9, pytest-9.1.1, pluggy-1.6.0
rootdir: /privatereview-2-standards
configfile: pytest.ini
plugins: mock-3.15.1, cov-7.1.0, xdist-3.8.0, anyio-4.14.2
collected 1 item

tests/test_nightly_refresh_command.py F                                  [100%]

=================================== FAILURES ===================================
____ test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider _____
tests/test_nightly_refresh_command.py:229: in test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider
    assert nightly_refresh._run(database_url, hosted_only=True) == 0
E   AssertionError: assert 1 == 0
E    +  where 1 = <function _run at 0x12ca1f1a0>('sqlite:////private/var/folders/vl/c4y_fblx7bg4lgkdxnmsbj_h0000gn/T/pytest-of-chrisfu/pytest-35/test_hosted_refresh_runs_real_0/nightly.sqlite3', hosted_only=True)
E    +    where <function _run at 0x12ca1f1a0> = nightly_refresh._run
----------------------------- Captured stderr call -----------------------------
Nightly Refresh attempt 1 failed during metadata refresh; retrying.
Nightly Refresh attempt 2 failed during metadata refresh; no retries remain.
=========================== short test summary info ============================
FAILED tests/test_nightly_refresh_command.py::test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider
============================== 1 failed in 0.73s ===============================

```

Restored exact source bytes; SHA-256: `9dc013daaa82e2cd3773ba783b6b5496983813d5c13f3dbbc8eeb315ed83518c`.

## M03: KILLED

Publish real game-log rows without their separate stats-surface completion.

Runtime file: `app/services/player_game_log_repository.py`

Before:
```python
            if canonical_season == self._stats_surface_season:
                self._surface_freshness.record_success(
                    retrieved, connection=connection
                )
        return PlayerGameLogPublication(
            row_count=published_rows,
```
After:
```python
        return PlayerGameLogPublication(
            row_count=published_rows,
```

Command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py::test_hosted_refresh_runs_real_ingestion_against_injected_pbp_provider -q --tb=short`

Exit: 1

```text
============================= test session starts ==============================
platform darwin -- Python 3.11.9, pytest-9.1.1, pluggy-1.6.0
rootdir: /privatereview-2-standards
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
============================== 1 failed in 0.84s ===============================

```

Restored exact source bytes; SHA-256: `53c5de9a9abd3c229ba766ba590e04979b26850f1a75382f2c1a1a16306ea00e`.

All three mutants killed with meaningful assertion failures; zero survivors. Every mutation restored byte-for-byte before the next. `git diff --exit-code HEAD` passed and `git status --short` was empty.

## Final verification

Command: `.venv/bin/python -m pytest tests/test_nightly_refresh_command.py tests/services/test_data_service.py -q`

```text
============================= test session starts ==============================
platform darwin -- Python 3.11.9, pytest-9.1.1, pluggy-1.6.0
rootdir: /privatereview-2-standards
configfile: pytest.ini
plugins: mock-3.15.1, cov-7.1.0, xdist-3.8.0, anyio-4.14.2
collected 54 items

tests/test_nightly_refresh_command.py ................................   [ 59%]
tests/services/test_data_service.py ......................               [100%]

============================== 54 passed in 1.74s ==============================

```

`git diff --exit-code HEAD` passed. Final `git status --short` output was empty.
