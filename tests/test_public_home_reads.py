"""Anonymous slate, slate edges and ladder reads (crf04/statsplus#211)."""

import pytest
import requests

import app.utils.auth as auth
from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.services.game_service import GameService
from tests.test_game_logs import _game_logs_frame, _StubRankings

PUBLIC_PATHS = ["/api/games/slate", "/api/games/slate/edges", "/api/games/ladder"]
LADDER_SETS = {
    "start": "player_name=Jalen Johnson",
    "narrow": "player_name=Jalen Johnson&players_off[]=Trae Young",
    "stack": (
        "player_name=Jalen Johnson&players_off[]=Trae Young"
        "&teams_against[]=OPP_PTS&rank_filter[]=10&minutes_filter=25,48"
    ),
}


@pytest.fixture
def firebase_enforced(monkeypatch):
    """Firebase is configured, so a route with auth refuses a missing token."""
    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())

    def verify(token):
        return {"uid": token, "email": f"{token}@example.com"}

    monkeypatch.setattr(auth, "verify_firebase_token", verify)


def _service(monkeypatch, mock_db_engine, mock_redis_client, ranked):
    service = GameService(
        mock_db_engine,
        mock_redis_client,
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season="2024-25"),
        ),
        team_filter_rankings=_StubRankings(ranked),
    )
    monkeypatch.setattr(service, "_get_game_logs", lambda name, season: (_game_logs_frame(), None))
    # "Trae Young sat": the third game is one he played in.
    monkeypatch.setattr(
        service,
        "filter_players_on_off",
        lambda df, on, off, season, **kw: df[df["GAME_ID"] != "003"],
    )
    return service


@pytest.fixture
def real_service(monkeypatch, mock_db_engine, mock_redis_client, dependencies):
    dependencies.game_service = _service(monkeypatch, mock_db_engine, mock_redis_client, ["LAL"])
    return dependencies.game_service


@pytest.fixture
def slate_services(dependencies):
    dependencies.slate_service.get_slate.return_value = {"slate_date": "2026-01-02"}
    dependencies.slate_edges_service = type(
        "Edges", (), {"get_edges": lambda self, date, fallback=None: {"slate_date": "2026-01-02"}}
    )()


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_anonymous_public_reads_answer_200(client, firebase_enforced, slate_services, real_service, path):
    response = client.get(path)

    assert response.status_code == 200
    # Slate and edges carry the slate date; the ladder carries its rungs.
    body = response.get_json()
    assert body.get("slate_date") == "2026-01-02" or [rung["id"] for rung in body["rungs"]] == [
        "start",
        "narrow",
        "stack",
    ]


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_a_token_is_ignored_even_when_it_would_be_refused(
    client, monkeypatch, firebase_enforced, slate_services, real_service, path
):
    def refuse(token):
        raise auth.InvalidTokenError("waitlisted or invalid")

    monkeypatch.setattr(auth, "verify_firebase_token", refuse)

    response = client.get(path, headers={"Authorization": "Bearer waitlisted-user"})

    assert response.status_code == 200


def test_game_logs_stays_authenticated(client, firebase_enforced, real_service):
    response = client.get("/api/games/game_logs?player_name=Jalen+Johnson")

    assert response.status_code == 401


def test_other_game_routes_stay_authenticated(client, firebase_enforced):
    statuses = {
        path: client.get(path).status_code
        for path in (
            "/api/games/matchup?game_id=1",
            "/api/games/seasons",
            "/api/games/matchup/selection?game_id=1&player_id=1",
        )
    }

    assert statuses == {path: 401 for path in statuses}


@pytest.mark.parametrize("query", ["?player_name=Jalen+Johnson", "?x=1", "?date="])
def test_ladder_refuses_any_query_parameter(client, real_service, query):
    response = client.get(f"/api/games/ladder{query}")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"


def test_ladder_answers_literal_points_games_and_series(client, real_service):
    response = client.get("/api/games/ladder")

    assert response.status_code == 200
    assert response.get_json() == {
        "rungs": [
            {"id": "start", "points": 23.33, "games": 3, "series": [25, 15, 30]},
            {"id": "narrow", "points": 20.0, "games": 2, "series": [25, 15]},
            {"id": "stack", "points": 25.0, "games": 1, "series": [25]},
        ]
    }


def test_each_rung_equals_the_game_logs_answer_for_its_filter_set(client, firebase_enforced, real_service):
    rungs = client.get("/api/games/ladder").get_json()["rungs"]

    for rung in rungs:
        logs = client.get(
            f"/api/games/game_logs?{LADDER_SETS[rung['id']]}",
            headers={"Authorization": "Bearer someone"},
        )
        assert logs.status_code == 200
        body = logs.get_json()
        assert rung["games"] == len(body["game_logs"])
        assert rung["series"] == [row["PTS"] for row in body["game_logs"]]
        assert rung["points"] == body["averages"][0]["PTS"]


def test_an_empty_rung_has_null_points(client, monkeypatch, mock_db_engine, mock_redis_client, dependencies):
    dependencies.game_service = _service(monkeypatch, mock_db_engine, mock_redis_client, ["NYK"])

    rungs = client.get("/api/games/ladder").get_json()["rungs"]

    assert rungs[2] == {"id": "stack", "points": None, "games": 0, "series": []}
    assert rungs[1]["games"] == 2


def test_ladder_provider_timeout_is_503(client, dependencies):
    dependencies.game_service.get_filtered_logs.side_effect = requests.exceptions.Timeout("slow")

    response = client.get("/api/games/ladder")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "provider_unavailable"


def test_ladder_missing_player_is_404(client, dependencies):
    dependencies.game_service.get_filtered_logs.side_effect = ValueError("No matching player found")

    response = client.get("/api/games/ladder")

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "resource_not_found"


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_public_reads_are_counted_against_the_ip_bucket(
    client, firebase_enforced, slate_services, real_service, path
):
    ip = {"X-Forwarded-For": "198.51.100.20"}
    # A waitlisted token changes nothing: the bucket is the IP's.
    token = {**ip, "Authorization": "Bearer waitlisted-user"}

    statuses = [client.get(path, headers=token).status_code for _ in range(61)]

    assert 429 not in statuses[:60]
    assert statuses[60] == 429
    other = client.get(path, headers={"X-Forwarded-For": "198.51.100.21"})
    assert other.status_code == 200


# --- the permanent ladder (crf04/statsplus-backend#422) ---------------------


def test_every_rung_is_pinned_to_the_2025_26_season(client, dependencies, monkeypatch):
    seen = []

    def logs(name, query):
        seen.append(query.season_filter)
        return {"game_logs": [{"PTS": 20}], "averages": [{"PTS": 20.0}]}

    dependencies.game_service.get_filtered_logs.side_effect = logs
    dependencies.game_service.default_season.return_value = "2026-27"

    response = client.get("/api/games/ladder")

    assert response.status_code == 200
    assert seen == ["2025-26", "2025-26", "2025-26"]


def test_the_ladder_is_computed_once_and_then_served_from_memory(client, dependencies):
    def logs(name, query):
        return {"game_logs": [{"PTS": 20}, {"PTS": 30}], "averages": [{"PTS": 25.0}]}

    dependencies.game_service.get_filtered_logs.side_effect = logs

    first = client.get("/api/games/ladder")
    second = client.get("/api/games/ladder")

    assert dependencies.game_service.get_filtered_logs.call_count == 3
    assert (
        first.get_json()
        == second.get_json()
        == {
            "rungs": [
                {"id": rung, "points": 25.0, "games": 2, "series": [20, 30]}
                for rung in ("start", "narrow", "stack")
            ]
        }
    )


def test_concurrent_first_ladder_requests_compute_it_once(client, dependencies):
    import threading
    import time

    def slow(name, query):
        time.sleep(0.02)
        return {"game_logs": [{"PTS": 20}], "averages": [{"PTS": 20.0}]}

    dependencies.game_service.get_filtered_logs.side_effect = slow
    statuses = []

    def request():
        statuses.append(client.get("/api/games/ladder").status_code)

    threads = [threading.Thread(target=request) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses == [200] * 4
    assert dependencies.game_service.get_filtered_logs.call_count == 3


def test_a_failed_ladder_is_not_remembered(client, dependencies):
    answer = {"game_logs": [{"PTS": 20}], "averages": [{"PTS": 20.0}]}
    dependencies.game_service.get_filtered_logs.side_effect = [
        requests.exceptions.Timeout("slow"),
        answer,
        answer,
        answer,
        answer,
        answer,
        answer,
    ]

    failed = client.get("/api/games/ladder")
    recovered = client.get("/api/games/ladder")
    again = client.get("/api/games/ladder")

    assert (failed.status_code, recovered.status_code, again.status_code) == (503, 200, 200)
    assert dependencies.game_service.get_filtered_logs.call_count == 4


def test_an_incomplete_ladder_is_not_remembered(client, dependencies):
    empty = {"game_logs": [], "averages": []}
    answer = {"game_logs": [{"PTS": 20}], "averages": [{"PTS": 20.0}]}
    dependencies.game_service.get_filtered_logs.side_effect = [empty, empty, empty] + [answer] * 3

    first = client.get("/api/games/ladder").get_json()
    second = client.get("/api/games/ladder").get_json()

    assert [rung["points"] for rung in first["rungs"]] == [None] * 3
    assert [rung["points"] for rung in second["rungs"]] == [20.0] * 3
