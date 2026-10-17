"""``GET /api/games/rotation?...&without=`` over a hand-countable DET season (crf04/statsplus#239).

Seven stored DET games, newest last in the table below (the API serves newest
first).  Cade is the absent player; Duren is the second one.

====  ==========  ======  ======  =====  ======  =====================
game  date        Cade    Duren   Ausar  Isaiah  note
====  ==========  ======  ======  =====  ======  =====================
g0    2025-12-30  0       (none)  25 S   10      Duren not yet on roster
g1    2026-01-01  30 S    25 S    30 SC  12      everyone plays
g2    2026-01-03  0       25 S    20 S   20 S    Cade out, Duren plays
g3    2026-01-05  0       0       36 SC  30 SC   both out
g4    2026-01-07  0       0       0      28 S    both out, Ausar DNP
g5    2026-01-09  0       22 S    24 S   14      Cade out, Duren plays
g6    2026-01-11  32 S    0       18     12      Cade plays, Duren out
====  ==========  ======  ======  =====  ======  =====================
"""

import pytest

from tests.support.rotation_world import Catalog, game, line, migrated_repository, service_for, store

ROTATION = "/api/games/rotation"
CADE, DUREN, AUSAR, ISAIAH, LATE, SAM_A, SAM_B = 10, 11, 20, 21, 22, 30, 31


def _g(game_id, date, *lines):
    return game(f"00225000{game_id:02d}", date, "DET", "BOS", lines)


@pytest.fixture
def detroit(tmp_path, dependencies):
    repository = migrated_repository(tmp_path)
    cade = lambda m, **k: line(CADE, "Cade Cunningham", "DET", minutes=m, **k)  # noqa: E731
    duren = lambda m, **k: line(DUREN, "Jalen Duren", "DET", minutes=m, **k)  # noqa: E731
    ausar = lambda m, **k: line(AUSAR, "Ausar Thompson", "DET", minutes=m, **k)  # noqa: E731
    isaiah = lambda m, **k: line(ISAIAH, "Isaiah Stewart", "DET", minutes=m, **k)  # noqa: E731
    late = lambda m: line(LATE, "Late Joiner", "DET", minutes=m)  # noqa: E731
    store(
        repository,
        _g(0, "2025-12-30", cade(0), ausar(25, started=True), isaiah(10)),
        _g(
            1,
            "2026-01-01",
            cade(30, started=True),
            duren(25, started=True),
            ausar(30, started=True, closed=True),
            isaiah(12),
            line(SAM_A, "Sam Same", "DET", minutes=5),
            line(SAM_B, "Sam Same", "DET", minutes=5),
        ),
        _g(
            2,
            "2026-01-03",
            cade(0),
            duren(25, started=True),
            ausar(20, started=True),
            isaiah(20, started=True),
        ),
        _g(
            3,
            "2026-01-05",
            cade(0),
            duren(0),
            ausar(
                36,
                started=True,
                closed=True,
                periods=(9, 9, 9, 9, 0),
                stints=((1, "12:00", 2, "6:00", 18), (3, "12:00", 4, "0:00", 18)),
            ),
            isaiah(30, started=True, closed=True),
        ),
        _g(4, "2026-01-07", cade(0), duren(0), ausar(0), isaiah(28, started=True)),
        _g(
            5,
            "2026-01-09",
            cade(0),
            duren(22, started=True),
            ausar(24, started=True),
            isaiah(14),
            late(10),
        ),
        _g(6, "2026-01-11", cade(32, started=True), duren(0), ausar(18), isaiah(12), late(10)),
    )
    dependencies.rotation_service = service_for(
        repository,
        Catalog(
            (CADE, "Cade Cunningham", "DET"),
            (DUREN, "Jalen Duren", "DET"),
            (AUSAR, "Ausar Thompson", "DET"),
            (ISAIAH, "Isaiah Stewart", "DET"),
        ),
    )
    return dependencies


def _get(client, query):
    return client.get(f"{ROTATION}?{query}")


def test_player_without_one_player_counts_dnp_subject_as_zero_minutes(client, detroit):
    response = _get(client, "player_name=Ausar%20Thompson&without=Cade%20Cunningham")

    assert response.status_code == 200
    assert response.get_json() == {
        "season": "2025-26",
        "player": {"name": "Ausar Thompson", "team": "DET"},
        "without": {
            "absent": ["Cade Cunningham"],
            # g0, g2, g3, g4 (DNP, 0 minutes), g5: 25+20+36+0+24 = 105 over 5 games.
            "without": {
                "games": 5,
                "played": 4,
                "starts": 4,
                "minutes": 21.0,
                "closed": 1,
                "minutes_by_period": {"Q1": 1.8, "Q2": 1.8, "Q3": 1.8, "Q4": 1.8, "OT": 0.0},
                "stints": 0.4,
            },
            # g1 and g6: 30 + 18 over 2 games.
            "with": {
                "games": 2,
                "played": 2,
                "starts": 1,
                "minutes": 24.0,
                "closed": 1,
                "minutes_by_period": {"Q1": 0.0, "Q2": 0.0, "Q3": 0.0, "Q4": 0.0, "OT": 0.0},
                "stints": 0.0,
            },
            "minutes_change": -3.0,
            "insufficient": False,
            "games": [
                {
                    "game_date": "2026-01-09",
                    "matchup": "DET vs. BOS",
                    "started": True,
                    "minutes": 24.0,
                    "closed": False,
                },
                {
                    "game_date": "2026-01-07",
                    "matchup": "DET vs. BOS",
                    "started": False,
                    "minutes": 0.0,
                    "closed": False,
                },
                {
                    "game_date": "2026-01-05",
                    "matchup": "DET vs. BOS",
                    "started": True,
                    "minutes": 36.0,
                    "closed": True,
                },
                {
                    "game_date": "2026-01-03",
                    "matchup": "DET vs. BOS",
                    "started": True,
                    "minutes": 20.0,
                    "closed": False,
                },
                {
                    "game_date": "2025-12-30",
                    "matchup": "DET vs. BOS",
                    "started": True,
                    "minutes": 25.0,
                    "closed": False,
                },
            ],
        },
    }


def test_two_absent_names_exclude_mixed_and_pre_roster_games_and_flag_a_short_sample(client, detroit):
    body = _get(
        client, "player_name=Ausar%20Thompson&without=Cade%20Cunningham&without=Jalen%20Duren"
    ).get_json()["without"]

    # Only g3 and g4 have both out: g0 has no Duren line (pre-roster), g2/g5/g6 are mixed.
    assert body["absent"] == ["Cade Cunningham", "Jalen Duren"]
    assert {k: body["without"][k] for k in ("games", "played", "starts", "minutes", "closed")} == {
        "games": 2,
        "played": 1,
        "starts": 1,
        "minutes": 18.0,
        "closed": 1,
    }
    assert {k: body["with"][k] for k in ("games", "played", "starts", "minutes", "closed")} == {
        "games": 1,
        "played": 1,
        "starts": 1,
        "minutes": 30.0,
        "closed": 1,
    }
    assert (body["minutes_change"], body["insufficient"]) == (-12.0, True)
    assert [g["game_date"] for g in body["games"]] == ["2026-01-07", "2026-01-05"]


def test_team_without_lists_starters_and_minute_changes(client, detroit):
    response = _get(client, "team=det&without=Cade%20Cunningham")

    assert response.status_code == 200
    body = response.get_json()
    assert body["season"] == "2025-26" and body["team"] == "DET"
    without = body["without"]
    assert {k: without[k] for k in ("absent", "games_without", "games_with", "insufficient")} == {
        "absent": ["Cade Cunningham"],
        "games_without": 5,
        "games_with": 2,
        "insufficient": False,
    }
    assert without["starters"] == [
        {"name": "Ausar Thompson", "starts_without": 4, "starts_with": 1},
        {"name": "Isaiah Stewart", "starts_without": 3, "starts_with": 0},
        {"name": "Jalen Duren", "starts_without": 2, "starts_with": 1},
    ]
    # |change| desc: Isaiah 8.4, Ausar 3.0, Duren 0.8 (11.75 - 12.5, half-up away from zero), Late 0.0.
    assert [(p["name"], p["minutes_change"]) for p in without["players"]] == [
        ("Isaiah Stewart", 8.4),
        ("Ausar Thompson", -3.0),
        ("Jalen Duren", -0.8),
        ("Late Joiner", 0.0),
    ]
    assert without["players"][0] == {
        "name": "Isaiah Stewart",
        "without": {"games": 5, "played": 5, "starts": 3, "minutes": 20.4, "closed": 1},
        "with": {"games": 2, "played": 2, "starts": 0, "minutes": 12.0, "closed": 0},
        "minutes_change": 8.4,
        "insufficient": False,
    }


def test_team_without_two_names_is_insufficient(client, detroit):
    without = _get(client, "team=DET&without=Cade%20Cunningham&without=Jalen%20Duren").get_json()["without"]

    assert (without["games_without"], without["games_with"], without["insufficient"]) == (2, 1, True)


@pytest.mark.parametrize(
    ("query", "parameters"),
    [
        ("game_id=0022500003&without=Cade%20Cunningham", ["without"]),
        ("player_name=Ausar%20Thompson&without=Cade%20Cunningham&last_n=5", ["last_n", "without"]),
        ("team=DET&without=Cade%20Cunningham&last_n=5", ["last_n", "without"]),
    ],
)
def test_without_is_rejected_with_game_id_and_last_n(client, detroit, query, parameters):
    response = _get(client, query)

    assert response.status_code == 400
    details = response.get_json()["error"]["details"]
    assert details["parameter"] == "without" and details["parameters"] == parameters


def test_unknown_without_name_is_a_400_naming_the_parameter(client, detroit):
    response = _get(client, "player_name=Ausar%20Thompson&without=Nobody%20Here")

    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["parameter"] == "without"


def test_ambiguous_without_name_lists_candidates(client, detroit):
    response = _get(client, "team=DET&without=Sam%20Same")

    assert response.status_code == 400
    details = response.get_json()["error"]["details"]
    assert details["parameter"] == "without"
    assert details["candidates"] == [
        {"name": "Sam Same", "team": "DET"},
        {"name": "Sam Same", "team": "DET"},
    ]


def test_the_subject_cannot_be_named_in_without(client, detroit):
    response = _get(client, "player_name=Ausar%20Thompson&without=ausar%20thompson")

    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["parameter"] == "without"


def test_existing_views_are_unchanged_without_the_parameter(client, detroit):
    body = _get(client, "team=DET&last_n=1").get_json()

    assert "without" not in body
    assert body["games"][0]["starters"] == ["Cade Cunningham"]


def test_team_players_with_equal_minute_changes_sort_by_name(client, tmp_path, dependencies):
    repository = migrated_repository(tmp_path)
    store(
        repository,
        _g(
            1,
            "2026-01-01",
            line(CADE, "Cade Cunningham", "DET", minutes=30),
            line(AUSAR, "Zed", "DET", minutes=20),
            line(ISAIAH, "Abe", "DET", minutes=20),
        ),
        *(
            _g(
                n,
                f"2026-01-0{n}",
                line(CADE, "Cade Cunningham", "DET", minutes=0),
                line(AUSAR, "Zed", "DET", minutes=22),
                line(ISAIAH, "Abe", "DET", minutes=18),
            )
            for n in (2, 3, 4)
        ),
    )
    dependencies.rotation_service = service_for(repository, Catalog())

    body = _get(client, "team=DET&without=Cade%20Cunningham").get_json()

    assert [(p["name"], p["minutes_change"]) for p in body["without"]["players"]] == [
        ("Abe", -2.0),
        ("Zed", 2.0),
    ]


def test_each_team_player_is_insufficient_on_his_own_without_games(client, tmp_path, dependencies):
    repository = migrated_repository(tmp_path)
    cade_out = line(CADE, "Cade Cunningham", "DET", minutes=0)
    store(
        repository,
        _g(
            1,
            "2026-01-01",
            line(CADE, "Cade Cunningham", "DET", minutes=30),
            line(AUSAR, "Steady", "DET", minutes=20),
        ),
        _g(2, "2026-01-02", cade_out, line(AUSAR, "Steady", "DET", minutes=20)),
        # Traded in: on the box score for only two of the four without games.
        _g(
            3,
            "2026-01-03",
            cade_out,
            line(AUSAR, "Steady", "DET", minutes=20),
            line(ISAIAH, "Traded In", "DET", minutes=15),
        ),
        _g(
            4,
            "2026-01-04",
            cade_out,
            line(AUSAR, "Steady", "DET", minutes=20),
            line(ISAIAH, "Traded In", "DET", minutes=15),
        ),
        _g(5, "2026-01-05", cade_out, line(AUSAR, "Steady", "DET", minutes=20)),
    )
    dependencies.rotation_service = service_for(repository, Catalog())

    without = _get(client, "team=DET&without=Cade%20Cunningham").get_json()["without"]

    assert without["games_without"] == 4 and without["insufficient"] is False
    assert [(p["name"], p["without"]["games"], p["insufficient"]) for p in without["players"]] == [
        ("Steady", 4, False),
        ("Traded In", 2, True),
    ]
