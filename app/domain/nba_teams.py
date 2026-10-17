"""Canonical NBA tricodes and reviewed provider dialects."""

from __future__ import annotations

NBA_TEAM_TRICODES = frozenset(
    {
        "ATL",
        "BOS",
        "BKN",
        "CHA",
        "CHI",
        "CLE",
        "DAL",
        "DEN",
        "DET",
        "GSW",
        "HOU",
        "IND",
        "LAC",
        "LAL",
        "MEM",
        "MIA",
        "MIL",
        "MIN",
        "NOP",
        "NYK",
        "OKC",
        "ORL",
        "PHI",
        "PHX",
        "POR",
        "SAC",
        "SAS",
        "TOR",
        "UTA",
        "WAS",
    }
)
NBA_TEAM_ID_TO_TRICODE = dict(
    zip(
        range(1610612737, 1610612767),
        (
            "ATL",
            "BOS",
            "CLE",
            "NOP",
            "CHI",
            "DAL",
            "DEN",
            "GSW",
            "HOU",
            "LAC",
            "LAL",
            "MIA",
            "MIL",
            "MIN",
            "BKN",
            "NYK",
            "ORL",
            "IND",
            "PHI",
            "PHX",
            "POR",
            "SAC",
            "SAS",
            "OKC",
            "TOR",
            "UTA",
            "MEM",
            "WAS",
            "DET",
            "CHA",
        ),
        strict=False,
    )
)

#: The same catalog read the other way, for a surface that stores a tricode
#: and has to query evidence keyed by team id.
NBA_TEAM_TRICODE_TO_ID = {tricode: team_id for team_id, tricode in NBA_TEAM_ID_TO_TRICODE.items()}

#: Full team names as a provider that publishes only the name states them
#: (Novig), mapped exactly to the catalog tricode.  A name outside this closed
#: table has no tricode; nothing here is fuzzy or inferred.  "LA Clippers" is
#: the league's own spelling of "Los Angeles Clippers".
NBA_TEAM_FULL_NAME_TO_TRICODE = {
    "Atlanta Hawks": "ATL",
    "Boston Celtics": "BOS",
    "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA",
    "Chicago Bulls": "CHI",
    "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL",
    "Denver Nuggets": "DEN",
    "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW",
    "Houston Rockets": "HOU",
    "Indiana Pacers": "IND",
    "Los Angeles Clippers": "LAC",
    "LA Clippers": "LAC",
    "Los Angeles Lakers": "LAL",
    "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA",
    "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN",
    "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK",
    "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL",
    "Philadelphia 76ers": "PHI",
    "Phoenix Suns": "PHX",
    "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC",
    "San Antonio Spurs": "SAS",
    "Toronto Raptors": "TOR",
    "Utah Jazz": "UTA",
    "Washington Wizards": "WAS",
}

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
    "NBA_TEAM_FULL_NAME_TO_TRICODE",
    "NBA_TEAM_ID_TO_TRICODE",
    "NBA_TEAM_TRICODES",
    "NBA_TEAM_TRICODE_TO_ID",
    "canonical_nba_team_abbreviation",
]
