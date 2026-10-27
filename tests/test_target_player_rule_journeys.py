"""Position and Average Targets through the real app, services and a seeded database.

The Target services are the production ones, built by ``build_dependencies``
over a database holding real Publications (game logs and Diets), a real athlete
catalog and a real account.  Only the Slate and the composed Matchup, which
need a day's schedule and pool, are replaced, so the wiring that hands the
catalog and the season logs to Backtest, preview and resolve is what these
journeys exercise.  Every assertion is a literal player, Position, count or
title.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from app import create_app
from app.config.settings import load_settings
from app.dependencies import build_dependencies
from app.models.athlete_catalog import AthleteCatalog
from tests import test_target_backtest as bt
from tests import test_target_resolution as res
from tests.services import test_backtest_seasons as seasons
from tests.services.test_backtest_seasons import LAST, NOW, OWNER

LEBRON, EMBIID = bt.LEBRON, bt.EMBIID
GHOST = 777001  # on tonight's Lakers pool, with no log and no catalog row
WING = {"base": "position", "groups": ["Wing"]}
BIG = {"base": "position", "groups": ["Big"]}
WING_BIG = {"base": "position", "groups": ["Wing", "Big"]}


def _average(stat, threshold, comparator="at_or_above"):
    return {"base": "season_average", "stat": stat, "comparator": comparator, "threshold": threshold}


def catalog_row(player_id, name, team_id, position, *, published_at=NOW):
    return {
        "season": LAST,
        "player_id": player_id,
        "display_name": name,
        "roster_status": "active",
        "is_active": True,
        "is_active_for_season": True,
        "team_id": team_id,
        "team_name": "Team",
        "team_abbreviation": "TM",
        "position": position,
        "published_at": published_at,
    }


class FakeComposer:
    """Tonight's Matchup document, standing in for the pool-and-schedule compose."""

    def __init__(self, document):
        self.document = document

    def get_matchup_from_snapshot(self, *, game_id, publication_snapshot, injuries):
        return self.document

    def get_matchup(self, *, game_id, **_kwargs):
        return self.document


def tonight():
    """LeBron (Lakers, 22 ppg) and a Lakers player nothing is stored for."""

    document = res._matchup(
        players=[
            res._player(LEBRON, "LeBron James", shot_zones=res._zone_diet(0.42, 0.2), season_scoring=22.0),
            res._player(GHOST, "Pool Ghost", shot_zones=res._zone_diet(0.5, 0.2), season_scoring=5.0),
        ]
    )
    document["as_of"] = {"season": LAST}
    return document


@pytest.fixture
def journey(tmp_path, monkeypatch, authenticate, make_db_user):
    from app.utils.db import get_engine

    url = f"sqlite:///{tmp_path / 'journey.sqlite3'}"
    get_engine.cache_clear()
    engine = create_engine(url)
    world = seasons.World(engine)
    world.publish_season(LAST, okc_games=2, corner_three=0.42)
    with engine.begin() as connection:
        connection.execute(
            AthleteCatalog.__table__.insert(),
            [
                catalog_row(LEBRON, "LeBron James", bt.LAL, "Wing"),
                catalog_row(EMBIID, "Joel Embiid", bt.OKC, None),
            ],
        )
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    monkeypatch.setenv("FLASK_ENV", "testing")
    settings = load_settings(overrides={"DATABASE_URL": url})
    dependencies = build_dependencies(settings)
    # Only the day's schedule and composed Matchup are doubled; the Target
    # services keep the readers production wiring gave them.
    composer = FakeComposer(tonight())
    dependencies.target_resolution_service.slates = res.FakeSlate()
    dependencies.target_resolution_service.matchups = composer
    dependencies.target_preview_service.matchups = composer
    application = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": dependencies,
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )
    client = application.test_client()
    headers = authenticate(claims={"uid": OWNER}, db_user=make_db_user(firebase_uid=OWNER))

    class Journey:
        pass

    built = Journey()
    built.client, built.headers, built.engine = client, headers, engine
    yield built
    engine.dispose()
    get_engine.cache_clear()


def _post(journey, path, body):
    return journey.client.post(path, headers=journey.headers, json=body)


def _preview(journey, opponent, qualifiers):
    response = _post(journey, "/api/user/targets/preview", {"opponent": opponent, "qualifiers": qualifiers})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_preview_lists_the_position_and_average_fits_for_the_season_and_for_tonight(journey):
    body = _preview(journey, "OKC", [WING, _average("3PA", 8)])

    assert body["target"]["title"] == "OKC vs Wing, 3PA avg ≥ 8"
    assert [(p["name"], p["position"]) for p in body["players"]] == [("LeBron James", "Wing")]
    assert body["summary"]["players"] == 1
    assert body["summary"]["players_without_position"] == 0
    assert body["stat_columns"] == ["3PA"]
    assert body["today"]["fit_count"] == 1


def test_preview_counts_the_unlisted_and_drops_the_other_groups(journey):
    assert _preview(journey, "OKC", [BIG])["players"] == []
    assert _preview(journey, "OKC", [BIG])["summary"]["players_without_position"] == 0
    body = _preview(journey, "LAL", [BIG])
    assert body["players"] == []
    assert body["summary"]["players_without_position"] == 1


def test_a_diet_only_target_lists_each_players_position(journey):
    body = _preview(journey, "OKC", [bt.CORNER_THREE])

    assert body["target"]["title"] == "OKC vs Corner 3 ≥ 40%"
    assert [(p["name"], p["position"]) for p in body["players"]] == [("LeBron James", "Wing")]
    assert "players_without_position" not in body["summary"]
    assert body["today"]["fit_count"] == 2


def test_a_saved_target_backtests_with_positions_and_resolves_for_tonight(journey):
    created = _post(
        journey,
        "/api/user/targets",
        {"opponent": "OKC", "qualifiers": [WING_BIG, _average("3PA", 5)]},
    )
    assert created.status_code == 201
    target = created.get_json()["target"]
    assert target["title"] == "OKC vs Wing/Big, 3PA avg ≥ 5"

    backtest = journey.client.get(f"/api/user/targets/{target['id']}/backtest", headers=journey.headers)
    assert backtest.status_code == 200
    backtest = backtest.get_json()
    assert [(p["name"], p["position"]) for p in backtest["players"]] == [("LeBron James", "Wing")]
    assert backtest["summary"]["players_without_position"] == 0

    resolved = journey.client.get(f"/api/user/targets/resolve?date={res.SLATE_DATE}", headers=journey.headers)
    assert resolved.status_code == 200
    [entry] = resolved.get_json()["targets"]
    assert [(p["name"], p["position"], p["thin"]) for p in entry["players"]] == [
        ("LeBron James", "Wing", False)
    ]
    assert [share["base"] for share in entry["players"][0]["shares"]] == ["position", "season_average"]


def test_resolve_marks_a_pool_player_with_no_position_and_never_fits_a_position(journey):
    diet = _post(journey, "/api/user/targets", {"opponent": "OKC", "qualifiers": [bt.CORNER_THREE]})
    big = _post(journey, "/api/user/targets", {"opponent": "OKC", "qualifiers": [BIG]})
    assert diet.status_code == big.status_code == 201

    resolved = journey.client.get(f"/api/user/targets/resolve?date={res.SLATE_DATE}", headers=journey.headers)

    by_title = {entry["target"]["title"]: entry for entry in resolved.get_json()["targets"]}
    assert [(p["name"], p["position"]) for p in by_title["OKC vs Corner 3 ≥ 40%"]["players"]] == [
        ("LeBron James", "Wing"),
        ("Pool Ghost", None),
    ]
    assert by_title["OKC vs Big"]["players"] == []
