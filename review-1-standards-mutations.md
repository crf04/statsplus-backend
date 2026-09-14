# Standards review mutation ledger

- Review checkout: review-1-standards
- Snapshot: 88787c98aa7aa7b2a2bdaa8f326264713d756108
- Base: ce9c7e33eab4de80ec1f5cddfd5145456a9e1851
- Diff: `git diff ce9c7e33eab4de80ec1f5cddfd5145456a9e1851 HEAD`
- Assigned new/modified test IDs: `tests/services/test_data_service.py::test_hosted_metadata_requires_every_non_offline_replacement_stream` (one test; no parameter variants).
- Baseline: `.venv/bin/python -m pytest tests/services/test_data_service.py -q` — 22 passed.
- Python 3.11.9; pre-provisioned shared dependencies, isolated source checkout. Bootstrap omitted to avoid modifying shared implementation dependencies. Full gate belongs to implementation and was not run here.
- Each mutant changes production code only; tests remain untouched. Restore exact source bytes before the next mutant.

## M1 omitted required frame: KILLED

Omit player_per36 from hosted preflight while its collector remains: violates child #273 implementation requirement 2 and the test coverage invariant.

Covered ID: `tests/services/test_data_service.py::test_hosted_metadata_requires_every_non_offline_replacement_stream`.

Command: `.venv/bin/python -m pytest tests/services/test_data_service.py::test_hosted_metadata_requires_every_non_offline_replacement_stream -q`

Exit: 1. Restored exact source bytes; SHA-256: `123fa190cb1232897bb8c2b9e947d43e14103830128c124c2d2bfe43b93982bd`.

```text
============================= test session starts ==============================
platform darwin -- Python 3.11.9, pytest-9.1.1, pluggy-1.6.0
rootdir: /privatereview-1-standards
configfile: pytest.ini
plugins: mock-3.15.1, cov-7.1.0, xdist-3.8.0, anyio-4.14.2
collected 1 item

tests/services/test_data_service.py F                                    [100%]

=================================== FAILURES ===================================
______ test_hosted_metadata_requires_every_non_offline_replacement_stream ______
tests/services/test_data_service.py:519: in test_hosted_metadata_requires_every_non_offline_replacement_stream
    assert set(HOSTED_METADATA_REQUIRED_TABLE_STREAMS) == frames
E   AssertionError: assert {'opp_shootin...ooting_zones'} == {'opp_shootin...ooting_zones'}
E     
E     Extra items in the right set:
E     'player_per36_stats'
E     Use -v to get more diff
=========================== short test summary info ============================
FAILED tests/services/test_data_service.py::test_hosted_metadata_requires_every_non_offline_replacement_stream
============================== 1 failed in 1.00s ===============================

```

## M2 wrong replacement stream: KILLED

Point the shooting-zone preflight at the wrong activation stream while leaving the owning fence correct: violates the same required four-stream contract.

Covered ID: `tests/services/test_data_service.py::test_hosted_metadata_requires_every_non_offline_replacement_stream`.

Command: `.venv/bin/python -m pytest tests/services/test_data_service.py::test_hosted_metadata_requires_every_non_offline_replacement_stream -q`

Exit: 1. Restored exact source bytes; SHA-256: `123fa190cb1232897bb8c2b9e947d43e14103830128c124c2d2bfe43b93982bd`.

```text
============================= test session starts ==============================
platform darwin -- Python 3.11.9, pytest-9.1.1, pluggy-1.6.0
rootdir: /privatereview-1-standards
configfile: pytest.ini
plugins: mock-3.15.1, cov-7.1.0, xdist-3.8.0, anyio-4.14.2
collected 1 item

tests/services/test_data_service.py F                                    [100%]

=================================== FAILURES ===================================
______ test_hosted_metadata_requires_every_non_offline_replacement_stream ______
tests/services/test_data_service.py:520: in test_hosted_metadata_requires_every_non_offline_replacement_stream
    assert HOSTED_METADATA_REQUIRED_TABLE_STREAMS == {
E   AssertionError: assert {'player_per3...tions_season'} == {'player_per3...tions_season'}
E     
E     Omitting 3 identical items, use -vv to show
E     Differing items:
E     {'player_shooting_zones': 'synergy_play_types'} != {'player_shooting_zones': 'exact_shot_zones'}
E     Use -v to get more diff
=========================== short test summary info ============================
FAILED tests/services/test_data_service.py::test_hosted_metadata_requires_every_non_offline_replacement_stream
============================== 1 failed in 0.37s ===============================

```

## M3 unaccounted provider collector: KILLED

Reintroduce a provider-backed collector without extending hosted prerequisites: precisely the future collector drift named in the new test docstring.

Covered ID: `tests/services/test_data_service.py::test_hosted_metadata_requires_every_non_offline_replacement_stream`.

Command: `.venv/bin/python -m pytest tests/services/test_data_service.py::test_hosted_metadata_requires_every_non_offline_replacement_stream -q`

Exit: 1. Restored exact source bytes; SHA-256: `123fa190cb1232897bb8c2b9e947d43e14103830128c124c2d2bfe43b93982bd`.

```text
============================= test session starts ==============================
platform darwin -- Python 3.11.9, pytest-9.1.1, pluggy-1.6.0
rootdir: /privatereview-1-standards
configfile: pytest.ini
plugins: mock-3.15.1, cov-7.1.0, xdist-3.8.0, anyio-4.14.2
collected 1 item

tests/services/test_data_service.py F                                    [100%]

=================================== FAILURES ===================================
______ test_hosted_metadata_requires_every_non_offline_replacement_stream ______
tests/services/test_data_service.py:519: in test_hosted_metadata_requires_every_non_offline_replacement_stream
    assert set(HOSTED_METADATA_REQUIRED_TABLE_STREAMS) == frames
E   AssertionError: assert {'opp_shootin...ooting_zones'} == {'opp_shootin...ooting_zones'}
E     
E     Extra items in the right set:
E     'player_play_types'
E     Use -v to get more diff
=========================== short test summary info ============================
FAILED tests/services/test_data_service.py::test_hosted_metadata_requires_every_non_offline_replacement_stream
============================== 1 failed in 0.35s ===============================

```

## Final verification

- `.venv/bin/python -m pytest tests/services/test_data_service.py -q`: 22 passed after restoration (0.39s).
- All three mutants killed through meaningful assertions at test lines 519 or 520; zero survivors. One assigned regression test covered; no parameter variants.
- Exact production-source bytes equal captured HEAD. HEAD unchanged at 88787c98aa7aa7b2a2bdaa8f326264713d756108.
- Final `git status --short`: empty output; clean tracked files and no untracked files.
- No implementation fixes, commits, agents, publishing, deployment, or changes to the implementation checkout.
- Standards review: no material findings against AGENTS.md, current shared guide, CONTRIBUTING.md, relevant architecture/API contracts, and supplied smell baseline. Nightly mutation checks belong to the separate Spec reviewer; full gate and production acceptance not claimed.
