"""Table name normalization utilities.

Provides a mapping from legacy SQLite table names (with spaces/mixed case)
to Postgres-friendly snake_case names and helpers to normalize names before
issuing SQL queries.
"""

from __future__ import annotations

from typing import Dict


LEGACY_TO_NORMALIZED: Dict[str, str] = {
    # The opponent and shooting-type display names are gone with the tables
    # they normalized to, which #199 dropped.  No caller passed them here.

    # PBP totals
    "pbp_Opponent_stats": "pbp_opponent_stats",
    "pbp_Player_stats": "pbp_player_stats",

    # Mixed-case legacy names
    "Player_Information": "player_information",
    "Player_Per36_Stats": "player_per36_stats",
    "Player_Team_Table": "player_team_table",
    "Team_Info": "team_info",
}


def normalize_table_name(table_name: str) -> str:
    """Return a Postgres-friendly table name for the given identifier.

    If the name is present in the legacy mapping, return the normalized name;
    otherwise return the original name unchanged.
    """

    return LEGACY_TO_NORMALIZED.get(table_name, table_name)

