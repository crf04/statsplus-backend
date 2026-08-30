"""Shared play-types matchup arithmetic.

Matchup Scores and the Log Workspace rating both cross a player's observed
Synergy play-type Diet with an opponent's allowed rate, so the definition lives
here rather than inside whichever service happened to need it first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from math import isfinite

from app.domain.team_matchup_taxonomy import PLAY_TYPES


_PLAY_TYPE_SHARE_BOUNDS = (0.0, 1.005)


def complete_play_type_shares(
    facts: Iterable[tuple[str, float]],
) -> dict[str, float] | None:
    """Return one valid observed Synergy Diet partition, else ``None``.

    Synergy omits unobserved slices, so a partial partition is valid. Duplicate
    or unknown slices and a share total outside the provider's accepted rounded
    range are invalid evidence for both Matchup Scores and the Log Workspace.
    """

    shares: dict[str, float] = {}
    for slice_key, share in facts:
        if (
            slice_key in shares
            or slice_key not in PLAY_TYPES
            or isinstance(share, bool)
            or not isinstance(share, (int, float))
            or not isfinite(share)
            or not 0 <= share <= 1
        ):
            return None
        shares[slice_key] = share
    if not shares:
        return None
    lower, upper = _PLAY_TYPE_SHARE_BOUNDS
    share_sum = sum(shares.values())
    if not lower - 1e-12 <= share_sum <= upper + 1e-12:
        return None
    return shares


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


__all__ = ["complete_play_type_shares", "play_type_matchup"]
