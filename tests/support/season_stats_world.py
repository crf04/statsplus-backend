"""A small stored season-stats world for the season stats route, loader and Postgres tests."""

from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.services.player_season_stats import (
    PlayerSeasonStatsRepository,
    PlayerSeasonStatsService,
    rows_from_frame,
)

FETCHED_AT = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
JOKIC = 203999

#: Jokic's 2023-24 line as NBA.com reports it for ``MeasureType=Base``, ``PerMode=Totals``.
JOKIC_LINE = {
    "PLAYER_ID": JOKIC,
    "PLAYER_NAME": "Nikola Jokić",
    "TEAM_ABBREVIATION": "DEN",
    "AGE": 29.0,
    "GP": 79,
    "W": 55,
    "L": 24,
    "MIN": 2736.5333333333333,
    "FGM": 822,
    "FGA": 1411,
    "FG3M": 83,
    "FG3A": 231,
    "FTM": 358,
    "FTA": 438,
    "OREB": 223,
    "DREB": 753,
    "REB": 976,
    "AST": 708,
    "STL": 108,
    "BLK": 68,
    "TOV": 237,
    "PF": 194,
    "PTS": 2085,
    "PLUS_MINUS": 682,
}

#: A traded reserve with 20 games: 3 points is 0.15 a game, a tie that rounds up to 0.2.
RESERVE_LINE = {
    **JOKIC_LINE,
    "PLAYER_ID": 1700001,
    "PLAYER_NAME": "Reserve Guard",
    "TEAM_ABBREVIATION": "MEM",
    "AGE": 22.0,
    "GP": 20,
    "W": 8,
    "L": 12,
    "MIN": 61.0,
    "FGM": 1,
    "FGA": 3,
    "FG3M": 0,
    "FG3A": 0,
    "FTM": 1,
    "FTA": 2,
    "OREB": 0,
    "DREB": 4,
    "REB": 4,
    "AST": 0,
    "STL": 1,
    "BLK": 0,
    "TOV": 2,
    "PF": 3,
    "PTS": 3,
    "PLUS_MINUS": -1,
}

#: One point of plus-minus over 41 games is -0.02 a game, which must print as 0.0, not -0.0.
EVEN_LINE = {**RESERVE_LINE, "PLAYER_ID": 1700002, "PLAYER_NAME": "Even Steven", "GP": 41}

#: A player who left the league after 2023-24 and was never in the 2024-25 table.
RETIRED_LINE = {**JOKIC_LINE, "PLAYER_ID": 2200, "PLAYER_NAME": "Retired Forward", "TEAM_ABBREVIATION": "LAL"}


#: Jokic's 2023-24 line as NBA.com reports it for ``MeasureType=Advanced``.  The
#: ``E_`` estimated columns carry decoy values: the loader must read the others.
JOKIC_ADVANCED_LINE = {
    "PLAYER_ID": JOKIC,
    "OFF_RATING": 122.4,
    "DEF_RATING": 110.6,
    "NET_RATING": 11.8,
    "TS_PCT": 0.65,
    "EFG_PCT": 0.612,
    "USG_PCT": 0.288,
    "AST_PCT": 0.391,
    "AST_TO": 2.99,
    "AST_RATIO": 27.9,
    "OREB_PCT": 0.086,
    "DREB_PCT": 0.27,
    "REB_PCT": 0.182,
    "TM_TOV_PCT": 9.3,
    "PACE": 98.05,
    "PIE": 0.211,
    "POSS": 5599,
    "E_OFF_RATING": 901.0,
    "E_DEF_RATING": 902.0,
    "E_NET_RATING": 903.0,
    "E_USG_PCT": 0.904,
    "E_TM_TOV_PCT": 905.0,
    "E_PACE": 906.0,
}

#: Jokic's stored advanced object, TM_TOV_PCT scaled from 9.3 to 0.093.
JOKIC_ADVANCED = {
    "off_rating": 122.4,
    "def_rating": 110.6,
    "net_rating": 11.8,
    "ts_pct": 0.65,
    "efg_pct": 0.612,
    "usg_pct": 0.288,
    "ast_pct": 0.391,
    "ast_to": 2.99,
    "ast_ratio": 27.9,
    "oreb_pct": 0.086,
    "dreb_pct": 0.27,
    "reb_pct": 0.182,
    "tov_pct": 0.093,
    "pace": 98.05,
    "pie": 0.211,
    "poss": 5599,
}

#: The reserve's Advanced row has no assist-to-turnover value (NBA sends NaN).
RESERVE_ADVANCED_LINE = {
    **JOKIC_ADVANCED_LINE,
    "PLAYER_ID": 1700001,
    "OFF_RATING": 80.0,
    "TM_TOV_PCT": 12.5,
    "AST_TO": float("nan"),
    "POSS": 40,
}

_ADVANCED_LINES = {JOKIC: JOKIC_ADVANCED_LINE, 1700001: RESERVE_ADVANCED_LINE}


def frame(*lines):
    return pd.DataFrame(list(lines))


def advanced_line_for(line):
    """The Advanced row NBA would report for a Base ``line``."""

    return _ADVANCED_LINES.get(line["PLAYER_ID"], {**JOKIC_ADVANCED_LINE, "PLAYER_ID": line["PLAYER_ID"]})


def rows(season, *lines, advanced=None):
    """Table rows for ``lines``; ``advanced`` overrides the Advanced rows they get."""

    advanced_lines = [advanced_line_for(line) for line in lines] if advanced is None else advanced
    return rows_from_frame(frame(*lines), frame(*advanced_lines), season=season, fetched_at=FETCHED_AT)


class FakeNBA:
    """The adapter's two season fetches, with a failure or a replacement frame per call."""

    def __init__(self, *lines, advanced=None, totals_error=None, advanced_error=None, totals_frame=None):
        self.lines = lines
        self.advanced = advanced
        self.totals_error = totals_error
        self.advanced_error = advanced_error
        self.totals_frame = totals_frame

    def fetch_player_totals_stats(self, *, season):
        if self.totals_error:
            raise self.totals_error
        return self.totals_frame if self.totals_frame is not None else frame(*self.lines)

    def fetch_player_advanced_stats(self, *, season):
        if self.advanced_error:
            raise self.advanced_error
        advanced = (
            [advanced_line_for(line) for line in self.lines] if self.advanced is None else self.advanced
        )
        return frame(*advanced)


def migrated_repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'season_stats.sqlite3'}")
    run_migrations(engine)
    return PlayerSeasonStatsRepository(engine)


def service_for(repository):
    return PlayerSeasonStatsService(repository)
