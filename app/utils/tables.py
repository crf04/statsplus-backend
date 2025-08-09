"""Table name normalization utilities.

Provides a mapping from legacy SQLite table names (with spaces/mixed case)
to Postgres-friendly snake_case names and helpers to normalize names before
issuing SQL queries.
"""

from __future__ import annotations

from typing import Dict


LEGACY_TO_NORMALIZED: Dict[str, str] = {
    # Opponent/traditional
    "General Opponent Stats": "general_opponent_stats",

    # Shooting type tables
    "Catch and Shoot": "catch_and_shoot",
    "Pullups": "pullups",
    "Less Than 10 ft": "less_than_10_ft",
    "Less Than 10 Ft": "less_than_10_ft",

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

