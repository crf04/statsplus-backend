"""Shared play-types matchup arithmetic."""

from __future__ import annotations

import pytest

from app.domain.play_type_matchup import play_type_matchup


@pytest.mark.parametrize(
    ("shares", "opponent", "league", "expected"),
    [
        pytest.param(
            {"Isolation": 0.5},
            {"Isolation": 12.0},
            {"Isolation": 10.0},
            0.5 * 1.2 - 0.5,
            id="one_slice_scales_its_share_by_the_matchup_difference",
        ),
        pytest.param(
            {"Isolation": 0.5, "Spotup": 0.25},
            {"Isolation": 12.0, "Spotup": 8.0},
            {"Isolation": 10.0, "Spotup": 10.0},
            0.5 * 1.2 + 0.25 * 0.8 - 0.75,
            id="slices_add_without_share_normalization",
        ),
        pytest.param(
            {"Isolation": 0.5, "Spotup": 0.0},
            {"Isolation": 12.0},
            {"Isolation": 10.0},
            0.5 * 1.2 - 0.5,
            id="a_zero_share_slice_needs_no_opponent_evidence",
        ),
        pytest.param(
            {"Isolation": 0.5, "Spotup": 0.25},
            {"Isolation": 12.0, "Spotup": 0.0},
            {"Isolation": 10.0, "Spotup": 0.0},
            0.5 * 1.2 - 0.5,
            id="an_exact_zero_over_zero_slice_is_a_structural_zero",
        ),
        pytest.param(
            {"Isolation": 0.5},
            {},
            {"Isolation": 10.0},
            None,
            id="a_missing_opponent_slice_fails_closed",
        ),
        pytest.param(
            {"Isolation": 0.5},
            {"Isolation": 12.0},
            {},
            None,
            id="a_missing_league_slice_fails_closed",
        ),
        pytest.param(
            {"Isolation": 0.5},
            {"Isolation": 12.0},
            {"Isolation": 0.0},
            None,
            id="opponent_evidence_without_a_league_denominator_fails_closed",
        ),
        pytest.param(
            {"Isolation": 0.5},
            {"Isolation": 12.0},
            {"Isolation": -1.0},
            None,
            id="a_negative_league_denominator_fails_closed",
        ),
        pytest.param(
            {},
            {},
            {},
            None,
            id="no_observed_slices_is_absent_not_neutral",
        ),
        pytest.param(
            {"Isolation": 0.0},
            {"Isolation": 12.0},
            {"Isolation": 10.0},
            None,
            id="only_nonpositive_shares_is_absent_not_neutral",
        ),
        pytest.param(
            {"Isolation": 0.5},
            {"Isolation": 0.0},
            {"Isolation": 10.0},
            0.5 * 0.0 - 0.5,
            id="an_opponent_that_allows_nothing_scores_the_full_negative_share",
        ),
    ],
)
def test_play_type_matchup_cases(shares, opponent, league, expected):
    result = play_type_matchup(shares, opponent, league)

    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)
