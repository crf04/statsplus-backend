"""``lineup`` on the team view of ``GET /api/games/rotation`` (crf04/statsplus#492)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.rotation_service import RotationService
from app.services.team_lineups import TeamLineupRepository, collect_team_lineups
from tests.support.game_context_world import schedule
from tests.support.rotation_world import Catalog, StoredLogs, game, line, migrated_repository, store

ROTATION = "/api/games/rotation"
RECORDED = json.loads(Path("tests/fixtures/nba_daily_lineups/00_daily_lineups_20261110.json").read_text())
LATE_EVENING = datetime(2026, 11, 11, 3, 30, tzinfo=UTC)


@pytest.fixture
def seeded(tmp_path, dependencies):
    repository = migrated_repository(tmp_path)
    store(
        repository,
        game(
            "0022600100",
            "2026-11-01",
            "OKC",
            "DEN",
            [line(1, "Unit One", "OKC", minutes=30, started=True)],
        ),
        season="2026-27",
    )
    schedule(
        repository.engine,
        "0022600216",
        "2026-11-11T04:00:00+00:00",
        (1610612760, "OKC"),
        (1610612757, "POR"),
        season="2026-27",
    )
    collect_team_lineups(repository.engine, fetch=lambda _day: RECORDED, clock=lambda: LATE_EVENING)
    dependencies.rotation_service = RotationService(
        repository,
        Catalog((1, "Unit One", "OKC")),
        lambda: "2026-27",
        StoredLogs(),
        lineups=TeamLineupRepository(repository.engine, clock=lambda: LATE_EVENING),
    )
    return dependencies


def test_the_team_view_carries_tonights_lineup_beside_its_past_starters(client, seeded):
    response = client.get(f"{ROTATION}?team=okc&last_n=1")

    assert response.status_code == 200
    body = response.get_json()
    assert body["games"] == [{"game_date": "2026-11-01", "matchup": "OKC vs. DEN", "starters": ["Unit One"]}]
    lineup = body["lineup"]
    assert {key: lineup[key] for key in ("status", "game_id", "game_date", "matchup", "feed_timestamp")} == {
        "status": "posted",
        "game_id": "0022600216",
        "game_date": "2026-11-10",
        "matchup": "OKC @ POR",
        "feed_timestamp": "2026-11-10T22:29:43-05:00",
    }
    assert lineup["starters"][2] == {
        "player_id": 1628983,
        "name": "Shai Gilgeous-Alexander",
        "position": "PG",
        "lineup_status": "Expected",
    }
    assert lineup["inactive"][0] == {"player_id": 1631096, "name": "Chet Holmgren"}


def test_a_date_with_no_game_answers_no_game(client, seeded):
    response = client.get(f"{ROTATION}?team=OKC&date=2026-11-09")

    assert response.status_code == 200
    assert response.get_json()["lineup"]["status"] == "no_game"


@pytest.mark.parametrize("split", ["without=Unit%20One", "with=Unit%20One"])
def test_the_split_team_views_carry_the_lineup_too(client, seeded, split):
    response = client.get(f"{ROTATION}?team=OKC&{split}")

    assert response.status_code == 200
    assert response.get_json()["lineup"]["status"] == "posted"


@pytest.mark.parametrize(
    "query",
    [
        "team=OKC&date=2026-11-31",
        "team=OKC&date=20261110",
        "team=OKC&date=2026-11-10&date=2026-11-11",
        "player_name=Unit%20One&date=2026-11-10",
        "game_id=0022600216&date=2026-11-10",
    ],
)
def test_a_bad_or_misplaced_date_is_refused(client, seeded, query):
    response = client.get(f"{ROTATION}?{query}")

    assert response.status_code == 400
    assert response.get_json()["error"]["details"] == {"parameters": ["date"]}


NO_HISTORY_EXITS = [
    "",
    "&without=Unit%20One",
    "&with=Unit%20One",
    "&season=2027-28",
    "&last_n=3&season=2026-27",
]


@pytest.mark.parametrize("extra", NO_HISTORY_EXITS)
def test_a_team_without_stored_rotations_still_reports_its_lineup_in_the_404(client, seeded, extra):
    response = client.get(f"{ROTATION}?team=POR{extra}")

    assert response.status_code == 404
    details = response.get_json()["error"]["details"]
    assert details["reason"] == "no_rotation_data"
    assert (details["lineup"]["status"], details["lineup"]["matchup"]) == ("posted", "POR vs. OKC")
    assert details["lineup"]["starters"][0]["lineup_status"] == "Expected"


@pytest.mark.parametrize("extra", NO_HISTORY_EXITS)
def test_the_404_lineup_follows_the_asked_date(client, seeded, extra):
    response = client.get(f"{ROTATION}?team=POR&date=2026-11-09{extra}")

    assert response.status_code == 404
    assert response.get_json()["error"]["details"]["lineup"]["status"] == "no_game"


def test_the_production_wiring_serves_the_stored_lineup(client, dependencies, tmp_path, monkeypatch):
    from unittest.mock import Mock

    from app.config.settings import RuntimeSettings
    from app.dependencies import build_dependencies

    repository = migrated_repository(tmp_path)
    store(
        repository,
        game(
            "0022600100", "2026-11-01", "OKC", "DEN", [line(1, "Unit One", "OKC", minutes=30, started=True)]
        ),
        season="2026-27",
    )
    schedule(
        repository.engine,
        "0022600216",
        "2026-11-11T04:00:00+00:00",
        (1610612760, "OKC"),
        (1610612757, "POR"),
        season="2026-27",
    )
    collect_team_lineups(repository.engine, fetch=lambda _day: RECORDED, clock=lambda: LATE_EVENING)
    monkeypatch.setattr("app.utils.db.get_engine", Mock(return_value=repository.engine))
    monkeypatch.setattr("app.utils.cache_config.get_redis_client", Mock(return_value=None))
    built = build_dependencies(
        RuntimeSettings(
            environment="testing",
            auth={"firebase_admin_disabled": True},
            database={"url": "sqlite:///unused.sqlite3"},
        )
    )
    dependencies.rotation_service = built.rotation_service

    response = client.get(f"{ROTATION}?team=OKC&season=2026-27&date=2026-11-10")

    assert response.status_code == 200
    lineup = response.get_json()["lineup"]
    assert (lineup["status"], lineup["game_id"], len(lineup["starters"])) == ("posted", "0022600216", 5)
