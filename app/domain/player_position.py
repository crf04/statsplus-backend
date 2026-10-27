"""Player Position: NBA.com's listed position mapped to Guard, Wing or Big.

A Position Qualifier (crf04/statsplus#351, ADR 0005) names these groups, so the
mapping is one fixed rule shared by every writer of the athlete catalog.  The
catalog stores the group, not the listed code: the code is only an input here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

import pandas as pd

#: The groups in their display order; a title and a stored set follow it.
POSITION_GROUPS: Final[tuple[str, ...]] = ("Guard", "Wing", "Big")

_LISTED_POSITION_GROUPS: Final[Mapping[str, str]] = {
    "G": "Guard",
    "G-F": "Wing",
    "F-G": "Wing",
    "F": "Wing",
    "F-C": "Big",
    "C-F": "Big",
    "C": "Big",
}


def position_group(listed_position: Any) -> str | None:
    """Return the group for one listed position, or ``None`` for anything else."""

    if not isinstance(listed_position, str):
        return None
    return _LISTED_POSITION_GROUPS.get(listed_position.strip().upper())


def listed_positions_by_player(team_rosters: Iterable[pd.DataFrame]) -> dict[int, str]:
    """Join ``CommonTeamRoster`` frames into ``{player id: listed position}``.

    A player listed by two teams in one season (a trade) keeps the first
    non-empty listing in the order the frames are given, so the join is
    deterministic.  Rows without a usable id or position are skipped.
    """

    listed: dict[int, str] = {}
    for frame in team_rosters:
        if frame is None or "PLAYER_ID" not in frame.columns or "POSITION" not in frame.columns:
            continue
        for player_id, position in zip(frame["PLAYER_ID"], frame["POSITION"], strict=False):
            if pd.isna(player_id) or not isinstance(position, str) or not position.strip():
                continue
            listed.setdefault(int(player_id), position.strip())
    return listed


def with_listed_positions(roster: pd.DataFrame, listed: Mapping[int, str]) -> pd.DataFrame:
    """Return ``roster`` with a raw ``POSITION`` column joined on player id."""

    id_column = next((name for name in ("PERSON_ID", "PLAYER_ID") if name in roster.columns), None)
    if id_column is None:
        return roster
    joined = roster.copy()
    joined["POSITION"] = [
        None if pd.isna(player_id) else listed.get(int(player_id)) for player_id in joined[id_column]
    ]
    return joined


__all__ = [
    "POSITION_GROUPS",
    "listed_positions_by_player",
    "position_group",
    "with_listed_positions",
]
