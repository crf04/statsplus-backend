"""The single Zone Shooting profile transformation.

``DataService._collect_player_zone`` built the legacy ``player_shooting_zones``
table, and the publication reader must render exactly the same row for the same
provider evidence.  Two copies of this arithmetic would drift at the first
correction, so the transform lives here and both callers use it.

The arithmetic is deliberately the historical one, including the parts a fresh
implementation would write differently: ``Sum`` is a sum over every remaining
numeric column rather than over points, ``Backcourt`` contributes to ``Sum``
and to the league ``PTS%`` mean before it is dropped, and ``PTS%+`` divides by
a league mean taken over the full source population.  #267 is a source cutover;
changing these statistics is not in its scope.
"""

from __future__ import annotations

import pandas as pd

from app.domain.player_shot_zone_taxonomy import (
    PLAYER_SHOT_ZONE_PROFILE_IDENTITY,
    PLAYER_SHOT_ZONE_RECONCILED_CATEGORY,
    player_shot_zone_profile_columns,
)


def _flatten_player_zone_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Give the endpoint's grouped columns their historical flat names."""

    if not isinstance(frame.columns, pd.MultiIndex):
        return frame
    flattened = frame.copy()
    flattened.columns = [
        "_".join(filter(None, column)).strip() for column in frame.columns
    ]
    return flattened


def transform_player_zone_profile(frame: pd.DataFrame) -> pd.DataFrame:
    """Render the Zone Shooting profile frame from one wide provider frame.

    ``frame`` may carry the endpoint's grouped columns or their flat names, and
    may carry additional evidence columns.  Only the pinned source columns are
    read, so extra games-played or minutes evidence collected beside the
    profile cannot reach ``Sum`` and silently move every ``PTS%``.
    """

    source = _flatten_player_zone_columns(frame)
    missing = [
        column
        for column in player_shot_zone_profile_columns()
        if column not in source.columns
    ]
    if missing:
        raise KeyError(f"player zone profile source columns missing: {missing}")
    player_zones = source.loc[:, list(player_shot_zone_profile_columns())].copy()

    # The historical column selector.  ``Backcourt`` is excluded here, so it
    # never gains a PTS/PTS% column, but its FGM/FGA/FG_PCT stay in the frame
    # and therefore in ``Sum`` and in the league mean below.
    scoring_columns = [
        column
        for column in player_zones.columns
        if ("FGM" in column or "_NAME" in column)
        and PLAYER_SHOT_ZONE_RECONCILED_CATEGORY not in column
    ]
    identity = list(PLAYER_SHOT_ZONE_PROFILE_IDENTITY)

    for column in scoring_columns:
        if "NAME" not in column:
            category = column.split("_")[0]
            player_zones[f"{category}_PTS"] = (
                player_zones[column] * 2
                if "3" not in column
                else player_zones[column] * 3
            )

    player_zones["Sum"] = player_zones.drop(identity, axis=1).sum(axis=1)

    for column in scoring_columns:
        if "NAME" not in column:
            category = column.split("_")[0]
            player_zones[f"{category}_PTS%"] = (
                player_zones[f"{category}_PTS"] / player_zones["Sum"] * 100
            )

    means = player_zones.drop(identity, axis=1).mean()
    for column in [column for column in player_zones.columns if "PTS%" in column]:
        player_zones[f"{column}+"] = (
            player_zones[column] / means[column] if means[column] != 0 else 0
        )

    player_zones.drop(
        [
            column
            for column in player_zones.columns
            if PLAYER_SHOT_ZONE_RECONCILED_CATEGORY in column
        ],
        axis=1,
        inplace=True,
    )
    player_zones.drop(
        [column for column in identity if column != "PLAYER_NAME"] + ["Sum"],
        axis=1,
        inplace=True,
    )
    player_zones.fillna(0, inplace=True)
    return player_zones


__all__ = ["transform_player_zone_profile"]
