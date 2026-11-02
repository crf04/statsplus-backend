"""Player Position: NBA.com's listed position mapped to Guard, Wing or Big.

A Position Qualifier (crf04/statsplus#351, ADR 0005) names these groups, so the
mapping is one fixed rule shared by every writer of the athlete catalog.  The
catalog stores the group, not the listed code: the code is only an input here.
A short hand-kept list of players whose listed code misplaces them overrides
the mapping (crf04/statsplus#403).
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

#: ``{NBA player id: group}`` for players NBA.com's listed code misplaces,
#: chosen by hand from their play (crf04/statsplus#403).
POSITION_OVERRIDES: Final[Mapping[int, str]] = {
    203944: "Wing",  # Julius Randle
    1626171: "Big",  # Bobby Portis Jr.
    1626172: "Big",  # Kevon Looney
    1628374: "Wing",  # Lauri Markkanen
    1628963: "Big",  # Marvin Bagley III
    1629029: "Guard",  # Luka Dončić
    1629614: "Guard",  # Andrew Nembhard
    1629660: "Guard",  # Ty Jerome
    1629731: "Wing",  # Dean Wade
    1629750: "Wing",  # Javonte Green
    1630173: "Big",  # Precious Achiuwa
    1630182: "Wing",  # Josh Green
    1630194: "Big",  # Paul Reed
    1630249: "Wing",  # Vít Krejčí
    1630541: "Wing",  # Moses Moody
    1630543: "Big",  # Isaiah Jackson
    1631119: "Big",  # Jaylin Williams
    1631124: "Wing",  # Julian Strawther
    1631128: "Wing",  # Christian Braun
    1631212: "Wing",  # Peyton Watson
    1631217: "Big",  # Moussa Diabaté
    1631218: "Big",  # Trayce Jackson-Davis
    1631243: "Big",  # Mouhamed Gueye
    1631321: "Wing",  # Sidy Cissoko
    1641724: "Wing",  # Jett Howard
    1641730: "Wing",  # Noah Clowney
    1641731: "Wing",  # Bilal Coulibaly
    1641737: "Big",  # Adem Bona
    1641767: "Wing",  # Ben Sheppard
    1641774: "Big",  # Tristan Vukčević
    1641775: "Wing",  # Jordan Walsh
    1642266: "Wing",  # Ja'Kobe Walter
    1642277: "Wing",  # Johnny Furphy
    1642345: "Big",  # Oso Ighodaro
    1642864: "Wing",  # Hugo González
    1642882: "Big",  # Julian Reese
    1642907: "Wing",  # Cedric Coward
    1642920: "Wing",  # Kobe Sanders
}


def position_group(player_id: int, listed_position: Any) -> str | None:
    """Return one player's group: an override, else the listed position's, else ``None``."""

    if player_id in POSITION_OVERRIDES:
        return POSITION_OVERRIDES[player_id]
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
    "POSITION_OVERRIDES",
    "listed_positions_by_player",
    "position_group",
    "with_listed_positions",
]
