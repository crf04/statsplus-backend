"""PLAYTYPE_RTG on game-log rows (crf04/statsplus#37).

The rating depends only on (player, opponent), so these tests pin both the
arithmetic and the request cost: the governed Diet and team-window facts are
read once per request, never once per row.
"""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, text

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.domain.play_type_matchup import play_type_matchup
from app.models.game_logs import GameLogQuery


LAL = 1610612747
GSW = 1610612744
BOS = 1610612738

# A deliberately partial Diet: three observed slices whose shares sum below 1,
# exactly the shape the Matchup page hands the shared domain function.
SHARES = {"Transition": 0.30, "Isolation": 0.20, "Spotup": 0.40}
LEAGUE_ALLOWED = {"Transition": 20.0, "Isolation": 10.0, "Spotup": 25.0}
TEAM_ALLOWED = {
    # A generous defense: every governed slice above league average.
    GSW: {"Transition": 26.0, "Isolation": 13.0, "Spotup": 30.0},
    # A stingy one.
    BOS: {"Transition": 16.0, "Isolation": 8.0, "Spotup": 21.0},
}


def _expected(team_id):
    matchup = play_type_matchup(SHARES, TEAM_ALLOWED[team_id], LEAGUE_ALLOWED)
    return round(100 * (1 + matchup), 1)


class _Fact:
    """The narrow shape GameService reads off a stored Diet fact."""

    def __init__(self, base, slice_key, share):
        self.base = base
        self.slice_key = slice_key
        self.share = share


class _Metric:
    def __init__(self, base, slice_key, stat_key, value):
        self.base = base
        self.slice_key = slice_key
        self.stat_key = stat_key
        self.allowed_per_48 = value
        self.average_allowed_per_48 = value


class _Window:
    def __init__(self, league_metrics, team_metrics):
        self.league_metrics = league_metrics
        self.team_metrics = team_metrics


class _DietResult:
    def __init__(self, players):
        self.players = players


class _DietReader:
    """Reads the player's shares with exactly one statement against ``engine``."""

    def __init__(self, engine):
        self.engine = engine

    def get_for_players(self, season, player_ids, **kwargs):
        players = {}
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT player_id, base, slice_key, share "
                    "FROM diet_shares WHERE season = :season"
                ),
                {"season": season},
            ).mappings().all()
        requested = set(player_ids)
        for row in rows:
            if row["player_id"] not in requested:
                continue
            players.setdefault(row["player_id"], []).append(
                _Fact(row["base"], row["slice_key"], row["share"])
            )
        return _DietResult({key: tuple(value) for key, value in players.items()})


class _WindowReader:
    """Reads the Season team window with exactly one statement against ``engine``."""

    def __init__(self, engine):
        self.engine = engine

    def get_latest_window(self, season, **kwargs):
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT team_id, base, slice_key, stat_key, value "
                    "FROM team_windows WHERE season = :season"
                ),
                {"season": season},
            ).mappings().all()
        if not rows:
            return None
        league = []
        teams = {}
        for row in rows:
            metric = _Metric(
                row["base"], row["slice_key"], row["stat_key"], row["value"]
            )
            if row["team_id"] is None:
                league.append(metric)
            else:
                teams.setdefault(row["team_id"], []).append(metric)
        return _Window(tuple(league), {key: tuple(value) for key, value in teams.items()})


@pytest.fixture
def rating_engine(tmp_path):
    """Seed player identity, the player's Diet, and the Season team window."""
    engine = create_engine(f"sqlite:///{tmp_path / 'ratings.db'}")

    pd.DataFrame([{"id": 2544, "full_name": "LeBron James"}]).to_sql(
        "player_information", engine, index=False
    )
    pd.DataFrame(
        [
            {
                "season": "2025-26",
                "player_id": 2544,
                "base": "play_types",
                "slice_key": slice_key,
                "share": share,
            }
            for slice_key, share in SHARES.items()
        ]
    ).to_sql("diet_shares", engine, index=False)

    window_rows = [
        {
            "season": "2025-26",
            "team_id": None,
            "base": "play_types",
            "slice_key": slice_key,
            "stat_key": "PTS",
            "value": value,
        }
        for slice_key, value in LEAGUE_ALLOWED.items()
    ]
    for team_id, allowed in TEAM_ALLOWED.items():
        window_rows.extend(
            {
                "season": "2025-26",
                "team_id": team_id,
                "base": "play_types",
                "slice_key": slice_key,
                "stat_key": "PTS",
                "value": value,
            }
            for slice_key, value in allowed.items()
        )
    pd.DataFrame(window_rows).to_sql("team_windows", engine, index=False)
    return engine


def _game_logs():
    """Two games against the generous defense, one against the stingy one."""
    columns = [
        "GAME_ID", "GAME_DATE", "MATCHUP", "TEAM_ABBREVIATION", "TEAM_ID",
        "MIN", "PTS", "REB", "AST", "PRA", "PA", "PR", "RA", "STKS", "FD_PTS",
        "FGM", "FGA", "FG_PCT", "FG2M", "FG2A", "FG3M", "FG3A", "FTM", "FTA",
        "OREB", "DREB", "TOV", "STL", "BLK", "PF",
    ]
    rows = [
        ["0001", "2025-01-01", "LAL vs. GSW", "LAL", LAL,
         30, 25, 8, 7, 40, 32, 33, 15, 2, 44.2,
         9, 16, 56.3, 6, 9, 3, 7, 4, 5, 1, 7, 2, 1, 1, 2],
        ["0002", "2025-01-05", "LAL @ BOS", "LAL", LAL,
         32, 20, 6, 9, 35, 29, 26, 15, 1, 38.0,
         8, 15, 53.3, 6, 11, 2, 4, 2, 2, 2, 4, 3, 1, 0, 3],
        ["0003", "2025-01-09", "LAL @ GSW", "LAL", LAL,
         28, 18, 4, 5, 27, 23, 22, 9, 3, 30.5,
         7, 14, 50.0, 5, 9, 2, 5, 2, 3, 0, 4, 1, 2, 1, 1],
    ]
    return pd.DataFrame(rows, columns=columns)


def _service(engine, monkeypatch, *, player_diets=..., team_matchups=...):
    from app.services import game_service as game_service_module

    monkeypatch.setattr(
        game_service_module, "get_redis_client", lambda *args, **kwargs: None
    )
    service = game_service_module.GameService(
        engine,
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season="2025-26"),
        ),
        player_diets=_DietReader(engine) if player_diets is ... else player_diets,
        team_matchups=_WindowReader(engine) if team_matchups is ... else team_matchups,
    )
    monkeypatch.setattr(
        service, "_get_game_logs", lambda name, season: (_game_logs(), None)
    )
    return service


def _ratings(result):
    return [row["PLAYTYPE_RTG"] for row in result["game_logs"]]


# --- the rating itself -------------------------------------------------------


def test_two_opponents_score_the_expected_ratings(rating_engine, monkeypatch):
    service = _service(rating_engine, monkeypatch)

    result = service.get_filtered_logs(
        "LeBron James", GameLogQuery(season_filter="2025-26")
    )

    assert _ratings(result) == [_expected(GSW), _expected(BOS), _expected(GSW)]
    assert _expected(GSW) > 100 > _expected(BOS)


def test_default_playstyle_range_keeps_every_game(rating_engine, monkeypatch):
    service = _service(rating_engine, monkeypatch)

    result = service.get_filtered_logs(
        "LeBron James",
        GameLogQuery(season_filter="2025-26", playstyle_range=(0.0, 200.0)),
    )

    assert len(result["game_logs"]) == 3


def test_minimum_rating_keeps_only_the_higher_rated_opponent(
    rating_engine, monkeypatch
):
    service = _service(rating_engine, monkeypatch)

    result = service.get_filtered_logs(
        "LeBron James",
        GameLogQuery(season_filter="2025-26", playstyle_range=(110.0, 200.0)),
    )

    assert [row["MATCHUP"] for row in result["game_logs"]] == [
        "LAL vs. GSW",
        "LAL @ GSW",
    ]
    assert _ratings(result) == [_expected(GSW), _expected(GSW)]


# --- fail-closed absence -----------------------------------------------------


def test_a_player_without_a_diet_share_scores_null_for_every_game(
    rating_engine, monkeypatch
):
    service = _service(rating_engine, monkeypatch)
    with rating_engine.begin() as connection:
        connection.execute(text("DELETE FROM diet_shares"))

    result = service.get_filtered_logs(
        "LeBron James", GameLogQuery(season_filter="2025-26")
    )

    assert _ratings(result) == [None, None, None]


def test_a_player_without_a_diet_share_yields_an_empty_non_default_range(
    rating_engine, monkeypatch
):
    service = _service(rating_engine, monkeypatch)
    with rating_engine.begin() as connection:
        connection.execute(text("DELETE FROM diet_shares"))

    result = service.get_filtered_logs(
        "LeBron James",
        GameLogQuery(season_filter="2025-26", playstyle_range=(110.0, 200.0)),
    )

    assert result["game_logs"] == []
    assert result["averages"] == []
    assert len(result["season_averages"]) == 1


def test_the_demo_database_scores_null_without_failing(rating_engine, monkeypatch):
    """No governed readers is the read-only demo branch, not an error."""
    service = _service(
        rating_engine, monkeypatch, player_diets=None, team_matchups=None
    )

    assert _ratings(
        service.get_filtered_logs(
            "LeBron James", GameLogQuery(season_filter="2025-26")
        )
    ) == [None, None, None]
    assert service.get_filtered_logs(
        "LeBron James",
        GameLogQuery(season_filter="2025-26", playstyle_range=(110.0, 200.0)),
    )["game_logs"] == []


def test_an_opponent_missing_from_the_window_scores_null(
    rating_engine, monkeypatch
):
    service = _service(rating_engine, monkeypatch)
    with rating_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM team_windows WHERE team_id = :team_id"),
            {"team_id": BOS},
        )

    result = service.get_filtered_logs(
        "LeBron James", GameLogQuery(season_filter="2025-26")
    )

    assert _ratings(result) == [_expected(GSW), None, _expected(GSW)]


# --- request cost ------------------------------------------------------------


def test_the_governed_facts_are_read_once_per_request_not_once_per_row(
    rating_engine, monkeypatch
):
    service = _service(rating_engine, monkeypatch)
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(rating_engine, "before_cursor_execute", record)
    try:
        result = service.get_filtered_logs(
            "LeBron James", GameLogQuery(season_filter="2025-26")
        )
    finally:
        event.remove(rating_engine, "before_cursor_execute", record)

    assert len(result["game_logs"]) == 3
    assert len([item for item in statements if "diet_shares" in item]) == 1
    assert len([item for item in statements if "team_windows" in item]) == 1


# --- route contract ----------------------------------------------------------


def test_game_log_route_rows_carry_the_rating(
    client, dependencies, rating_engine, monkeypatch
):
    from app.routes import game_routes

    service = _service(rating_engine, monkeypatch)
    dependencies.game_service = service
    monkeypatch.setattr(game_routes, "_default_season", lambda: "2025-26")

    response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James&season_filter=2025-26"
    )

    assert response.status_code == 200
    rows = response.get_json()["game_logs"]
    assert [row["PLAYTYPE_RTG"] for row in rows] == [
        _expected(GSW),
        _expected(BOS),
        _expected(GSW),
    ]
