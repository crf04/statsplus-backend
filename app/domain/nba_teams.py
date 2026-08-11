"""Canonical NBA tricodes and reviewed provider dialects."""

from __future__ import annotations


NBA_TEAM_TRICODES = frozenset(
    {
        "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET",
        "GSW", "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN",
        "NOP", "NYK", "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS",
        "TOR", "UTA", "WAS",
    }
)

NBA_TEAM_ABBREVIATION_DIALECTS = {
    "GS": "GSW",
    "NO": "NOP",
    "NY": "NYK",
    "PHO": "PHX",
    "SA": "SAS",
}


def canonical_nba_team_abbreviation(value: object) -> str:
    """Return one reviewed provider tricode in the NBA catalog dialect."""

    normalized = str(value or "").strip().upper()
    return NBA_TEAM_ABBREVIATION_DIALECTS.get(normalized, normalized)


__all__ = [
    "NBA_TEAM_ABBREVIATION_DIALECTS",
    "NBA_TEAM_TRICODES",
    "canonical_nba_team_abbreviation",
]
