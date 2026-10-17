"""Nightly/backfill rotation collection, with LiveData served from fixtures."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import requests

from app.services.rotation_collection import RotationCollectionService
from app.utils.telemetry import ProviderResponseError
from tests.support.rotation_world import SEASON, migrated_repository

FIXTURES = Path("tests/fixtures/nba_live_data")
OVERTIME_ID = "0022500123"
BETWEEN_ID = "0022500500"


def _document(kind, name):
    return json.loads((FIXTURES / f"{kind}_{name}.json").read_text())


class FakeLiveData:
    """Serves recorded documents by game id; anything else is a fetch failure."""

    def __init__(self):
        self.documents = {
            OVERTIME_ID: (_document("boxscore", "overtime"), _document("playbyplay", "overtime")),
            BETWEEN_ID: (_document("boxscore", "between_period"), _document("playbyplay", "between_period")),
            "0022500777": (
                _document("boxscore", "between_period")
                | {"game": {**_document("boxscore", "between_period")["game"], "gameId": "0022500777"}},
                _document("playbyplay", "corrupted")
                | {"game": {**_document("playbyplay", "corrupted")["game"], "gameId": "0022500777"}},
            ),
        }
        self.fetched = []

    def fetch_game_stats(self, game_id, season):
        self.fetched.append(game_id)
        if game_id not in self.documents:
            raise requests.ConnectionError("no route to host")
        return copy.deepcopy(self.documents[game_id][0])

    def fetch_play_by_play(self, game_id):
        if game_id == "0022500888":
            raise ProviderResponseError("NBA LiveData play-by-play is missing the game's actions.")
        return copy.deepcopy(self.documents[game_id][1])


def _event(
    game_id,
    *,
    classification="Regular Season",
    status_code=3,
    status_text="Final",
    scheduled="2025-10-28T23:00:00Z",
    postponed=None,
):
    return {
        "nba_game_id": game_id,
        "classification": classification,
        "status_code": status_code,
        "status_text": status_text,
        "scheduled_at": scheduled,
        "postponed_status": postponed,
        "postponement_evidence": None,
    }


def _ticking_clock():
    ticks = iter(range(10_000))
    return lambda: datetime(2026, 10, 21, tzinfo=UTC) + timedelta(minutes=next(ticks))


def _service(tmp_path, events, provider=None, sleeps=None):
    repository = migrated_repository(tmp_path)
    provider = provider or FakeLiveData()
    service = RotationCollectionService(
        provider,
        repository,
        lambda season: events,
        clock=_ticking_clock(),
        sleep=(sleeps.append if sleeps is not None else (lambda seconds: None)),
    )
    return service, repository, provider


def test_collects_only_completed_regular_season_games_not_yet_stored(tmp_path):
    events = [
        _event(OVERTIME_ID, scheduled="2025-10-28T23:00:00Z"),
        _event(BETWEEN_ID, scheduled="2026-01-05T02:00:00Z"),
        _event("0012500001", classification="Preseason"),
        _event("0022500901", status_code=1, status_text="7:00 pm ET"),
        _event("0022500902", postponed="Postponed", status_text="Postponed", status_code=1),
        _event("0042500101", classification="Playoffs"),
    ]
    service, repository, provider = _service(tmp_path, events)

    summary = service.collect(SEASON)

    assert summary == {
        "season": SEASON,
        "candidates": 2,
        "attempted": 2,
        "stored": 2,
        "stored_game_ids": [OVERTIME_ID, BETWEEN_ID],
        "rejected": [],
        "failed": [],
        "given_up": [],
        "remaining": 0,
    }
    assert provider.fetched == [OVERTIME_ID, BETWEEN_ID]
    assert repository.stored_game_ids(SEASON) == {OVERTIME_ID, BETWEEN_ID}


def test_a_rerun_skips_stored_games_and_leaves_identical_rows(tmp_path):
    events = [_event(OVERTIME_ID), _event(BETWEEN_ID, scheduled="2026-01-05T02:00:00Z")]
    service, repository, provider = _service(tmp_path, events)
    service.collect(SEASON)
    with repository.engine.connect() as connection:
        from sqlalchemy import text

        before = [tuple(r) for r in connection.execute(text("SELECT * FROM rotation_stints ORDER BY 1,2,3"))]
    provider.fetched.clear()

    summary = service.collect(SEASON)

    assert summary["candidates"] == summary["attempted"] == summary["stored"] == 0
    assert provider.fetched == []
    service.collect(SEASON, game_ids=[OVERTIME_ID, BETWEEN_ID])
    with repository.engine.connect() as connection:
        after = [tuple(r) for r in connection.execute(text("SELECT * FROM rotation_stints ORDER BY 1,2,3"))]
    assert after == before


def test_a_rejected_or_unfetchable_game_is_reported_and_blocks_no_other(tmp_path):
    events = [
        _event("0022500777", scheduled="2025-10-01T00:00:00Z"),  # corrupted feed
        _event("0022500666", scheduled="2025-10-02T00:00:00Z"),  # unreachable
        _event("0022500888", scheduled="2025-10-03T00:00:00Z"),  # bad play-by-play
        _event(OVERTIME_ID, scheduled="2025-10-28T23:00:00Z"),
    ]
    service, repository, _ = _service(tmp_path, events)

    summary = service.collect(SEASON)

    assert summary["stored"] == 1
    assert summary["stored_game_ids"] == [OVERTIME_ID]
    assert summary["rejected"] == [
        {
            "game_id": "0022500777",
            "reason": "MIL has 4 on court at period action 140, not 5",
        }
    ]
    assert [item["game_id"] for item in summary["failed"]] == ["0022500666", "0022500888"]
    assert summary["failed"][0]["error"] == "no route to host"
    assert repository.stored_game_ids(SEASON) == {OVERTIME_ID}


def test_max_games_bounds_a_run_and_reports_what_remains(tmp_path):
    events = [_event(OVERTIME_ID), _event(BETWEEN_ID, scheduled="2026-01-05T02:00:00Z")]
    service, repository, _ = _service(tmp_path, events)

    first = service.collect(SEASON, max_games=1)
    second = service.collect(SEASON, max_games=1)

    assert (first["stored_game_ids"], first["remaining"]) == ([OVERTIME_ID], 1)
    assert (second["stored_game_ids"], second["remaining"]) == ([BETWEEN_ID], 0)


def test_the_throttle_pauses_between_games_but_not_before_the_first(tmp_path):
    events = [_event(OVERTIME_ID), _event(BETWEEN_ID, scheduled="2026-01-05T02:00:00Z")]
    sleeps = []
    service, _, _ = _service(tmp_path, events, sleeps=sleeps)

    service.collect(SEASON, throttle_seconds=1.5)

    assert sleeps == [1.5]


def test_explicit_game_ids_must_be_regular_season_ids(tmp_path):
    service, _, _ = _service(tmp_path, [])

    with pytest.raises(ValueError, match="not regular-season game ids: 0012500001"):
        service.collect(SEASON, game_ids=["0012500001"])


def test_persistently_rejected_games_cannot_starve_newer_ones_and_are_given_up(tmp_path):
    events = [
        _event("0022500777", scheduled="2025-10-01T00:00:00Z"),  # always rejected
        _event(OVERTIME_ID, scheduled="2025-10-28T23:00:00Z"),
    ]
    service, repository, _ = _service(tmp_path, events)

    runs = [service.collect(SEASON, max_games=1) for _ in range(5)]

    # The oldest game is rejected first, but the very next run reaches the
    # newer one (never-tried games go first); the old game is then retried
    # twice more and given up.
    assert [r["stored_game_ids"] for r in runs] == [[], [OVERTIME_ID], [], [], []]
    assert [len(r["rejected"]) for r in runs] == [1, 0, 1, 1, 0]
    assert runs[-1]["given_up"] == [
        {
            "game_id": "0022500777",
            "rejections": 3,
            "reason": "MIL has 4 on court at period action 140, not 5",
        }
    ]
    assert runs[-1]["candidates"] == 0
    assert repository.stored_game_ids(SEASON) == {OVERTIME_ID}


def test_a_game_that_always_fails_to_fetch_queues_behind_untried_games(tmp_path):
    events = [
        _event("0022500666", scheduled="2025-10-01T00:00:00Z"),  # unreachable
        _event(OVERTIME_ID, scheduled="2025-10-28T23:00:00Z"),
    ]
    service, repository, _ = _service(tmp_path, events)

    first = service.collect(SEASON, max_games=1)
    second = service.collect(SEASON, max_games=1)

    assert [item["game_id"] for item in first["failed"]] == ["0022500666"]
    assert second["stored_game_ids"] == [OVERTIME_ID]
    assert repository.attempts(SEASON)["0022500666"]["rejections"] == 0


def test_fetch_failures_never_use_up_the_rejection_budget(tmp_path):
    events = [_event("0022500666")]
    service, _, _ = _service(tmp_path, events)

    for _ in range(6):
        summary = service.collect(SEASON)

    assert [item["game_id"] for item in summary["failed"]] == ["0022500666"]
    assert summary["given_up"] == []


def test_a_forced_retry_collects_a_given_up_game_and_clears_its_record(tmp_path):
    events = [_event(BETWEEN_ID, scheduled="2026-01-05T02:00:00Z")]
    provider = FakeLiveData()
    good = provider.documents[BETWEEN_ID]
    provider.documents[BETWEEN_ID] = (
        good[0],
        _document("playbyplay", "corrupted")
        | {"game": {**_document("playbyplay", "corrupted")["game"], "gameId": BETWEEN_ID}},
    )
    service, repository, _ = _service(tmp_path, events, provider=provider)
    for _ in range(3):
        service.collect(SEASON)
    assert service.collect(SEASON)["given_up"][0]["game_id"] == BETWEEN_ID

    provider.documents[BETWEEN_ID] = good
    forced = service.collect(SEASON, game_ids=[BETWEEN_ID])

    assert forced["stored_game_ids"] == [BETWEEN_ID]
    assert repository.attempts(SEASON) == {}
    assert service.collect(SEASON)["given_up"] == []
