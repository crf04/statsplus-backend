"""Shared play-types matchup arithmetic.

Matchup Scores and the Log Workspace rating both cross a player's observed
Synergy play-type Diet with an opponent's allowed rate, so the definition lives
here rather than inside whichever service happened to need it first.
"""

from __future__ import annotations

from collections.abc import Mapping


def play_type_matchup(
    shares: Mapping[str, float],
    opponent_allowed_per_48: Mapping[str, float],
    league_allowed_per_48: Mapping[str, float],
) -> float | None:
    """Return ``Σ share × opponent/league − Σ share`` over the observed slices.

    Each raw observed share is applied to its slice's fractional matchup
    difference, so the unobserved residual of a rounded partition keeps a
    neutral baseline instead of being normalized away. A slice whose opponent
    and league values are both exactly zero is a structural zero and
    contributes nothing; anything the caller cannot evidence -- a missing slice,
    or nonzero opponent evidence against a non-positive league denominator --
    fails closed as ``None`` rather than scoring on a fabricated denominator.
    """
    total = 0.0
    weight_total = 0.0
    for slice_key, share in shares.items():
        if share <= 0:
            continue
        league_value = league_allowed_per_48.get(slice_key)
        opponent_value = opponent_allowed_per_48.get(slice_key)
        if league_value is None or opponent_value is None:
            return None
        if league_value == 0 and opponent_value == 0:
            continue
        if league_value <= 0:
            return None
        total += share * (opponent_value / league_value)
        weight_total += share
    if weight_total == 0:
        return None
    return total - weight_total


__all__ = ["play_type_matchup"]
