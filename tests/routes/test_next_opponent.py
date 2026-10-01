"""Next-opponent HTTP reads and the filter tier applied to real game logs."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.config.settings import RuntimeSettings
from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.services.database_first_activation import PublicationReadSnapshot
from app.services.game_service import GameService
from app.services.next_opponent import NextOpponentService
from app.services.team_filter_rankings import TeamFilterRankingService
from tests.routes.test_team_stats import _seeded_reads
from tests.support.publication_stubs import StubReader, StubGovernance
from tests.test_game_logs import _game_logs_frame


class Reader(StubReader):
    def snapshot(self, keys, *, season, **kwargs):
        return PublicationReadSnapshot(season, self.read_many(keys, season=season), ())


class Catalog:
    def get_catalog(self, season, *, active_only=False):
        return [
            dict(
                player_id=1,
                display_name="LeBron James",
                is_active_for_season=True,
                team_id=1610612738,
            )
        ]


class Logs:
    def get_player_logs(self, player_id, season, **kwargs):
        return _game_logs_frame().head(1).copy()


@pytest.fixture
def setup(dependencies, client, mock_db_engine):
    reads = _seeded_reads()
    reader = Reader(reads)
    rankings = TeamFilterRankingService(reader, governance_resolver=StubGovernance())
    settings = RuntimeSettings(
        environment="testing",
        nba={"current_season": "2025-26"},
        cache={"enabled": False},
    )
    game = GameService(
        mock_db_engine,
        redis_client=SimpleNamespace(),
        settings=settings,
        athlete_catalog=Catalog(),
        game_logs_source=Logs(),
        team_filter_rankings=rankings,
        publication_reader=reader,
    )
    event = dict(
        nba_game_id="0022500500",
        scheduled_at="2026-01-20T00:30:00+00:00",
        status_code=1,
        home_team=dict(id=1610612747, tricode="LAL", name="Los Angeles Lakers"),
        away_team=dict(id=1610612738, tricode="BOS", name="Boston Celtics"),
    )
    events = [event]
    service = NextOpponentService(
        game,
        SimpleNamespace(get_events=lambda season: events),
        rankings,
        clock=lambda: datetime(2026, 1, 19, tzinfo=timezone.utc),
    )
    dependencies.next_opponent_service = service
    dependencies.game_service = game
    return client, reads, events


def get(client):
    return client.get("/api/players/next-opponent?player_name=LeBron+James")


def test_next_game_and_all_categories(setup):
    client, _, _ = setup
    response = get(client)
    assert response.status_code == 200
    body = response.get_json()
    assert body["next_game"] == dict(
        game_id="0022500500",
        date="2026-01-19",
        opponent="LAL",
        opponent_name="Los Angeles Lakers",
        home=False,
    )
    assert {row["group"] for row in body["opponent_ranks"]} == {
        "General",
        "Play type",
        "Assists",
        "Zones",
        "Shot type",
    }
    points = next(
        row for row in body["opponent_ranks"] if row["team_filter"] == "OPP_PTS"
    )
    assert points["value"] == 168
    assert points["most_rank"] == 1
    assert points["ranked_teams"] == 30
    assert {"C&S PTS", "PU PTS", "C&S 3s", "Less Than 10 ft"} <= {
        row["team_filter"] for row in body["opponent_ranks"]
    }


@pytest.mark.parametrize("direction", ["most", "fewest", "tie", "short"])
def test_returned_tiers_include_opponent_through_game_log(setup, direction):
    client, reads, _ = setup
    if direction in ("fewest", "tie"):
        key = "traditional_opponent_season"
        codes = sorted(NBA_TEAM_ID_TO_TRICODE.values())
        rows = []
        for row in reads[key].decoded:
            value = 1 if direction == "fewest" and row.team_tricode == "LAL" else 112
            if direction == "tie":
                value = (
                    200
                    if row.team_tricode in [code for code in codes if code != "LAL"][:7]
                    else (50 if row.team_tricode < "LAL" else 100)
                )
            rows.append(replace(row, per48={**row.per48, "points": value}))
        reads[key] = replace(reads[key], decoded=tuple(rows))
    if direction == "short":
        key = "synergy_play_types_opponent_season"
        reads[key] = replace(
            reads[key],
            decoded=tuple(
                replace(
                    row, per48={**row.per48, "Transition_PTS": 0, "Transition_POSS": 0}
                )
                if row.team_tricode in ("ATL", "BKN")
                else replace(row, per48={**row.per48, "Transition_PTS": 1.0})
                if row.team_tricode == "LAL"
                else row
                for row in reads[key].decoded
            ),
        )
    body = get(client).get_json()
    points = next(
        row for row in body["opponent_ranks"] if row["team_filter"] == "OPP_PTS"
    )
    if direction == "fewest":
        assert points["most_rank"] == 30
    if direction == "tie":
        assert points["most_rank"] == 8
    if direction == "short":
        transition = next(
            row for row in body["opponent_ranks"] if row["team_filter"] == "Transition"
        )
        assert transition["ranked_teams"] == 28
        assert transition["most_rank"] == 28
    for row in body["opponent_ranks"]:
        if not row["team_filter"] or not (
            row["most_rank"] <= 8 or row["most_rank"] > row["ranked_teams"] - 8
        ):
            continue
        low, high = (
            (1, 8)
            if row["most_rank"] <= 8
            else (row["ranked_teams"] - 7, row["ranked_teams"])
        )
        response = client.get(
            "/api/games/game_logs",
            query_string={
                "player_name": "LeBron James",
                "season_filter": "2025-26",
                "teams_against[]": row["team_filter"],
                "rank_filter[]": f"{low},{high}",
            },
        )
        assert response.status_code == 200, response.get_json()
        assert [game["MATCHUP"] for game in response.get_json()["game_logs"]] == [
            "BOS vs. LAL"
        ]
        assert response.get_json()["next_game"] is None


def test_no_next_game(setup):
    client, _, events = setup
    events.clear()
    assert get(client).get_json() == {"next_game": None, "opponent_ranks": []}


def test_unknown_player(setup):
    client, _, _ = setup
    response = client.get("/api/players/next-opponent?player_name=Nobody")
    assert response.status_code == 404
    assert (
        response.get_json()["error"]["message"] == "The requested player was not found."
    )


@pytest.mark.parametrize(
    "query",
    ["", "?player_name=", "?player_name=A&player_name=B", "?player_name=A&extra=x"],
)
def test_invalid_request(setup, query):
    response = setup[0].get("/api/players/next-opponent" + query)
    assert response.status_code == 400


def test_requires_authentication(setup, monkeypatch):
    monkeypatch.setattr("app.utils.auth.get_firebase_app", lambda: object())
    response = get(setup[0])
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"


def test_next_game_ignores_past_finished_and_postponed_events(setup):
    client, _, events = setup
    original = events[0]
    events.extend(
        [
            {
                **original,
                "nba_game_id": "0022500400",
                "scheduled_at": "2026-01-18T00:30:00+00:00",
            },
            {**original, "nba_game_id": "0022500401", "status_code": 3},
            {
                **original,
                "nba_game_id": "0022500402",
                "postponement_evidence": {"source": "NBA"},
            },
            {**original, "nba_game_id": "0032500403"},
            {
                **original,
                "nba_game_id": "0022500600",
                "scheduled_at": "2026-01-21T00:30:00+00:00",
            },
        ]
    )
    assert get(client).get_json()["next_game"]["game_id"] == "0022500500"


def test_home_game(setup):
    client, _, events = setup
    events[0]["away_team"], events[0]["home_team"] = (
        events[0]["home_team"],
        events[0]["away_team"],
    )
    assert get(client).get_json()["next_game"]["home"] is True
