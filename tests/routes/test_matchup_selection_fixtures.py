"""Offline endpoint fixtures for stored matchup selection facts."""

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine

from app import create_app
from app.config.settings import (
    AuthenticationSettings,
    CacheSettings,
    NBASeasonSettings,
    RuntimeSettings,
)
from app.migrations import run_migrations
from app.services.player_game_log_repository import (
    PlayerGameLogRecord,
    PlayerGameLogRepository,
)
from app.services.player_pool import PlayerPool, PoolPlayer
from app.services.statistic_catalog import StatisticCatalog
from app.services.matchup_selection import MatchupSelectionService


FIXTURE = Path(__file__).parents[1] / "fixtures/matchup_selection/selected_player.json"
RETRIEVED_AT = datetime(2026, 5, 2, tzinfo=timezone.utc)


class RecordedEventCatalog:
    def __init__(self, game):
        self.game = game

    def get_events(self, season):
        return [{"season": season, **self.game}]


class RecordedPool:
    def __init__(self, player):
        self.player = player

    def get_pool(self, *, season, game_ids):
        return PlayerPool(
            players=(
                PoolPlayer(
                    canonical_player_id=self.player["canonical_player_id"],
                    name=self.player["name"],
                    team_id=self.player["team_id"],
                    market_categories=tuple(self.player["market_categories"]),
                    provenance={"recorded": tuple(self.player["market_categories"])},
                ),
            ),
            team_counts={self.player["team_id"]: 1},
            freshness={
                "status": "fresh",
                "retrieved_at": RETRIEVED_AT.isoformat(),
                "providers": {},
            },
        )


class RecordedArchetypePeers:
    def __init__(self, peer_ids=()):
        self.peer_ids = tuple(peer_ids)

    def list_peer_ids(self, player_id):
        return self.peer_ids


def _client(tmp_path, *, peer_ids=(), pool_player=None):
    fixture = json.loads(FIXTURE.read_text())
    engine = create_engine(f"sqlite:///{tmp_path / 'selection.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    repository = PlayerGameLogRepository(
        engine,
        statistic_catalog=catalog,
        stats_surface_max_age=timedelta(hours=30),
        stats_surface_season=fixture["season"],
        clock=lambda: RETRIEVED_AT,
    )
    records = [
        PlayerGameLogRecord(
            **{
                **row,
                "season": fixture["season"],
                "game_date": date.fromisoformat(row["game_date"]),
            }
        )
        for row in fixture["logs"]
    ]
    repository.publish(
        fixture["season"],
        records,
        retrieved_at=RETRIEVED_AT,
        source_provider="recorded",
        source_row_count=len(records),
    )
    settings = RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
        nba=NBASeasonSettings(current_season=fixture["season"]),
    )
    service = MatchupSelectionService(
        event_catalog=RecordedEventCatalog(fixture["game"]),
        player_pool=RecordedPool(pool_player or fixture["player"]),
        player_logs=repository,
        archetypes=RecordedArchetypePeers(peer_ids),
        statistic_catalog=catalog,
        settings=settings,
    )
    dependencies = SimpleNamespace(
        settings=settings,
        matchup_selection_service=service,
        user_service=SimpleNamespace(create_or_update_user=lambda _user: None),
    )
    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": dependencies,
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )
    return app.test_client(), fixture


def test_selection_uses_regular_season_rate_for_combined_phase_h2h_rows(tmp_path):
    client, fixture = _client(tmp_path)

    response = client.get(
        f"/api/games/matchup/selection?game_id={fixture['game']['nba_game_id']}"
        f"&player_id={fixture['player']['canonical_player_id']}"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["player_id"] == 101
    assert payload["h2h"]["thin"] is False
    assert [row["row_type"] for row in payload["h2h"]["rows"]] == [
        "game",
        "game",
        "average",
    ]
    playoff, regular, average = payload["h2h"]["rows"]
    assert (playoff["game_date"], playoff["matchup"]) == (
        "2026-05-01",
        "AAA @ BBB",
    )
    assert (regular["game_date"], regular["matchup"]) == (
        "2026-01-05",
        "AAA vs. BBB",
    )
    assert average["game_date"] is None
    assert average["matchup"] is None
    assert average["minutes"] == 37.5
    assert average["stats"]["PTS"] == 30.0
    assert playoff["deltas"]["PTS"] == 0.428571
    assert regular["deltas"]["PTS"] == 0.053571
    assert average["deltas"]["PTS"] == 0.228571
    expected_markets = set(fixture["player"]["market_categories"])
    for row in payload["h2h"]["rows"]:
        assert set(row["stats"]) == expected_markets
        assert set(row["deltas"]) == expected_markets
    assert regular["stats"] == {
        "3PM": 3.0,
        "AST": 5.0,
        "BLK": 1.0,
        "FG2A": 12.0,
        "FG3A": 6.0,
        "FGA": 18.0,
        "PA": 30.0,
        "PR": 35.0,
        "PRA": 40.0,
        "PTS": 25.0,
        "RA": 15.0,
        "REB": 10.0,
        "STKS": 3.0,
        "STL": 2.0,
        "TOV": 3.0,
    }
    assert regular["deltas"]["PRA"] == 0.085714
    assert regular["deltas"]["STKS"] == 0.017857
    assert regular["deltas"]["FGA"] == 0.021429
    assert regular["deltas"]["FG3A"] == 0.021429
    assert regular["deltas"]["FG2A"] == 0.0
    assert payload["archetype"] == {"thin": True, "rows": []}


def test_archetype_rows_use_each_sample_players_own_regular_season_rate(tmp_path):
    client, fixture = _client(tmp_path, peer_ids=(202, 303))

    response = client.get(
        f"/api/games/matchup/selection?game_id={fixture['game']['nba_game_id']}"
        f"&player_id={fixture['player']['canonical_player_id']}"
    )

    archetype = response.get_json()["archetype"]
    assert archetype["thin"] is True
    assert [row["row_type"] for row in archetype["rows"]] == [
        "game",
        "game",
        "game",
        "average",
    ]
    playoff, peer_two, peer_one, average = archetype["rows"]
    assert [row["game_date"] for row in archetype["rows"]] == [
        "2026-05-02",
        "2026-01-12",
        "2026-01-10",
        None,
    ]
    assert playoff["deltas"]["PTS"] == 0.32
    assert peer_two["deltas"]["PTS"] == -0.25
    assert peer_one["deltas"]["PTS"] == 0.12
    assert average["minutes"] == 30.0
    assert average["stats"]["PTS"] == 24.666667
    assert average["deltas"]["PTS"] == 0.126667
    expected_markets = set(fixture["player"]["market_categories"])
    for row in archetype["rows"]:
        assert set(row["stats"]) == expected_markets
        assert set(row["deltas"]) == expected_markets


def test_known_pool_player_without_stored_logs_gets_honest_empty_tables(tmp_path):
    empty_player = {
        "canonical_player_id": 404,
        "name": "No Stored Logs",
        "team_id": 1,
        "market_categories": ["PTS", "PRA", "FGA", "FG3A"],
    }
    client, fixture = _client(tmp_path, pool_player=empty_player)

    response = client.get(
        f"/api/games/matchup/selection?game_id={fixture['game']['nba_game_id']}"
        "&player_id=404"
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "player_id": 404,
        "h2h": {"thin": True, "rows": []},
        "archetype": {"thin": True, "rows": []},
    }


def test_unknown_game_and_player_ids_return_resource_not_found(tmp_path):
    client, fixture = _client(tmp_path)

    unknown_game = client.get(
        "/api/games/matchup/selection?game_id=unknown&player_id=101"
    )
    unknown_player = client.get(
        f"/api/games/matchup/selection?game_id={fixture['game']['nba_game_id']}"
        "&player_id=999"
    )

    assert unknown_game.status_code == 404
    assert unknown_game.get_json()["error"]["code"] == "resource_not_found"
    assert unknown_player.status_code == 404
    assert unknown_player.get_json()["error"]["code"] == "resource_not_found"
