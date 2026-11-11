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


def test_the_team_view_with_without_carries_the_lineup_too(client, seeded):
    response = client.get(f"{ROTATION}?team=OKC&without=Unit%20One")

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
