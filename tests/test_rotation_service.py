"""The stored-rotation read: player and team documents (crf04/statsplus#223)."""

import pytest

from app.errors import InvalidInputError, ResourceNotFoundError
from tests.support.rotation_world import (
    Catalog,
    game,
    line,
    migrated_repository,
    service_for,
    store,
)

AUSAR = 1641709
CADE = 1630595
ISAIAH = 1630168


def _detroit_world(tmp_path):
    """Five DET games, newest first below.  Ausar sat out 04-08 and was absent
    from the 04-05 box score; Isaiah is a bench player whose role grew."""
    repository = migrated_repository(tmp_path)
    store(
        repository,
        game(
            "0022501200",
            "2026-04-12",
            "CHA",
            "DET",
            [
                line(
                    AUSAR,
                    "Ausar Thompson",
                    "DET",
                    minutes=33,
                    started=True,
                    closed=True,
                    periods=(9, 6, 9, 9, 0),
                    stints=(
                        (1, "12:00", 1, "3:00", 9),
                        (2, "12:00", 2, "6:00", 6),
                        (3, "12:00", 3, "3:00", 9),
                        (4, "12:00", 4, "3:00", 9),
                    ),
                ),
                line(
                    CADE,
                    "Cade Cunningham",
                    "DET",
                    minutes=36,
                    started=True,
                    closed=True,
                    periods=(9, 9, 9, 9, 0),
                    stints=((1, "12:00", 4, "0:00", 36),),
                ),
                line(
                    ISAIAH,
                    "Isaiah Stewart",
                    "DET",
                    minutes=12,
                    periods=(0, 6, 0, 6, 0),
                    stints=((2, "6:00", 2, "0:00", 6), (4, "6:00", 4, "0:00", 6)),
                ),
                line(77, "Not On DET", "CHA", minutes=20, started=True),
            ],
        ),
        game(
            "0022501190",
            "2026-04-10",
            "DET",
            "BOS",
            [
                line(
                    AUSAR,
                    "Ausar Thompson",
                    "DET",
                    minutes=30,
                    started=True,
                    periods=(8, 7, 8, 7, 0),
                    stints=(
                        (1, "12:00", 1, "4:00", 8),
                        (2, "12:00", 2, "5:00", 7),
                        (3, "12:00", 3, "4:00", 8),
                        (4, "12:00", 4, "5:00", 7),
                    ),
                ),
                line(
                    CADE,
                    "Cade Cunningham",
                    "DET",
                    minutes=34,
                    started=True,
                    closed=True,
                    periods=(9, 8, 9, 8, 0),
                    stints=((1, "12:00", 4, "0:00", 34),),
                ),
                line(
                    ISAIAH,
                    "Isaiah Stewart",
                    "DET",
                    minutes=18,
                    periods=(3, 5, 5, 5, 0),
                    stints=(
                        (1, "3:00", 1, "0:00", 3),
                        (2, "5:00", 2, "0:00", 5),
                        (3, "7:00", 3, "2:00", 5),
                        (4, "7:00", 4, "2:00", 5),
                    ),
                ),
            ],
        ),
        game(
            "0022501180",
            "2026-04-08",
            "MIA",
            "DET",
            [
                line(AUSAR, "Ausar Thompson", "DET", minutes=0),
                line(
                    CADE,
                    "Cade Cunningham",
                    "DET",
                    minutes=32,
                    started=True,
                    periods=(8, 8, 8, 8, 0),
                    stints=((1, "12:00", 4, "0:00", 32),),
                ),
                line(
                    ISAIAH,
                    "Isaiah Stewart",
                    "DET",
                    minutes=15,
                    periods=(5, 0, 5, 5, 0),
                    stints=((1, "7:00", 1, "0:00", 5),),
                ),
            ],
        ),
        game(
            "0022501170",
            "2026-04-05",
            "DET",
            "NYK",
            [
                line(
                    CADE,
                    "Cade Cunningham",
                    "DET",
                    minutes=30,
                    started=True,
                    closed=True,
                    periods=(7, 8, 7, 8, 0),
                    stints=((1, "12:00", 4, "0:00", 30),),
                ),
                line(
                    ISAIAH,
                    "Isaiah Stewart",
                    "DET",
                    minutes=9,
                    periods=(3, 0, 3, 3, 0),
                    stints=((1, "3:00", 1, "0:00", 3),),
                ),
            ],
        ),
        game(
            "0022500500",
            "2026-01-05",
            "DET",
            "LAL",
            [
                line(
                    AUSAR,
                    "Ausar Thompson",
                    "DET",
                    minutes=18,
                    started=True,
                    closed=True,
                    periods=(5, 4, 5, 4, 0),
                    stints=((1, "12:00", 1, "7:00", 5),),
                ),
                line(
                    CADE,
                    "Cade Cunningham",
                    "DET",
                    minutes=40,
                    started=True,
                    closed=True,
                    periods=(10, 10, 10, 10, 0),
                    stints=((1, "12:00", 4, "0:00", 40),),
                ),
                line(
                    ISAIAH,
                    "Isaiah Stewart",
                    "DET",
                    minutes=6,
                    periods=(0, 0, 3, 3, 0),
                    stints=((3, "3:00", 3, "0:00", 3),),
                ),
            ],
        ),
    )
    catalog = Catalog(
        (AUSAR, "Ausar Thompson", "DET"),
        (CADE, "Cade Cunningham", "DET"),
        (ISAIAH, "Isaiah Stewart", "DET"),
    )
    return service_for(repository, catalog), repository


def test_player_view_averages_minutes_over_games_played_and_compares_with_the_season(tmp_path):
    service, _ = _detroit_world(tmp_path)

    document = service.get_player_rotation(
        player_name="ausar thompson", player_team=None, last_n=3, season=None
    )

    assert document["season"] == "2025-26"
    assert document["last_n"] == 3
    assert document["player"] == {"name": "Ausar Thompson", "team": "DET"}
    # Recent: 04-12, 04-10, 04-08 (a 0-minute game).  Minutes (33+30)/2 played = 31.5.
    assert document["recent"] == {
        "games": 3,
        "played": 2,
        "starts": 2,
        "minutes": 31.5,
        "stints": 4.0,
        "closed": 1,
        "minutes_by_period": {"Q1": 8.5, "Q2": 6.5, "Q3": 8.5, "Q4": 8.0, "OT": 0.0},
    }
    # Season: DET's five games from his first (01-05), 04-05 missed; (33+30+18)/3 played = 27.0.
    assert document["season_summary"] == {
        "games": 5,
        "played": 3,
        "starts": 3,
        "minutes": 27.0,
        "stints": 3.0,
        "closed": 2,
        "minutes_by_period": {"Q1": 7.3, "Q2": 5.7, "Q3": 7.3, "Q4": 6.7, "OT": 0.0},
    }


def test_player_view_lists_recent_games_newest_first_with_stints_and_matchups(tmp_path):
    service, _ = _detroit_world(tmp_path)

    games = service.get_player_rotation(
        player_name="Ausar Thompson", player_team=None, last_n=3, season=None
    )["games"]

    assert games[0] == {
        "game_date": "2026-04-12",
        "matchup": "DET @ CHA",
        "started": True,
        "minutes": 33.0,
        "closed": True,
        "on_box_score": True,
        "minutes_by_period": {"Q1": 9.0, "Q2": 6.0, "Q3": 9.0, "Q4": 9.0, "OT": 0.0},
        "stints": [
            {"in": {"period": 1, "clock": "12:00"}, "out": {"period": 1, "clock": "3:00"}, "minutes": 9.0},
            {"in": {"period": 2, "clock": "12:00"}, "out": {"period": 2, "clock": "6:00"}, "minutes": 6.0},
            {"in": {"period": 3, "clock": "12:00"}, "out": {"period": 3, "clock": "3:00"}, "minutes": 9.0},
            {"in": {"period": 4, "clock": "12:00"}, "out": {"period": 4, "clock": "3:00"}, "minutes": 9.0},
        ],
    }
    assert [(g["game_date"], g["matchup"], g["minutes"]) for g in games] == [
        ("2026-04-12", "DET @ CHA", 33.0),
        ("2026-04-10", "DET vs. BOS", 30.0),
        ("2026-04-08", "DET @ MIA", 0.0),
    ]
    assert games[2] == {
        "game_date": "2026-04-08",
        "matchup": "DET @ MIA",
        "started": False,
        "minutes": 0.0,
        "closed": False,
        "on_box_score": True,
        "minutes_by_period": {"Q1": 0.0, "Q2": 0.0, "Q3": 0.0, "Q4": 0.0, "OT": 0.0},
        "stints": [],
    }


def test_a_team_game_inside_his_window_without_his_line_is_a_missed_game(tmp_path):
    service, _ = _detroit_world(tmp_path)

    document = service.get_player_rotation(
        player_name="Ausar Thompson", player_team=None, last_n=4, season=None
    )

    # DET's 04-05 game has no Ausar line: a missed game, beside his 0-minute 04-08 line.
    assert [(g["game_date"], g["minutes"], g["on_box_score"]) for g in document["games"]] == [
        ("2026-04-12", 33.0, True),
        ("2026-04-10", 30.0, True),
        ("2026-04-08", 0.0, True),
        ("2026-04-05", 0.0, False),
    ]
    assert document["recent"]["games"] == 4
    assert document["recent"]["starts"] == 2
    assert document["recent"]["played"] == 2
    assert document["recent"]["minutes"] == 31.5


def test_last_n_defaults_are_the_callers_and_a_long_window_stops_at_stored_games(tmp_path):
    service, _ = _detroit_world(tmp_path)

    document = service.get_player_rotation(
        player_name="Cade Cunningham", player_team=None, last_n=20, season=None
    )

    assert document["recent"]["games"] == 5
    assert document["recent"]["minutes"] == document["season_summary"]["minutes"] == 34.4
    assert len(document["games"]) == 5


def test_team_view_ranks_players_by_how_far_their_minutes_moved(tmp_path):
    service, _ = _detroit_world(tmp_path)

    document = service.get_team_rotation(team="DET", last_n=3, season=None)

    assert document["games"] == [
        {
            "game_date": "2026-04-12",
            "matchup": "DET @ CHA",
            "starters": ["Ausar Thompson", "Cade Cunningham"],
        },
        {
            "game_date": "2026-04-10",
            "matchup": "DET vs. BOS",
            "starters": ["Ausar Thompson", "Cade Cunningham"],
        },
        {"game_date": "2026-04-08", "matchup": "DET @ MIA", "starters": ["Cade Cunningham"]},
    ]
    assert document["players"] == [
        {
            "name": "Ausar Thompson",
            "recent": {"games": 3, "played": 2, "starts": 2, "minutes": 31.5, "closed": 1},
            "season_summary": {"games": 5, "played": 3, "starts": 3, "minutes": 27.0, "closed": 2},
            "minutes_change": 4.5,
        },
        {
            "name": "Isaiah Stewart",
            "recent": {"games": 3, "played": 3, "starts": 0, "minutes": 15.0, "closed": 0},
            "season_summary": {"games": 5, "played": 5, "starts": 0, "minutes": 12.0, "closed": 0},
            "minutes_change": 3.0,
        },
        {
            "name": "Cade Cunningham",
            "recent": {"games": 3, "played": 3, "starts": 3, "minutes": 34.0, "closed": 2},
            "season_summary": {"games": 5, "played": 5, "starts": 5, "minutes": 34.4, "closed": 4},
            "minutes_change": -0.4,
        },
    ]
    assert "Not On DET" not in [player["name"] for player in document["players"]]


def test_minutes_change_uses_unrounded_averages(tmp_path):
    repository = migrated_repository(tmp_path)
    # Recent average 30.04 vs season 29.96 would round to 30.0 vs 30.0 but the
    # difference 0.08 must show as 0.1, not 0.0.
    store(
        repository,
        game(
            "0022500001",
            "2026-01-01",
            "DET",
            "BOS",
            [
                line(1, "Unit One", "DET", minutes=29.88, started=True),
            ],
        ),
        game(
            "0022500002",
            "2026-01-03",
            "DET",
            "BOS",
            [
                line(1, "Unit One", "DET", minutes=30.04, started=True),
            ],
        ),
    )
    service = service_for(repository, Catalog((1, "Unit One", "DET")))

    document = service.get_team_rotation(team="DET", last_n=1, season=None)

    [player] = document["players"]
    assert player["recent"]["minutes"] == 30.0
    assert player["season_summary"]["minutes"] == 30.0
    assert player["minutes_change"] == 0.1


def test_a_traded_player_reads_recent_on_his_current_team_and_season_across_teams(tmp_path):
    repository = migrated_repository(tmp_path)
    store(
        repository,
        game(
            "0022500001",
            "2026-01-01",
            "LAL",
            "BOS",
            [
                line(9, "Mover Mike", "LAL", minutes=20, started=True, closed=True),
            ],
        ),
        game(
            "0022500000",
            "2026-01-30",
            "DET",
            "MIA",
            [
                line(8, "Someone Else", "DET", minutes=30, started=True),
            ],
        ),
        game(
            "0022500002",
            "2026-02-01",
            "DET",
            "BOS",
            [
                line(9, "Mover Mike", "DET", minutes=30, started=True),
            ],
        ),
        game(
            "0022500003",
            "2026-02-03",
            "DET",
            "CHA",
            [
                line(9, "Mover Mike", "DET", minutes=10),
            ],
        ),
    )
    service = service_for(repository, Catalog((9, "Mover Mike", "DET")))

    document = service.get_player_rotation(player_name="Mover Mike", player_team=None, last_n=5, season=None)

    assert document["player"] == {"name": "Mover Mike", "team": "DET"}
    # His DET games are the two he is listed for; the pre-trade DET game he is
    # not on the box score of, and the LAL game, are not counted.
    assert document["recent"]["games"] == 2
    assert document["recent"]["minutes"] == 20.0
    assert document["season_summary"]["games"] == 3
    assert document["season_summary"]["minutes"] == 20.0
    assert document["season_summary"]["starts"] == 2


def test_namesakes_with_rotation_data_are_ambiguous_until_narrowed_by_team(tmp_path):
    repository = migrated_repository(tmp_path)
    store(
        repository,
        game(
            "0022500001",
            "2026-01-01",
            "DET",
            "BOS",
            [
                line(101, "Jalen Williams", "DET", minutes=25, started=True),
                line(102, "Jalen Williams", "BOS", minutes=15),
            ],
        ),
    )
    catalog = Catalog(
        (101, "Jalen Williams", "DET"),
        (102, "Jalen Williams", "BOS"),
        (103, "Jalen Williams", "LAL"),
    )
    service = service_for(repository, catalog)

    with pytest.raises(InvalidInputError) as ambiguous:
        service.get_player_rotation(player_name="Jalen Williams", player_team=None, last_n=5, season=None)

    assert ambiguous.value.public_details == {
        "parameter": "player_name",
        "parameters": ["player_name"],
        "candidates": [
            {"name": "Jalen Williams", "team": "BOS"},
            {"name": "Jalen Williams", "team": "DET"},
        ],
    }
    narrowed = service.get_player_rotation(
        player_name="Jalen Williams", player_team="BOS", last_n=5, season=None
    )
    assert narrowed["player"] == {"name": "Jalen Williams", "team": "BOS"}
    assert narrowed["recent"]["minutes"] == 15.0


def test_missing_players_teams_and_seasons_are_not_found(tmp_path):
    service, _ = _detroit_world(tmp_path)
    service.athlete_catalog.athletes += ((555, "Rookie Nodata", "DET"),)

    with pytest.raises(ResourceNotFoundError, match="player was not found"):
        service.get_player_rotation(player_name="Nobody Real", player_team=None, last_n=5, season=None)
    with pytest.raises(ResourceNotFoundError) as no_rows:
        service.get_player_rotation(player_name="Rookie Nodata", player_team=None, last_n=5, season=None)
    assert no_rows.value.public_details == {"reason": "no_rotation_data"}
    with pytest.raises(ResourceNotFoundError):
        service.get_team_rotation(team="MIL", last_n=5, season=None)
    with pytest.raises(ResourceNotFoundError):
        service.get_team_rotation(team="DET", last_n=5, season="2024-25")


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.get_team_rotation(team="ZZZ", last_n=5, season=None),
        lambda s: s.get_player_rotation(
            player_name="Cade Cunningham", player_team="ZZZ", last_n=5, season=None
        ),
    ],
)
def test_an_unknown_tricode_is_invalid_input(tmp_path, call):
    service, _ = _detroit_world(tmp_path)

    with pytest.raises(InvalidInputError, match="not an NBA tricode"):
        call(service)


def test_the_default_season_falls_back_to_the_latest_season_with_rotation_data(tmp_path):
    repository = migrated_repository(tmp_path)
    store(
        repository,
        game(
            "0022500001",
            "2026-01-01",
            "DET",
            "BOS",
            [
                line(1, "Unit One", "DET", minutes=30, started=True),
            ],
        ),
    )
    catalog = Catalog((1, "Unit One", "DET"))

    fallback = service_for(repository, catalog, default_season="2026-27")
    document = fallback.get_team_rotation(team="DET", last_n=5, season=None)

    assert document["season"] == "2025-26"
    with pytest.raises(ResourceNotFoundError):
        fallback.get_team_rotation(team="DET", last_n=5, season="2026-27")


def test_team_view_drops_players_who_left_before_the_recent_games(tmp_path):
    repository = migrated_repository(tmp_path)
    store(
        repository,
        game(
            "0022500001",
            "2026-01-01",
            "DET",
            "BOS",
            [
                line(1, "Stayer", "DET", minutes=30, started=True),
                line(2, "Departed", "DET", minutes=20),
            ],
        ),
        game(
            "0022500002",
            "2026-01-03",
            "DET",
            "BOS",
            [
                line(1, "Stayer", "DET", minutes=32, started=True),
                line(3, "Inactive Ian", "DET", minutes=0),
            ],
        ),
    )
    service = service_for(repository, Catalog((1, "Stayer", "DET"), (2, "Departed", "BOS")))

    document = service.get_team_rotation(team="DET", last_n=1, season=None)
    wider = service.get_team_rotation(team="DET", last_n=2, season=None)

    assert [p["name"] for p in document["players"]] == ["Stayer", "Inactive Ian"]
    assert sorted(p["name"] for p in wider["players"]) == ["Departed", "Inactive Ian", "Stayer"]


# -- team games he missed (crf04/statsplus#483) ------------------------------
#
# 2026-27 LiveData box scores list only active players, so a game he missed
# has no line.  PHI's games, newest first; Bona's minutes, ``-`` for no line.
#
#   11-08 -   11-06 -   11-04 24   11-02 20   10-31 28   10-29 0 (2025-26-style
#   inactive line)   10-27 -   10-25 - (both before his first PHI game)

BONA, MAXEY, GONE = 1641737, 1630178, 1630999
CURRENT = "2026-27"


def _phi_world(tmp_path, catalog=None):
    repository = migrated_repository(tmp_path)
    dates = ("11-08", "11-06", "11-04", "11-02", "10-31", "10-29", "10-27", "10-25")
    bona = {"11-04": 24, "11-02": 20, "10-31": 28, "10-29": 0}
    gone = {"10-25": 15, "10-27": 14, "10-29": 16}
    store(
        repository,
        *(
            game(
                f"00226001{n:02d}",
                f"2026-{day}",
                "PHI",
                "BOS",
                [
                    line(MAXEY, "Tyrese Maxey", "PHI", minutes=36, started=True),
                    *([line(BONA, "Adem Bona", "PHI", minutes=bona[day])] if day in bona else []),
                    *([line(GONE, "Gone Gary", "PHI", minutes=gone[day])] if day in gone else []),
                ],
            )
            for n, day in enumerate(dates)
        ),
        game(
            "0022600200",
            "2026-11-01",
            "NYK",
            "MIA",
            [line(GONE, "Gone Gary", "NYK", minutes=22, started=True)],
        ),
        season=CURRENT,
    )
    catalog = catalog or Catalog(
        (BONA, "Adem Bona", "PHI"),
        (MAXEY, "Tyrese Maxey", "PHI"),
        (GONE, "Gone Gary", "NYK"),
    )
    return service_for(repository, catalog, default_season=CURRENT)


def test_recent_counts_the_team_games_he_missed_and_averages_minutes_over_games_played(tmp_path):
    service = _phi_world(tmp_path)

    document = service.get_player_rotation(player_name="Adem Bona", player_team=None, last_n=5, season=None)

    assert [(g["game_date"], g["minutes"], g["on_box_score"]) for g in document["games"]] == [
        ("2026-11-08", 0.0, False),
        ("2026-11-06", 0.0, False),
        ("2026-11-04", 24.0, True),
        ("2026-11-02", 20.0, True),
        ("2026-10-31", 28.0, True),
    ]
    assert document["games"][0] == {
        "game_date": "2026-11-08",
        "matchup": "PHI vs. BOS",
        "started": False,
        "minutes": 0.0,
        "closed": False,
        "on_box_score": False,
        "minutes_by_period": {"Q1": 0.0, "Q2": 0.0, "Q3": 0.0, "Q4": 0.0, "OT": 0.0},
        "stints": [],
    }
    # (24 + 20 + 28) / 3 played, not / 5.
    assert document["recent"] == {
        "games": 5,
        "played": 3,
        "starts": 0,
        "minutes": 24.0,
        "stints": 0.0,
        "closed": 0,
        "minutes_by_period": {"Q1": 0.0, "Q2": 0.0, "Q3": 0.0, "Q4": 0.0, "OT": 0.0},
    }


def test_season_counts_team_games_inside_his_window_and_an_inactive_line_once(tmp_path):
    service = _phi_world(tmp_path)

    document = service.get_player_rotation(player_name="Adem Bona", player_team=None, last_n=5, season=None)

    # 10-29 (his 0-minute line) through 11-08: six team games, three played.
    # 10-27 and 10-25 are before his first PHI game, outside his window.
    assert (document["season_summary"]["games"], document["season_summary"]["played"]) == (6, 3)
    assert document["season_summary"]["minutes"] == 24.0


def test_games_after_a_move_to_another_team_are_not_counted_for_the_old_team(tmp_path):
    service = _phi_world(tmp_path)

    document = service.get_player_rotation(player_name="Gone Gary", player_team=None, last_n=5, season=None)

    # His PHI window closes at his first NYK game (11-01); PHI's later games are not his.
    assert document["player"] == {"name": "Gone Gary", "team": "NYK"}
    assert (document["recent"]["games"], document["recent"]["played"]) == (1, 1)
    assert (document["season_summary"]["games"], document["season_summary"]["played"]) == (5, 4)
    assert document["season_summary"]["minutes"] == 16.8


def test_team_view_counts_missed_games_and_keeps_a_player_who_missed_them_all(tmp_path):
    service = _phi_world(tmp_path)

    document = service.get_team_rotation(team="PHI", last_n=2, season=None)

    by_name = {player["name"]: player for player in document["players"]}
    assert sorted(by_name) == ["Adem Bona", "Tyrese Maxey"]
    assert by_name["Adem Bona"]["recent"] == {
        "games": 2,
        "played": 0,
        "starts": 0,
        "minutes": None,
        "closed": 0,
    }
    assert by_name["Adem Bona"]["minutes_change"] is None
    wider = {p["name"]: p for p in service.get_team_rotation(team="PHI", last_n=5, season=None)["players"]}
    assert wider["Adem Bona"]["recent"] == {
        "games": 5,
        "played": 3,
        "starts": 0,
        "minutes": 24.0,
        "closed": 0,
    }
    assert wider["Adem Bona"]["season_summary"] == {
        "games": 6,
        "played": 3,
        "starts": 0,
        "minutes": 24.0,
        "closed": 0,
    }
    assert wider["Adem Bona"]["minutes_change"] == 0.0


def _team_view_after_missing_both(service):
    return [
        player["name"] for player in service.get_team_rotation(team="PHI", last_n=2, season=None)["players"]
    ]


@pytest.mark.parametrize(
    "catalog",
    [
        pytest.param(Catalog((MAXEY, "Tyrese Maxey", "PHI")), id="no catalog row"),
        pytest.param(Catalog((BONA, "Adem Bona", None), (MAXEY, "Tyrese Maxey", "PHI")), id="null team"),
    ],
)
def test_team_view_keeps_a_player_who_missed_them_all_when_the_catalog_is_silent(tmp_path, catalog):
    service = _phi_world(tmp_path, catalog=catalog)

    team = {p["name"]: p for p in service.get_team_rotation(team="PHI", last_n=2, season=None)["players"]}
    # The player view resolves him by catalog row; his counts do not depend on it.
    named = service_for(service.repository, Catalog((BONA, "Adem Bona", "PHI")))
    player = named.get_player_rotation(player_name="Adem Bona", player_team=None, last_n=2, season=None)

    assert sorted(team) == ["Adem Bona", "Tyrese Maxey"]
    assert team["Adem Bona"]["recent"] == {"games": 2, "played": 0, "starts": 0, "minutes": None, "closed": 0}
    assert team["Adem Bona"]["season_summary"] == {
        "games": 6,
        "played": 3,
        "starts": 0,
        "minutes": 24.0,
        "closed": 0,
    }
    for summary in ("recent", "season_summary"):
        assert team["Adem Bona"][summary] == {
            key: player[summary][key] for key in ("games", "played", "starts", "minutes", "closed")
        }


@pytest.mark.parametrize(
    "bona",
    [
        pytest.param((BONA, "Adem Bona", "NYK"), id="another team"),
        pytest.param((BONA, "Adem Bona", "PHI", "inactive"), id="off every roster"),
        pytest.param((BONA, "Adem Bona", None, "inactive"), id="off every roster, no team"),
    ],
)
def test_team_view_drops_a_player_who_missed_them_all_when_the_catalog_says_he_left(tmp_path, bona):
    service = _phi_world(tmp_path, catalog=Catalog(bona, (MAXEY, "Tyrese Maxey", "PHI")))

    player = service.get_player_rotation(player_name="Adem Bona", player_team=None, last_n=2, season=None)

    # The team view drops him on that evidence; the player view still reads
    # his PHI window, where no other team's game has closed it.
    assert _team_view_after_missing_both(service) == ["Tyrese Maxey"]
    assert (player["recent"]["games"], player["recent"]["played"]) == (2, 0)


def test_team_view_keeps_a_player_on_the_recent_box_score_whatever_the_catalog_says(tmp_path):
    service = _phi_world(
        tmp_path,
        catalog=Catalog(
            (BONA, "Adem Bona", "NYK"), (MAXEY, "Tyrese Maxey", "PHI"), (GONE, "Gone Gary", "NYK")
        ),
    )

    wider = [p["name"] for p in service.get_team_rotation(team="PHI", last_n=5, season=None)["players"]]

    assert sorted(wider) == ["Adem Bona", "Tyrese Maxey"]
