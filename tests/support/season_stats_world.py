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


def frame(*lines):
    return pd.DataFrame(list(lines))


def rows(season, *lines):
    return rows_from_frame(frame(*lines), season=season, fetched_at=FETCHED_AT)


def migrated_repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'season_stats.sqlite3'}")
    run_migrations(engine)
    return PlayerSeasonStatsRepository(engine)


def service_for(repository):
    return PlayerSeasonStatsService(repository)
