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
from app.services.player_pool import StoredPlayerPoolReader
from app.services.player_pool_snapshot_repository import (
    PlayerPoolSnapshotRepository,
    PlayerPoolSnapshotScope,
)
from app.services.statistic_catalog import CanonicalStatistic, StatisticCatalog
from app.services.matchup_selection import MatchupSelectionService


FIXTURE = Path(__file__).parents[1] / "fixtures/matchup_selection/selected_player.json"
RETRIEVED_AT = datetime(2026, 5, 2, tzinfo=timezone.utc)


class RecordedEventCatalog:
    def __init__(self, game):
        self.game = game

    def get_events(self, season):
        return [{"season": season, **self.game}]

    def count_events(self, season):
        return 1


class RecordedArchetypePeers:
    def __init__(self, peer_ids=()):
        self.peer_ids = tuple(peer_ids)

    def list_peer_ids(self, player_id):
        return self.peer_ids


def _client(
    tmp_path,
    *,
    peer_ids=(),
    pool_player=None,
    event_catalog=None,
    seed_pool=True,
    clock=lambda: RETRIEVED_AT,
    statistic_catalog=None,
    publish_logs=True,
    extra_logs=(),
):
    fixture = json.loads(FIXTURE.read_text())
    fixture["logs"] = [*fixture["logs"], *extra_logs]
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{tmp_path / 'selection.sqlite3'}")
    run_migrations(engine)
    catalog = statistic_catalog or StatisticCatalog.load_default()
    repository = PlayerGameLogRepository(
        engine,
        statistic_catalog=catalog,
        stats_surface_max_age=timedelta(hours=30),
        stats_surface_season=fixture["season"],
        clock=clock,
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
    if publish_logs:
        repository.publish(
            fixture["season"],
            records,
            retrieved_at=RETRIEVED_AT,
            source_provider="recorded",
            source_row_count=len(records),
        )
    snapshot_repository = PlayerPoolSnapshotRepository(engine)
    if seed_pool:
        player = pool_player or fixture["player"]
        scope = PlayerPoolSnapshotScope.create(
            fixture["season"],
            (fixture["game"]["nba_game_id"], "0022500999"),
        )
        assert snapshot_repository.try_acquire_refresh(
            scope,
            owner="fixture",
            now=RETRIEVED_AT,
            lease_seconds=60,
        )
        assert snapshot_repository.replace_owned(
            scope,
            owner="fixture",
            payload={
                "players": [
                    {
                        **player,
                        "provenance": {
                            "recorded": player["market_categories"],
                        },
                    }
                ],
                "team_counts": {str(player["team_id"]): 1},
                "freshness": {
                    "status": "fresh",
                    "retrieved_at": RETRIEVED_AT.isoformat(),
                    "providers": {},
                },
            },
            retrieved_at=RETRIEVED_AT,
            now=RETRIEVED_AT,
        )
    settings = RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
        nba=NBASeasonSettings(current_season=fixture["season"]),
    )
    service = MatchupSelectionService(
        event_catalog=event_catalog or RecordedEventCatalog(fixture["game"]),
        player_pool=StoredPlayerPoolReader(
            snapshot_repository, clock=lambda: RETRIEVED_AT
        ),
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
    assert payload["freshness"] == {
        "player_pool": {
            "status": "fresh",
            "retrieved_at": RETRIEVED_AT.isoformat(),
            "providers": {},
        },
        "player_game_logs": {
            "status": "fresh",
            "retrieved_at": RETRIEVED_AT.isoformat(),
        },
    }
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
    assert [(row["player_id"], row["player_name"]) for row in archetype["rows"]] == [
        (202, "Peer One"),
        (303, "Peer Two"),
        (202, "Peer One"),
        (None, None),
    ]
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
        "experience": {
            "mode": "current",
            "player_source": "player_pool",
            "focal_game": None,
            "samples": {
                "context": "season_to_date",
                "excludes_focal_game": False,
            },
            "baseline": {"context": "season_to_date", "hindsight": False},
        },
        "freshness": {
            "player_pool": {
                "status": "fresh",
                "retrieved_at": RETRIEVED_AT.isoformat(),
                "providers": {},
            },
            "player_game_logs": {
                "status": "fresh",
                "retrieved_at": RETRIEVED_AT.isoformat(),
            },
        },
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


def test_empty_event_catalog_and_missing_stored_pool_are_unavailable(tmp_path):
    class EmptyEventCatalog:
        def count_events(self, season):
            return 0

        def get_events(self, season):
            raise AssertionError("an empty catalog must fail before an event read")

    empty_catalog_client, _ = _client(tmp_path / "empty", event_catalog=EmptyEventCatalog())
    missing_pool_client, fixture = _client(tmp_path / "pool", seed_pool=False)

    empty_catalog = empty_catalog_client.get(
        "/api/games/matchup/selection?game_id=0022500001&player_id=101"
    )
    missing_pool = missing_pool_client.get(
        f"/api/games/matchup/selection?game_id={fixture['game']['nba_game_id']}"
        "&player_id=101"
    )

    assert empty_catalog.status_code == 503
    assert empty_catalog.get_json()["error"]["code"] == "provider_unavailable"
    assert missing_pool.status_code == 503
    assert missing_pool.get_json()["error"]["code"] == "provider_unavailable"


def test_stale_player_logs_are_distinct_from_fresh_empty_history(tmp_path):
    client, fixture = _client(
        tmp_path,
        clock=lambda: RETRIEVED_AT + timedelta(hours=31),
    )

    response = client.get(
        f"/api/games/matchup/selection?game_id={fixture['game']['nba_game_id']}"
        "&player_id=101"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["freshness"]["player_game_logs"] == {
        "status": "stale",
        "retrieved_at": RETRIEVED_AT.isoformat(),
    }
    assert payload["h2h"] == {"thin": True, "rows": []}
    assert payload["archetype"] == {"thin": True, "rows": []}


def test_missing_player_log_publication_is_explicit_and_degraded_200(tmp_path):
    client, fixture = _client(tmp_path, publish_logs=False)

    response = client.get(
        f"/api/games/matchup/selection?game_id={fixture['game']['nba_game_id']}"
        "&player_id=101"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["freshness"]["player_game_logs"] == {
        "status": "missing",
        "retrieved_at": None,
    }
    assert payload["h2h"] == {"thin": True, "rows": []}


def test_removed_stored_pool_market_is_explicitly_unavailable(tmp_path):
    player = json.loads(FIXTURE.read_text())["player"]
    player["market_categories"] = [*player["market_categories"], "REMOVED"]
    client, fixture = _client(tmp_path, pool_player=player)

    response = client.get(
        f"/api/games/matchup/selection?game_id={fixture['game']['nba_game_id']}"
        "&player_id=101"
    )

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "provider_unavailable"


def test_sanctioned_derived_component_catalog_extension_is_computed(tmp_path):
    default = StatisticCatalog.load_default()
    catalog = StatisticCatalog(
        statistics=(
            *default.statistics,
            CanonicalStatistic(
                id="two_pointer_attempt_volume",
                label="Two Pointer Attempt Volume",
                unit="count",
                scoring_periods=("full_game",),
                components=("two_pointers_attempted",),
                market_category="X2A",
            ),
        )
    )
    player = json.loads(FIXTURE.read_text())["player"]
    player["market_categories"] = [*player["market_categories"], "X2A"]
    client, fixture = _client(
        tmp_path,
        pool_player=player,
        statistic_catalog=catalog,
    )

    response = client.get(
        f"/api/games/matchup/selection?game_id={fixture['game']['nba_game_id']}"
        "&player_id=101"
    )

    assert response.status_code == 200
    regular = response.get_json()["h2h"]["rows"][1]
    assert regular["stats"]["X2A"] == 12.0
    assert regular["deltas"]["X2A"] == 0.0


FOCAL_GAME_DATE = "2026-01-20"


def _focal_log_row(*, player_id, name, team_id, tricode, opponent_id, opponent):
    """One canonical row for the focal game itself."""

    return {
        "season_type": "Regular Season",
        "player_id": player_id,
        "game_id": "0022500200",
        "player_name": name,
        "game_date": FOCAL_GAME_DATE,
        "team_id": team_id,
        "team_tricode": tricode,
        "opponent_team_id": opponent_id,
        "opponent_team_tricode": opponent,
        "is_home": False,
        "minutes": 36.0,
        "points": 40,
        "rebounds": 9,
        "assists": 6,
        "field_goals_made": 15,
        "field_goals_attempted": 25,
        "three_pointers_made": 4,
        "three_pointers_attempted": 9,
        "free_throws_made": 6,
        "free_throws_attempted": 7,
        "offensive_rebounds": 2,
        "defensive_rebounds": 7,
        "turnovers": 3,
        "steals": 2,
        "blocks": 1,
        "personal_fouls": 3,
    }


def _historical_selection_client(tmp_path, **kwargs):
    """A completed focal game with canonical rows and no Player Pool at all."""

    fixture = json.loads(FIXTURE.read_text())
    final_game = {**fixture["game"], "status_text": "Final", "status_code": 3}
    return _client(
        tmp_path,
        event_catalog=RecordedEventCatalog(final_game),
        seed_pool=False,
        extra_logs=(
            _focal_log_row(
                player_id=101,
                name="Selected Player",
                team_id=1,
                tricode="AAA",
                opponent_id=2,
                opponent="BBB",
            ),
        ),
        **kwargs,
    )


def test_historical_selection_accepts_a_participant_without_a_player_pool(
    tmp_path,
):
    client, fixture = _historical_selection_client(tmp_path)

    response = client.get(
        f"/api/games/matchup/selection?game_id={fixture['game']['nba_game_id']}"
        "&player_id=101"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["player_id"] == 101
    assert payload["experience"]["mode"] == "historical"
    assert payload["experience"]["player_source"] == "game_logs"
    assert payload["experience"]["samples"] == {
        "context": "pregame",
        "excludes_focal_game": True,
    }
    assert payload["experience"]["baseline"] == {
        "context": "completed_season",
        "hindsight": True,
    }
    # No Player Pool exists at all, and the response says so truthfully.
    assert payload["freshness"]["player_pool"] == {
        "status": "unavailable",
        "state": "missing",
        "observed_at": None,
        "retrieved_at": None,
        "providers": {},
    }


def test_historical_selection_separates_the_focal_line_from_the_samples(
    tmp_path,
):
    client, fixture = _historical_selection_client(tmp_path)

    response = client.get(
        f"/api/games/matchup/selection?game_id={fixture['game']['nba_game_id']}"
        "&player_id=101"
    )

    payload = response.get_json()
    focal = payload["experience"]["focal_game"]
    assert focal["game_id"] == "0022500200"
    assert focal["game_date"] == FOCAL_GAME_DATE
    assert focal["matchup"] == "AAA @ BBB"
    assert focal["minutes"] == 36.0
    assert focal["stats"]["PTS"] == 40.0
    # Only the one stored game against BBB strictly before the focal date:
    # the focal row itself and the later playoff row are both excluded.
    game_rows = [row for row in payload["h2h"]["rows"] if row["row_type"] == "game"]
    assert [row["game_date"] for row in game_rows] == ["2026-01-05"]
    assert all(row["game_date"] < FOCAL_GAME_DATE for row in game_rows)


def test_historical_selection_baseline_excludes_the_focal_result(tmp_path):
    client, fixture = _historical_selection_client(tmp_path)

    response = client.get(
        f"/api/games/matchup/selection?game_id={fixture['game']['nba_game_id']}"
        "&player_id=101"
    )

    (game_row, _average) = response.get_json()["h2h"]["rows"]
    # Regular Season rows excluding the focal game are 15 points in 30 minutes
    # and 25 in 40, so the baseline rate is 40/70 PTS per minute. Including the
    # 40-point focal game would drag the delta down instead.
    assert game_row["stats"]["PTS"] == 25.0
    assert game_row["deltas"]["PTS"] == round(25.0 / 40 - 40 / 70, 6)


def test_a_player_without_a_canonical_row_in_the_focal_game_is_404(tmp_path):
    client, fixture = _historical_selection_client(tmp_path)

    response = client.get(
        f"/api/games/matchup/selection?game_id={fixture['game']['nba_game_id']}"
        "&player_id=202"
    )

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "resource_not_found"


def test_historical_selection_scores_the_governed_statistic_catalog(tmp_path):
    client, fixture = _historical_selection_client(tmp_path)

    response = client.get(
        f"/api/games/matchup/selection?game_id={fixture['game']['nba_game_id']}"
        "&player_id=101"
    )

    payload = response.get_json()
    catalog_markets = {
        statistic.market_category
        for statistic in StatisticCatalog.load_default().statistics
        if statistic.market_category is not None
    }
    # The category universe comes from the Statistic Catalog, never from a
    # DFS projection archive.
    assert set(payload["experience"]["focal_game"]["stats"]) == catalog_markets
    assert set(payload["h2h"]["rows"][0]["stats"]) == catalog_markets
