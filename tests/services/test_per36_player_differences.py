"""Unit coverage for the per-player Season per-36 parity classification."""

from app.services.ledger_derivations import PlayerPer36Fact
from app.services.ledger_parity import PER36_RAW_FIELDS
from app.services.matchup_parity_operation import (
    PER36_MINUTES_TOLERANCE_PER_GAME,
    per36_player_differences,
)


def _expected(*, minutes=1988.9833333333333, game_count=70, teams=(1610612747,)):
    rates = {f"{field}_per36": 1.0 for field in PER36_RAW_FIELDS}
    return PlayerPer36Fact(
        season="2025-26", player_id=2544, minutes=minutes, game_count=game_count,
        team_ids_at_game=tuple(teams), **rates,
    )


def _raw():
    return {field: 10 for field in PER36_RAW_FIELDS}


def _actual(**overrides):
    row = {
        "player_id": 2544, "minutes": 1988.9833333333333, "game_count": 70,
        "team_ids_at_game": [1610612747], **_raw(),
    }
    row.update(overrides)
    return row


def _classes(differences):
    return sorted((d.field, d.classification) for d in differences)


def test_exact_match_has_no_differences():
    assert per36_player_differences(2544, expected=_expected(), expected_raw=_raw(), actual=_actual()) == []


def test_provider_minute_rounding_within_per_game_bound_is_not_a_difference():
    # 70 games of two-decimal MM:SS rounding drift (observed: 1988.983 vs 1989.055).
    actual = _actual(minutes=1989.055)
    assert per36_player_differences(2544, expected=_expected(), expected_raw=_raw(), actual=actual) == []


def test_minutes_drift_at_exact_bound_is_not_a_difference():
    bound = PER36_MINUTES_TOLERANCE_PER_GAME * 70
    for sign in (1, -1):
        actual = _actual(minutes=1988.9833333333333 + sign * bound)
        assert per36_player_differences(
            2544, expected=_expected(), expected_raw=_raw(), actual=actual,
        ) == []


def test_minutes_drift_just_above_bound_stays_hard():
    bound = PER36_MINUTES_TOLERANCE_PER_GAME * 70
    for sign in (1, -1):
        actual = _actual(minutes=1988.9833333333333 + sign * (bound + 1e-6))
        assert _classes(per36_player_differences(
            2544, expected=_expected(), expected_raw=_raw(), actual=actual,
        )) == [("minutes", "minutes_difference")]


def test_minutes_drift_above_bound_stays_hard():
    bound = PER36_MINUTES_TOLERANCE_PER_GAME * 70
    actual = _actual(minutes=1988.9833333333333 + bound + 0.5)
    assert _classes(per36_player_differences(
        2544, expected=_expected(), expected_raw=_raw(), actual=actual,
    )) == [("minutes", "minutes_difference")]


def test_roster_team_identity_is_not_compared():
    # Provider TEAM_ID is the roster team at capture time, not participation.
    actual = _actual(team_ids_at_game=[1610612764])
    assert per36_player_differences(
        2544, expected=_expected(teams=(1610612742,)), expected_raw=_raw(), actual=actual,
    ) == []


def test_zero_minute_appearance_counted_by_provider_is_not_a_difference():
    actual = _actual(game_count=71)
    assert per36_player_differences(2544, expected=_expected(), expected_raw=_raw(), actual=actual) == []


def test_provider_fewer_games_than_ledger_is_hard():
    actual = _actual(game_count=69)
    assert _classes(per36_player_differences(
        2544, expected=_expected(), expected_raw=_raw(), actual=actual,
    )) == [("game_count", "game_count_difference")]


def test_raw_count_difference_stays_hard():
    actual = _actual(personal_fouls=11)
    assert _classes(per36_player_differences(
        2544, expected=_expected(), expected_raw=_raw(), actual=actual,
    )) == [("personal_fouls", "raw_count_difference")]
