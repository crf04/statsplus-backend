"""Rebound chances and potential assists: the provider, the collector and its reruns.

The NBA.com documents are recorded subsets of the real responses for
ORL @ BOS (game 0022501186) and TOR vs BKN (game 0022501192) on 2026-04-12; only
the network boundary is replaced.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from app.providers.nba_tracking import NBATrackingProvider
from app.services.player_tracking_collection import PlayerTrackingCollectionService
from app.services.player_tracking_repository import PlayerTrackingRepository
from app.utils.telemetry import ProviderResponseError

FIXTURES = Path("tests/fixtures/nba_tracking")
SEASON = "2025-26"
BOS_GAME, TOR_GAME = "0022501186", "0022501192"
DAY = date(2026, 4, 12)
SCHEIERMAN, GARZA, CARTER, PRITCHARD, BANCHERO = 1631248, 1630568, 1628975, 1630202, 1631094
WALTER, BATTLE, BARNES, SMITH, CLAXTON = 1642266, 1642419, 1630567, 1641869, 1629651


def _document(name):
    return json.loads((FIXTURES / name).read_text())


def _box(game_id):
    return _document(f"boxscoreplayertrackv3_{game_id}.json")


PASSING = _document("leaguedashptstats_passing_2026-04-12.json")


class FakeEndpoint:
    """Stands in for an ``nba_api`` endpoint class and records how it was called."""

    def __init__(self, documents, calls, key):
        self.documents, self.calls, self.key = documents, calls, key

    def __call__(self, **kwargs):
        self.calls.append((self.key, kwargs))
        outcome = self.documents.pop(0) if len(self.documents) > 1 else self.documents[0]
        if isinstance(outcome, Exception):
            raise outcome
        return type("Response", (), {"get_dict": staticmethod(lambda: copy.deepcopy(outcome))})()


@pytest.fixture
def nba_api(monkeypatch):
    """Patch both endpoint classes; ``boxes[game_id]`` and ``passing`` list the replies in order."""

    from nba_api.stats import endpoints

    Tracking = SimpleNamespace(
        boxes={BOS_GAME: [_box(BOS_GAME)], TOR_GAME: [_box(TOR_GAME)]},
        passing=[PASSING],
        calls=[],
    )

    def box(**kwargs):
        return FakeEndpoint(Tracking.boxes[kwargs["game_id"]], Tracking.calls, "box")(**kwargs)

    def day(**kwargs):
        return FakeEndpoint(Tracking.passing, Tracking.calls, "day")(**kwargs)

    monkeypatch.setattr(endpoints, "BoxScorePlayerTrackV3", box)
    monkeypatch.setattr(endpoints, "LeagueDashPtStats", day)
    return Tracking


def _provider():
    return NBATrackingProvider(backoff_seconds=0, sleep=lambda _seconds: None)


def _event(game_id, scheduled="2026-04-12T23:00:00Z", **extra):
    return {
        "nba_game_id": game_id,
        "classification": "Regular Season",
        "status_code": 3,
        "status_text": "Final",
        "scheduled_at": scheduled,
        "postponed_status": None,
        "postponement_evidence": None,
        **extra,
    }


def _service(tmp_path, events, nba_api):
    engine = create_engine(f"sqlite:///{tmp_path / 'tracking.sqlite3'}")
    run_migrations(engine)
    repository = PlayerTrackingRepository(engine)
    ticks = iter(range(10_000))
    service = PlayerTrackingCollectionService(
        _provider(),
        repository,
        lambda season: events,
        clock=lambda: datetime(2026, 4, 13, 10, tzinfo=UTC) + timedelta(minutes=next(ticks)),
        sleep=lambda _seconds: None,
    )
    return service, repository, engine


def _rows(engine):
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT game_id, player_id, reb_chances, oreb_chances, dreb_chances, potential_ast, "
                    "game_date, fetched_at FROM player_game_tracking ORDER BY game_id, player_id"
                )
            )
        ]


def test_the_provider_reads_the_recorded_box_score_and_day_feed(nba_api):
    box = _provider().fetch_game_rebound_chances(TOR_GAME)
    day = _provider().fetch_day_potential_assists(SEASON, DAY)

    assert box[WALTER] == {"minutes": 1447, "total": 2, "offensive": 0, "defensive": 2}
    assert box[BATTLE]["total"] == 0 and box[BATTLE]["minutes"] == 318
    assert box[CLAXTON] == {"minutes": 0, "total": 0, "offensive": 0, "defensive": 0}
    assert day[WALTER] == 2
    assert day[BATTLE] == 0
    assert CLAXTON not in day


def test_requests_send_the_standard_nba_api_headers_and_the_game_day(nba_api):
    from nba_api.stats.library.http import STATS_HEADERS

    _provider().fetch_game_rebound_chances(TOR_GAME)
    _provider().fetch_day_potential_assists(SEASON, DAY)

    (_, box_call), (_, day_call) = nba_api.calls
    assert box_call["headers"] == STATS_HEADERS and day_call["headers"] == STATS_HEADERS
    assert day_call["pt_measure_type"] == "Passing"
    assert day_call["per_mode_simple"] == "Totals"
    assert day_call["season_type_all_star"] == "Regular Season"
    assert (day_call["date_from_nullable"], day_call["date_to_nullable"]) == ("04/12/2026", "04/12/2026")


def test_an_empty_body_and_a_timeout_are_retried_then_succeed(nba_api):
    nba_api.boxes[TOR_GAME] = [
        json.JSONDecodeError("Expecting value", "", 0),
        requests.Timeout("slow"),
        _box(TOR_GAME),
    ]

    box = _provider().fetch_game_rebound_chances(TOR_GAME)

    assert box[WALTER]["total"] == 2
    assert len([call for call in nba_api.calls if call[0] == "box"]) == 3


def test_a_feed_that_keeps_failing_raises_after_the_last_attempt(nba_api):
    nba_api.boxes[TOR_GAME] = [json.JSONDecodeError("Expecting value", "", 0)]

    with pytest.raises(ProviderResponseError):
        _provider().fetch_game_rebound_chances(TOR_GAME)

    assert len(nba_api.calls) == 4


def test_collecting_stores_the_recorded_values_and_zero_rebound_players_keep_their_chances(tmp_path, nba_api):
    service, _repository, engine = _service(tmp_path, [_event(BOS_GAME), _event(TOR_GAME)], nba_api)

    result = service.collect(SEASON)

    assert (result["stored"], result["not_published"], result["failed"], result["remaining"]) == (
        2,
        [],
        [],
        0,
    )
    by_player = {(row[0], row[1]): row[2:6] for row in _rows(engine)}
    # (reb_chances, oreb_chances, dreb_chances, potential_ast)
    assert by_player[(TOR_GAME, WALTER)] == (2, 0, 2, 2)
    assert by_player[(BOS_GAME, SCHEIERMAN)] == (12, 0, 12, 15)
    assert by_player[(BOS_GAME, GARZA)] == (27, 13, 14, 4)
    # A real zero potential assist is stored as 0, and a player the day feed lacks as NULL.
    assert by_player[(BOS_GAME, CARTER)][3] == 0
    assert by_player[(TOR_GAME, BATTLE)][3] == 0
    assert by_player[(BOS_GAME, PRITCHARD)] == (0, 0, 0, None)
    assert by_player[(TOR_GAME, CLAXTON)] == (0, 0, 0, None)
    assert {row[6] for row in _rows(engine)} == {"2026-04-12"}


def test_the_day_feed_is_fetched_once_per_game_day(tmp_path, nba_api):
    service, _repository, _engine = _service(tmp_path, [_event(BOS_GAME), _event(TOR_GAME)], nba_api)

    service.collect(SEASON)

    assert [key for key, _kwargs in nba_api.calls].count("day") == 1


def test_running_twice_changes_nothing(tmp_path, nba_api):
    service, _repository, engine = _service(tmp_path, [_event(BOS_GAME), _event(TOR_GAME)], nba_api)
    service.collect(SEASON)
    first = _rows(engine)
    calls_after_first = len(nba_api.calls)

    second = service.collect(SEASON)

    assert _rows(engine) == first
    assert (second["candidates"], second["stored"]) == (0, 0)
    assert len(nba_api.calls) == calls_after_first


def test_rereading_a_named_game_leaves_identical_rows_including_fetched_at(tmp_path, nba_api):
    service, _repository, engine = _service(tmp_path, [_event(BOS_GAME)], nba_api)
    service.collect(SEASON)
    first = _rows(engine)

    again = service.collect(SEASON, game_ids=[BOS_GAME])

    assert (again["stored"], again["unchanged"]) == (0, 1)
    assert _rows(engine) == first


def test_a_game_with_no_tracking_yet_is_collected_on_the_next_run(tmp_path, nba_api):
    unpublished = copy.deepcopy(_box(TOR_GAME))
    for side in ("homeTeam", "awayTeam"):
        for player in unpublished["boxScorePlayerTrack"][side]["players"]:
            player["statistics"].update(minutes="", reboundChancesTotal=0)
    nba_api.boxes[TOR_GAME] = [unpublished, _box(TOR_GAME)]
    service, _repository, engine = _service(tmp_path, [_event(TOR_GAME)], nba_api)

    first = service.collect(SEASON)

    assert first["not_published"] == [TOR_GAME] and first["stored"] == 0
    assert _rows(engine) == []

    second = service.collect(SEASON)

    assert second["stored"] == 1
    assert {(row[1], row[2]) for row in _rows(engine)} >= {(WALTER, 2)}


def test_a_game_the_day_feed_does_not_list_yet_waits_for_a_later_run(tmp_path, nba_api):
    empty = copy.deepcopy(PASSING)
    empty["resultSets"][0]["rowSet"] = []
    nba_api.passing[:] = [empty, PASSING]
    service, _repository, engine = _service(tmp_path, [_event(TOR_GAME)], nba_api)

    assert service.collect(SEASON)["not_published"] == [TOR_GAME]
    assert _rows(engine) == []

    assert service.collect(SEASON)["stored"] == 1
    assert {row[5] for row in _rows(engine) if row[1] == WALTER} == {2}


def test_a_failed_fetch_is_reported_and_does_not_block_the_next_game(tmp_path, nba_api):
    nba_api.boxes[BOS_GAME] = [requests.ConnectionError("no route")]
    service, _repository, engine = _service(tmp_path, [_event(BOS_GAME), _event(TOR_GAME)], nba_api)

    result = service.collect(SEASON)

    assert [item["game_id"] for item in result["failed"]] == [BOS_GAME]
    assert result["stored_game_ids"] == [TOR_GAME]
    assert {row[0] for row in _rows(engine)} == {TOR_GAME}


def test_only_completed_regular_season_games_are_candidates_and_max_games_bounds_a_run(tmp_path, nba_api):
    events = [
        _event(BOS_GAME),
        _event(TOR_GAME),
        _event("0042500101", classification="Playoffs"),
        _event("0022501999", status_code=1, status_text="7:00 PM ET"),
    ]
    service, _repository, _engine = _service(tmp_path, events, nba_api)

    result = service.collect(SEASON, max_games=1)

    assert (result["candidates"], result["attempted"], result["remaining"]) == (2, 1, 1)
    with pytest.raises(ValueError, match="0042500101"):
        service.collect(SEASON, game_ids=["0042500101"])


def test_the_game_date_is_the_eastern_date_of_the_tip(tmp_path, nba_api):
    # 00:30 UTC on the 13th is 8:30 PM Eastern on the 12th.
    service, _repository, engine = _service(
        tmp_path, [_event(TOR_GAME, scheduled="2026-04-13T00:30:00Z")], nba_api
    )

    service.collect(SEASON)

    assert {row[6] for row in _rows(engine)} == {"2026-04-12"}
    day_call = next(kwargs for key, kwargs in nba_api.calls if key == "day")
    assert day_call["date_from_nullable"] == "04/12/2026"


def test_the_refresh_script_runs_the_collector_for_each_season_and_exits_1_on_a_failure(
    tmp_path, monkeypatch
):
    from scripts import tracking_refresh

    seen = []

    class Service:
        def collect(self, season, **kwargs):
            seen.append((season, kwargs))
            return {"season": season, "failed": [{"game_id": "x"}] if season == "2026-27" else []}

    dependencies = type(
        "Dependencies",
        (),
        {"player_tracking_collection_service": Service(), "engine": create_engine("sqlite://")},
    )()
    monkeypatch.setattr("app.dependencies.build_dependencies", lambda settings: dependencies)
    monkeypatch.setattr("app.migrations.run_migrations", lambda engine: None)

    code = tracking_refresh.main(
        [
            "--season", "2025-26", "--season", "2026-27",
            "--database-url", f"sqlite:///{tmp_path / 'x.sqlite3'}",
            "--max-games", "5", "--throttle", "0",
        ]
    )  # fmt: skip

    assert code == 1
    assert seen == [
        ("2025-26", {"max_games": 5, "throttle_seconds": 0.0, "game_ids": None}),
        ("2026-27", {"max_games": 5, "throttle_seconds": 0.0, "game_ids": None}),
    ]
