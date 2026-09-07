"""Offline evidence for conservative cross-team Synergy aggregation."""

import json
from pathlib import Path

import pytest

from app.domain.player_synergy import aggregate_player_synergy


def record(team=1, category="Isolation", poss=20, share=0.2, **changes):
    return dict(
        player_id=7,
        team_id=team,
        category=category,
        possessions=poss,
        possession_share=share,
        games_played=10,
        **changes,
    )


def test_single_team_preserves_sparse_raw_share_without_reconstruction():
    rows, withheld = aggregate_player_synergy([record(poss=37, share=0.123)])
    assert withheld == {}
    assert rows == [
        dict(
            player_id=7,
            slice_key="Isolation",
            share=0.123,
            volume=37,
            games_played=10,
            volume_unit="possessions",
            provider="nba_synergy",
        )
    ]


def test_trade_uses_one_proven_denominator_and_keeps_missing_categories_sparse():
    rows, withheld = aggregate_player_synergy(
        [
            record(),
            record(category="Cut", poss=30, share=0.3),
            record(team=2, poss=40, share=0.4),
        ]
    )
    assert withheld == {}
    assert {r["slice_key"]: (r["share"], r["volume"]) for r in rows} == {
        "Isolation": (0.3, 60),
        "Cut": (0.15, 30),
    }
    assert {r["games_played"] for r in rows} == {20}


@pytest.mark.parametrize(
    "bad_stint",
    [
        [record(team=2, poss=1, share=0.001)],  # many integer denominators
        [record(team=2, poss=1, share=0.3)],  # no integer denominator
        [record(team=2, poss=0, share=0.1)],
        [
            record(team=2, poss=90, share=0.9),
            record(team=2, category="Cut", poss=20, share=0.2),
        ],
        [dict(record(team=2), games_played=3), record(team=2, category="Cut")],
    ],
)
def test_inconsistent_or_unproven_stint_withholds_entire_player(bad_stint):
    rows, withheld = aggregate_player_synergy([record(), *bad_stint])
    assert rows == []
    assert set(withheld) == {7}


@pytest.mark.parametrize(
    "changes",
    [
        {"player_id": True},
        {"team_id": 0},
        {"games_played": -1},
        {"possessions": 1.5},
        {"possessions": "20"},
        {"possessions": float("inf")},
        {"possession_share": float("nan")},
        {"possession_share": True},
        {"possession_share": 1.1},
        {"category": "unknown"},
        {"category": []},
    ],
)
def test_malformed_records_fail_whole_source(changes):
    with pytest.raises(ValueError):
        aggregate_player_synergy([record(), dict(record(team=2), **changes)])


def test_duplicates_and_missing_fields_fail_source():
    with pytest.raises(ValueError):
        aggregate_player_synergy([record(), record()])
    with pytest.raises(ValueError):
        aggregate_player_synergy([{"player_id": 7}])


@pytest.mark.parametrize("endpoint", [0.062, 0.063])
def test_rounding_endpoints_are_inclusive(endpoint):
    # 1 / 16 = .0625 is at either inclusive endpoint; both identify 16 uniquely.
    rows, withheld = aggregate_player_synergy(
        [
            record(poss=1, share=endpoint),
            record(team=2, poss=1, share=endpoint),
        ]
    )
    assert withheld == {}
    assert rows[0]["share"] == 0.0625


def test_observed_possessions_cannot_exceed_inferred_team_total():
    rows, withheld = aggregate_player_synergy(
        [
            record(),
            record(team=2, poss=1, share=0.005),
            record(team=2, category="Cut", poss=200, share=1),
        ]
    )
    assert rows == []
    assert set(withheld) == {7}


def test_real_eleven_invalid_partitions_recover_nine_and_withhold_two():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/player_synergy_affected.json").read_text()
    )
    rows, withheld = aggregate_player_synergy(fixture["records"])
    assert set(withheld) == {203468, 1631172}  # McCollum and Dieng remain ambiguous.
    assert len({row["player_id"] for row in rows}) == 9
    for player_id in {row["player_id"] for row in rows}:
        player = [row for row in rows if row["player_id"] == player_id]
        observed = [r for r in fixture["records"] if r["player_id"] == player_id]
        assert sum(row["share"] for row in player) <= 1
        assert sum(row["volume"] for row in player) == sum(
            r["possessions"] for r in observed
        )
        assert len({row["games_played"] for row in player}) == 1
        denominator = fixture["expected_player_denominators"][str(player_id)]
        assert all(row["share"] == row["volume"] / denominator for row in player)


def test_zero_games_played_is_malformed_even_for_single_team():
    with pytest.raises(ValueError, match="games_played"):
        aggregate_player_synergy([dict(record(), games_played=0)])


def test_zero_possessions_cannot_support_positive_rounded_share():
    rows, withheld = aggregate_player_synergy(
        [
            record(),
            record(category="Cut", poss=0, share=0.001),
        ]
    )
    assert rows == []
    assert set(withheld) == {7}


def test_positive_possessions_can_round_to_zero_share_for_single_team():
    rows, withheld = aggregate_player_synergy([record(poss=1, share=0)])
    assert withheld == {}
    assert rows[0]["volume"] == 1
    assert rows[0]["share"] == 0
