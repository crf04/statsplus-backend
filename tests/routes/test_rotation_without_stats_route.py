"""``without`` box-score stats per side on ``GET /api/games/rotation`` (crf04/statsplus#252).

Seven stored DET games; Cade is the absent player, Duren the second one.
Minutes are the rotation line (a DNP is 0); ``log`` is the stored game-log row
(min, pts, reb, ast, 3pm, fga), absent where he has none.

====  ==========  =====  =====  ========================  =======================
game  date        Cade   Duren  Ausar rotation / log      Isaiah rotation / log
====  ==========  =====  =====  ========================  =======================
g1    2026-01-01  30     25     30 / 30,10,4,2,1,8        12 / 12 min, 5 pts
g2    2026-01-02  0      25     20 / 20,17,3,8,2,13       10 / 10 min, 1 pt
g3    2026-01-03  0      0      36 / 36,18,4,9,1,15       10 / 10 min, 0 pts
g4    2026-01-04  0      0      0 (DNP, no log)           10 / 10 min, 0 pts
g5    2026-01-05  32     22     24 / 24,14,6,4,2,12       12 / 12 min, 4 pts
g6    2026-01-06  0      0      28 (no log row)           10 / 10 min, 0 pts
g7    2026-01-07  0      25     26 / 26,20,5,8,3,14       (no line)
====  ==========  =====  =====  ========================  =======================

Cameo has 0-minute lines in g2 and g3 only, and no game logs.
"""

from unittest.mock import Mock

import pytest

from app.services.game_service import GameService
from tests.support.rotation_world import (
    SEASON,
    Catalog,
    StoredLogs,
    game,
    line,
    migrated_repository,
    service_for,
    store,
)

ROTATION = "/api/games/rotation"
CADE, DUREN, AUSAR, ISAIAH, CAMEO = 10, 11, 20, 21, 22
DATES = [f"2026-01-0{n}" for n in range(1, 8)]
GAME_IDS = [f"00225000{n:02d}" for n in range(1, 8)]

CADE_MIN = [30, 0, 0, 0, 32, 0, 0]
DUREN_MIN = [25, 25, 0, 0, 22, 0, 25]
AUSAR_MIN = [30, 20, 36, 0, 24, 28, 26]
ISAIAH_MIN = [12, 10, 10, 10, 12, 10, None]
#: game index -> (min, pts, reb, ast, 3pm, fga)
AUSAR_LOGS = {
    0: (30, 10, 4, 2, 1, 8),
    1: (20, 17, 3, 8, 2, 13),
    2: (36, 18, 4, 9, 1, 15),
    4: (24, 14, 6, 4, 2, 12),
    6: (26, 20, 5, 8, 3, 14),
}
ISAIAH_PTS = {0: 5, 1: 1, 2: 0, 3: 0, 4: 4, 5: 0}

AUSAR_WITHOUT_CADE = {
    "played": 3,
    "minutes": 27.3,
    "PTS": 18.3,
    "REB": 4.0,
    "AST": 8.3,
    "3PM": 2.0,
    # (55 + 12 + 25) / 3 = 30.67; the rounded parts 18.3 + 4.0 + 8.3 would say 30.6.
    "PRA": 30.7,
    "FGA": 14.0,
    "missing_logs": 1,
}
AUSAR_WITH_CADE = {
    "played": 2,
    "minutes": 27.0,
    "PTS": 12.0,
    "REB": 5.0,
    "AST": 3.0,
    "3PM": 1.5,
    "PRA": 20.0,
    "FGA": 10.0,
    "missing_logs": 0,
}


def _build(tmp_path, logs):
    repository = migrated_repository(tmp_path)
    people = [
        (CADE, "Cade Cunningham", CADE_MIN),
        (DUREN, "Jalen Duren", DUREN_MIN),
        (AUSAR, "Ausar Thompson", AUSAR_MIN),
        (ISAIAH, "Isaiah Stewart", ISAIAH_MIN),
        (CAMEO, "Cameo Player", [None, 0, 0, None, None, None, None]),
    ]
    store(
        repository,
        *(
            game(
                GAME_IDS[i],
                DATES[i],
                "DET",
                "BOS",
                [
                    line(pid, name, "DET", minutes=mins[i])
                    for pid, name, mins in people
                    if mins[i] is not None
                ],
            )
            for i in range(7)
        ),
    )
    for i, (minutes, pts, reb, ast, fg3m, fga) in AUSAR_LOGS.items():
        logs.add(
            AUSAR,
            "DET",
            GAME_IDS[i],
            DATES[i],
            minutes=minutes,
            pts=pts,
            reb=reb,
            ast=ast,
            fg3m=fg3m,
            fga=fga,
        )
    for i, pts in ISAIAH_PTS.items():
        logs.add(ISAIAH, "DET", GAME_IDS[i], DATES[i], minutes=ISAIAH_MIN[i], pts=pts)
    for i in (0, 4):
        logs.add(CADE, "DET", GAME_IDS[i], DATES[i], minutes=CADE_MIN[i], pts=20)
    for i in (0, 1, 4, 6):
        logs.add(DUREN, "DET", GAME_IDS[i], DATES[i], minutes=DUREN_MIN[i], pts=9)
    catalog = Catalog(
        (CADE, "Cade Cunningham", "DET"),
        (DUREN, "Jalen Duren", "DET"),
        (AUSAR, "Ausar Thompson", "DET"),
        (ISAIAH, "Isaiah Stewart", "DET"),
        (CAMEO, "Cameo Player", "DET"),
    )
    return repository, catalog


@pytest.fixture
def detroit(tmp_path, dependencies):
    logs = StoredLogs()
    repository, catalog = _build(tmp_path, logs)
    dependencies.rotation_service = service_for(repository, catalog, logs=logs)
    dependencies.stored_logs = logs
    return dependencies


def _get(client, query):
    response = client.get(f"{ROTATION}?{query}")
    assert response.status_code == 200
    return response.get_json()["without"]


def _entry(body, name):
    return next(p for p in body["players"] if p["name"] == name)


def test_player_view_sides_carry_stats_with_dnp_out_of_the_averages_but_in_rotation_minutes(client, detroit):
    body = _get(client, "player_name=Ausar%20Thompson&without=Cade%20Cunningham")

    assert body["without"]["stats"] == AUSAR_WITHOUT_CADE
    assert body["with"]["stats"] == AUSAR_WITH_CADE
    # Rotation minutes keep the g4 DNP as 0 over five games: (20 + 36 + 0 + 28 + 26) / 5.
    assert {k: body["without"][k] for k in ("games", "played", "minutes")} == {
        "games": 5,
        "played": 4,
        "minutes": 22.0,
    }
    # The stats never take the DNP: three games played, not four or five.
    assert body["without"]["stats"]["played"] == 3


def test_team_view_gives_every_listed_player_stats_on_both_sides(client, detroit):
    body = _get(client, "team=DET&without=Cade%20Cunningham")

    assert [p["name"] for p in body["players"]].count("Ausar Thompson") == 1
    ausar = _entry(body, "Ausar Thompson")
    assert (ausar["without"]["stats"], ausar["with"]["stats"]) == (AUSAR_WITHOUT_CADE, AUSAR_WITH_CADE)
    # Four played without games, 1 point in total: 0.25 rounds half-up to 0.3.
    isaiah = _entry(body, "Isaiah Stewart")
    assert isaiah["without"]["stats"] == {
        "played": 4,
        "minutes": 10.0,
        "PTS": 0.3,
        "REB": 0.0,
        "AST": 0.0,
        "3PM": 0.0,
        "PRA": 0.3,
        "FGA": 0.0,
        "missing_logs": 0,
    }
    assert isaiah["with"]["stats"]["PTS"] == 4.5
    assert {k: isaiah["without"][k] for k in ("games", "played", "minutes")} == {
        "games": 4,
        "played": 4,
        "minutes": 10.0,
    }
    assert all("stats" in p["without"] and "stats" in p["with"] for p in body["players"])


def test_a_side_with_no_game_played_has_null_stats(client, detroit):
    cameo = _entry(_get(client, "team=DET&without=Cade%20Cunningham"), "Cameo Player")

    # Two 0-minute lines in the without games and no with games at all.
    assert cameo["without"]["games"] == 2 and cameo["without"]["played"] == 0
    assert cameo["without"]["stats"] is None
    assert cameo["with"]["stats"] is None


def test_two_absent_players_give_stats_over_the_games_both_missed(client, detroit):
    query = "without=Cade%20Cunningham&without=Jalen%20Duren"
    body = _get(client, f"player_name=Ausar%20Thompson&{query}")

    # Both out in g3, g4 (DNP) and g6 (no log row): only g3 has a log.
    assert body["without"]["stats"] == {
        "played": 1,
        "minutes": 36.0,
        "PTS": 18.0,
        "REB": 4.0,
        "AST": 9.0,
        "3PM": 1.0,
        "PRA": 31.0,
        "FGA": 15.0,
        "missing_logs": 1,
    }
    # Both played in g1 and g5.
    assert body["with"]["stats"] == AUSAR_WITH_CADE
    team = _get(client, f"team=DET&{query}")
    assert _entry(team, "Ausar Thompson")["without"]["stats"] == body["without"]["stats"]


def test_stats_come_from_one_batched_read_under_one_snapshot(client, detroit):
    logs = detroit.stored_logs
    _get(client, "team=DET&without=Cade%20Cunningham")

    # Every listed teammate, not the absent player, in one read.
    assert logs.batches == [([DUREN, AUSAR, ISAIAH, CAMEO], SEASON, StoredLogs.snapshot)]
    logs.batches.clear()
    _get(client, "player_name=Ausar%20Thompson&without=Cade%20Cunningham")
    assert logs.batches == [([AUSAR], SEASON, StoredLogs.snapshot)]


def test_a_game_log_row_for_another_team_is_not_a_game_played(client, tmp_path, dependencies):
    logs = StoredLogs()
    repository, catalog = _build(tmp_path, logs)
    logs.rows[AUSAR][0]["TEAM_ABBREVIATION"] = "BOS"  # g1 logged for another team
    dependencies.rotation_service = service_for(repository, catalog, logs=logs)

    body = _get(client, "player_name=Ausar%20Thompson&without=Cade%20Cunningham")

    assert body["with"]["stats"]["played"] == 1
    assert body["with"]["stats"]["missing_logs"] == 1


def test_without_stats_equal_the_game_log_players_off_result(
    client, tmp_path, dependencies, runtime_settings
):
    logs = StoredLogs()
    repository, catalog = _build(tmp_path, logs)
    games = GameService(
        None, Mock(), settings=runtime_settings, game_logs_source=logs, athlete_catalog=catalog
    )
    dependencies.game_service = games
    dependencies.rotation_service = service_for(repository, catalog, logs=games)

    stats = _get(client, "player_name=Ausar%20Thompson&without=Cade%20Cunningham")["without"]["stats"]
    response = client.get(
        f"/api/games/game_logs?player_name=Ausar Thompson&season_filter={SEASON}&players_off[]=Cade Cunningham"
    )

    assert response.status_code == 200
    rows = response.get_json()["game_logs"]
    assert len(rows) == stats["played"] == 3
    assert sum(r["PTS"] for r in rows) == 55 and sum(r["MIN"] for r in rows) == 82
    for key in ("PTS", "REB", "AST", "PRA", "FGA"):
        assert round(sum(r[key] for r in rows) / len(rows) + 1e-9, 1) == stats[key]
    assert round(sum(r["FG3M"] for r in rows) / len(rows), 1) == stats["3PM"]
