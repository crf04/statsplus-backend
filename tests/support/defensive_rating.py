"""One season of NBA.com team ``DEF_RATING`` values (crf04/statsplus#447).

Thirty distinct ratings, deliberately not in team-id order, so a ranking that
followed identity or input order would be visibly wrong.  Sorted, the ten
highest are UTA 120.4 down to PHI 115.7, the eleventh is ATL 115.3, OKC 106.9
is the lowest, and LAL 114.8 is the eighteenth lowest.  They sum to 3412.6.
"""

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from tests.support.publication_stubs import league, read

DEF_RATINGS = {
    "ATL": 115.3,
    "BOS": 109.1,
    "CLE": 108.4,
    "NOP": 117.9,
    "CHI": 116.2,
    "DAL": 114.0,
    "DEN": 113.6,
    "GSW": 111.2,
    "HOU": 108.9,
    "LAC": 112.5,
    "LAL": 114.8,
    "MIA": 112.1,
    "MIL": 113.1,
    "MIN": 110.0,
    "BKN": 116.8,
    "NYK": 111.7,
    "ORL": 107.6,
    "IND": 116.5,
    "PHI": 115.7,
    "PHX": 114.4,
    "POR": 117.2,
    "SAC": 118.6,
    "SAS": 113.9,
    "OKC": 106.9,
    "TOR": 115.0,
    "UTA": 120.4,
    "MEM": 112.9,
    "WAS": 119.8,
    "DET": 110.6,
    "CHA": 117.5,
}
TEN_HIGHEST = ["UTA", "WAS", "SAC", "NOP", "CHA", "POR", "BKN", "IND", "CHI", "PHI"]
STREAM = "defensive_rating_season"


def advanced_rows(ratings=None, *, games=1):
    """``LeagueDashTeamStats`` Advanced rows, as NBA.com returns them."""

    ratings = DEF_RATINGS if ratings is None else ratings
    return [
        {
            "TEAM_ID": team_id,
            "TEAM_NAME": tricode,
            "GP": games,
            "W": 0,
            "L": games,
            "MIN": 48.0,
            "OFF_RATING": 110.0,
            "DEF_RATING": ratings[tricode],
            "NET_RATING": 110.0 - ratings[tricode],
            "PACE": 99.0,
            "POSS": 99,
        }
        for team_id, tricode in sorted(NBA_TEAM_ID_TO_TRICODE.items())
        if tricode in ratings
    ]


def defensive_rating_read(ratings=None):
    """A decoded ``defensive_rating_season`` read for the stub reader."""

    ratings = DEF_RATINGS if ratings is None else ratings
    return read(STREAM, league(lambda tricode: {"DEF_RATING": ratings[tricode]}))
