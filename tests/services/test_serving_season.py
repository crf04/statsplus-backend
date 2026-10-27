"""The Readiness Rule table and the trailing list (crf04/statsplus#304)."""

from __future__ import annotations

from types import SimpleNamespace

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.services.serving_season import (
    READINESS_RULES,
    merge_trailing,
    previous_season,
    readiness_rule,
    serves_prior_season,
    trailing,
)

TEAMS = sorted(NBA_TEAM_ID_TO_TRICODE)
EVERY_TEAM_PLAYS_10 = "every team has played 10 games"
EVERY_TEAM_PLAYS_ONE = "every team has played a game"


def _games(per_team):
    return {team_id: [f"{team_id}-{index}" for index in range(count)] for team_id, count in per_team.items()}


def test_the_readiness_rules_name_each_familys_streams_and_threshold():
    assert {
        rule.family: (sorted(rule.streams), rule.min_team_games, rule.wording) for rule in READINESS_RULES
    } == {
        "game_logs": (["player_game_logs", "player_per36"], None, None),
        "player_diets": (
            ["exact_shot_zones", "grouped_shot_types", "player_assist_locations", "player_rebound_types"],
            10,
            EVERY_TEAM_PLAYS_10,
        ),
        "team_defense": (
            [
                "assist_locations_season",
                "exact_shot_zones_opponent_season",
                "grouped_shot_types_opponent_season",
                "rebound_types_season",
                "synergy_play_types_opponent_season",
                "traditional_opponent_season",
            ],
            1,
            EVERY_TEAM_PLAYS_ONE,
        ),
        "synergy": (
            ["synergy_play_types"],
            10,
            "NBA has published the season and every team has played 10 games",
        ),
        "last_15": (
            [
                "assist_locations_l15",
                "exact_shot_zones_opponent_l15",
                "grouped_shot_types_opponent_l15",
                "rebound_types_l15",
                "traditional_opponent_l15",
            ],
            None,
            "its first window of the season is composed",
        ),
    }


def test_player_diets_and_synergy_hold_once_all_thirty_teams_have_played_ten_games():
    for stream in ("grouped_shot_types", "synergy_play_types"):
        rule = readiness_rule(stream)

        assert rule.holds(_games(dict.fromkeys(TEAMS, 10))) is True
        assert rule.holds(_games({**dict.fromkeys(TEAMS, 12), TEAMS[7]: 9})) is False
        assert rule.holds(_games(dict.fromkeys(TEAMS[1:], 10))) is False
        assert rule.holds({}) is False
    # The cutover family and a stream in no family never wait.
    assert readiness_rule("player_game_logs").holds({}) is True
    assert readiness_rule("event_catalog") is None


def test_team_defense_holds_once_all_thirty_teams_have_played_a_game():
    for stream in (
        "traditional_opponent_season",
        "grouped_shot_types_opponent_season",
        "exact_shot_zones_opponent_season",
        "synergy_play_types_opponent_season",
        "assist_locations_season",
        "rebound_types_season",
    ):
        rule = readiness_rule(stream)

        assert rule.holds(_games(dict.fromkeys(TEAMS, 1))) is True
        assert rule.holds(_games({**dict.fromkeys(TEAMS, 1), TEAMS[7]: 0})) is False
        assert rule.holds(_games(dict.fromkeys(TEAMS[1:], 3))) is False
        assert rule.holds({}) is False


def test_only_a_trailing_family_serves_the_season_just_before():
    assert previous_season("2026-27") == "2025-26"
    assert previous_season("2000-01") == "1999-00"
    assert [
        serves_prior_season(stream, publication_season, "2026-27")
        for stream, publication_season in (
            ("grouped_shot_types", "2025-26"),
            ("synergy_play_types_opponent_season", "2025-26"),
            ("traditional_opponent_season", "2024-25"),
            ("traditional_opponent_l15", "2025-26"),
            ("player_game_logs", "2025-26"),
        )
    ] == [True, True, False, False, False]


def _read(stream_key, status, season="2025-26"):
    return SimpleNamespace(stream_key=stream_key, status=status, season=season)


def test_trailing_lists_each_family_with_a_prior_season_read_once_in_table_order():
    reads = [
        _read("synergy_play_types", "prior_season"),
        _read("traditional_opponent_season", "prior_season"),
        _read("assist_locations_season", "prior_season"),
        _read("exact_shot_zones", "active", "2026-27"),
        _read("traditional_opponent_l15", "missing"),
    ]

    assert trailing(reads) == [
        {"family": "team_defense", "serving_season": "2025-26", "rule": EVERY_TEAM_PLAYS_ONE},
        {
            "family": "synergy",
            "serving_season": "2025-26",
            "rule": "NBA has published the season and every team has played 10 games",
        },
    ]
    assert trailing([_read("exact_shot_zones", "active", "2026-27")]) == []


def test_play_type_defense_trails_as_team_defense_and_player_synergy_as_synergy():
    assert trailing([_read("synergy_play_types_opponent_season", "prior_season")]) == [
        {"family": "team_defense", "serving_season": "2025-26", "rule": EVERY_TEAM_PLAYS_ONE},
    ]
    assert trailing([_read("synergy_play_types", "prior_season")]) == [
        {
            "family": "synergy",
            "serving_season": "2025-26",
            "rule": "NBA has published the season and every team has played 10 games",
        },
    ]


def test_merged_trailing_lists_keep_one_entry_per_family_in_table_order():
    synergy = {"family": "synergy", "serving_season": "2025-26", "rule": "x"}
    diets = {"family": "player_diets", "serving_season": "2025-26", "rule": "y"}

    assert merge_trailing([[synergy], [diets, synergy], []]) == [diets, synergy]
