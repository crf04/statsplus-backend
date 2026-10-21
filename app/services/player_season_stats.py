"""Season player stats: the loader's replace and the ``GET /api/players/season_stats`` read.

Implements crf04/statsplus-backend#509 for crf04/statsplus#277.  The table
holds NBA ``LeagueDashPlayerStats`` Base totals and Advanced values, one row
per player per season and season type.  The read derives per-game values
(totals / GP rounded half-up to one decimal) and shooting percentages (made /
attempted rounded half-up to three decimals, ``null`` when the player
attempted none) from the totals, and serves the Advanced values as stored.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from app.errors import SeasonNotLoadedError
from app.models.player_season_stats import REGULAR_SEASON, PlayerSeasonStats

_TABLE = PlayerSeasonStats.__table__

#: The stat keys of ``totals`` and ``per_game``, in response order.
STAT_KEYS = (
    "min",
    "pts",
    "reb",
    "oreb",
    "dreb",
    "ast",
    "stl",
    "blk",
    "tov",
    "pf",
    "fgm",
    "fga",
    "fg3m",
    "fg3a",
    "ftm",
    "fta",
    "plus_minus",
)

#: Stored advanced column -> NBA ``MeasureType=Advanced`` key, in response order.
#: Every ``*_pct`` and ``pie`` is a 0-1 fraction; NBA reports only ``TM_TOV_PCT``
#: as a percent, which is why it is the one key in ``_PERCENT_SCALED``.
ADVANCED_KEYS = {
    "off_rating": "OFF_RATING",
    "def_rating": "DEF_RATING",
    "net_rating": "NET_RATING",
    "ts_pct": "TS_PCT",
    "efg_pct": "EFG_PCT",
    "usg_pct": "USG_PCT",
    "ast_pct": "AST_PCT",
    "ast_to": "AST_TO",
    "ast_ratio": "AST_RATIO",
    "oreb_pct": "OREB_PCT",
    "dreb_pct": "DREB_PCT",
    "reb_pct": "REB_PCT",
    "tov_pct": "TM_TOV_PCT",
    "pace": "PACE",
    "pie": "PIE",
    "poss": "POSS",
}
_PERCENT_SCALED = frozenset({"tov_pct"})
REQUIRED_ADVANCED_COLUMNS = ("PLAYER_ID", *ADVANCED_KEYS.values())

_IDENTITY_COLUMNS = ("PLAYER_ID", "PLAYER_NAME", "TEAM_ABBREVIATION", "AGE", "GP", "W", "L")
REQUIRED_COLUMNS = (*_IDENTITY_COLUMNS, *(key.upper() for key in STAT_KEYS))


def _round_half_up(value: Fraction, places: str) -> float:
    exact = Decimal(value.numerator) / Decimal(value.denominator)
    return float(exact.quantize(Decimal(places), rounding=ROUND_HALF_UP)) + 0.0


def _percentage(made: int, attempted: int) -> float | None:
    return _round_half_up(Fraction(made, attempted), "0.001") if attempted else None


def _advanced_document(row: Mapping[str, Any]) -> dict[str, Any] | None:
    advanced = {key: row[key] for key in ADVANCED_KEYS}
    return None if all(value is None for value in advanced.values()) else advanced


def _player_document(row: Mapping[str, Any]) -> dict[str, Any]:
    totals = {key: row[key] for key in STAT_KEYS}
    return {
        "player_id": row["player_id"],
        "player_name": row["player_name"],
        "team": row["team_abbreviation"],
        "age": row["age"],
        "gp": row["gp"],
        "w": row["w"],
        "l": row["l"],
        "totals": totals,
        "per_game": {
            key: _round_half_up(Fraction(str(value)) / row["gp"], "0.1") for key, value in totals.items()
        },
        "pct": {
            "fg": _percentage(row["fgm"], row["fga"]),
            "fg3": _percentage(row["fg3m"], row["fg3a"]),
            "ft": _percentage(row["ftm"], row["fta"]),
        },
        "advanced": _advanced_document(row),
    }


class PlayerSeasonStatsRepository:
    """Own the table: whole-season replace for the loader, reads for the route."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def replace_season(self, season: str, season_type: str, rows: Iterable[Mapping[str, Any]]) -> int:
        """Replace one season's rows in a single transaction; return the new row count."""

        rows = list(rows)
        with self.engine.begin() as connection:
            connection.execute(
                delete(_TABLE).where(_TABLE.c.season == season, _TABLE.c.season_type == season_type)
            )
            if rows:
                connection.execute(insert(_TABLE), rows)
        return len(rows)

    def loaded_seasons(self, season_type: str) -> list[str]:
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    select(_TABLE.c.season)
                    .where(_TABLE.c.season_type == season_type)
                    .distinct()
                    .order_by(_TABLE.c.season)
                ).scalars()
            )

    def season_rows(
        self, season: str, season_type: str, *, player_id: int | None = None
    ) -> list[dict[str, Any]]:
        statement = select(_TABLE).where(_TABLE.c.season == season, _TABLE.c.season_type == season_type)
        if player_id is not None:
            statement = statement.where(_TABLE.c.player_id == player_id)
        statement = statement.order_by(_TABLE.c.player_id)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings()]


class PlayerSeasonStatsService:
    """Compose the season stats document from stored totals."""

    def __init__(self, repository: PlayerSeasonStatsRepository) -> None:
        self.repository = repository

    def get_season_stats(self, season: str, player_id: int | None = None) -> dict[str, Any]:
        """The regular-season document for ``season``, one player or all.

        Raises :class:`SeasonNotLoadedError` when the season has no rows at
        all; a ``player_id`` with no row in a loaded season answers an empty
        ``players`` list.
        """

        rows = self.repository.season_rows(season, REGULAR_SEASON, player_id=player_id)
        if not rows:
            loaded = self.repository.loaded_seasons(REGULAR_SEASON)
            if season not in loaded:
                raise SeasonNotLoadedError(season, loaded)
        return {
            "season": season,
            "season_type": REGULAR_SEASON,
            "players": [_player_document(row) for row in rows],
        }


def _advanced_values(frame: Any, *, season: str) -> dict[int, dict[str, Any]]:
    """Index a ``MeasureType=Advanced`` frame by player id as stored column values."""

    if frame is None or len(frame) == 0:
        raise ValueError(f"NBA returned no advanced player rows for {season}")
    missing = [column for column in REQUIRED_ADVANCED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"NBA advanced response is missing columns: {', '.join(missing)}")
    values: dict[int, dict[str, Any]] = {}
    for record in frame.to_dict("records"):
        player_id = int(record["PLAYER_ID"])
        if player_id in values:
            raise ValueError(f"NBA returned an advanced player more than once for {season}")
        values[player_id] = {
            key: _advanced_value(key, record[nba_key]) for key, nba_key in ADVANCED_KEYS.items()
        }
    return values


def _advanced_value(key: str, value: Any) -> float | int | None:
    if value is None or value != value:
        return None
    if key == "poss":
        return int(value)
    if key in _PERCENT_SCALED:
        return float(Decimal(str(value)) / 100)
    return float(value)


def rows_from_frame(
    frame: Any, advanced_frame: Any, *, season: str, fetched_at: datetime
) -> list[dict[str, Any]]:
    """Convert a Base/Totals frame and an Advanced frame to table rows.

    A Base player with no Advanced row stores null advanced columns.  Raises
    ``ValueError`` for an empty frame, a missing column, a repeated player or
    an unusable value, so a bad response is never written.
    """

    if frame is None or len(frame) == 0:
        raise ValueError(f"NBA returned no player rows for {season}")
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"NBA response is missing columns: {', '.join(missing)}")
    advanced = _advanced_values(advanced_frame, season=season)
    no_advanced = dict.fromkeys(ADVANCED_KEYS)
    rows: list[dict[str, Any]] = []
    for record in frame.to_dict("records"):
        row: dict[str, Any] = {
            "season": season,
            "season_type": REGULAR_SEASON,
            "player_id": int(record["PLAYER_ID"]),
            "player_name": str(record["PLAYER_NAME"]),
            "team_abbreviation": str(record["TEAM_ABBREVIATION"]),
            "age": int(record["AGE"]),
            "gp": int(record["GP"]),
            "w": int(record["W"]),
            "l": int(record["L"]),
            "fetched_at": fetched_at,
        }
        row.update(advanced.get(row["player_id"], no_advanced))
        for key in STAT_KEYS:
            value = record[key.upper()]
            row[key] = float(value) if key == "min" else int(value)
        if row["gp"] < 1:
            raise ValueError(f"player {row['player_id']} has no games played")
        rows.append(row)
    if len({row["player_id"] for row in rows}) != len(rows):
        raise ValueError(f"NBA returned a player more than once for {season}")
    return rows


def load_season(
    adapter: Any,
    repository: PlayerSeasonStatsRepository,
    season: str,
    *,
    now: datetime | None = None,
) -> int:
    """Fetch ``season`` and replace its stored rows; return the row count.

    ``adapter`` offers ``fetch_player_totals_stats`` and
    ``fetch_player_advanced_stats``.  Both fetches and the conversion finish
    before the transaction opens, so a failure of either leaves the prior rows
    untouched.  A rerun writes the same rows.
    """

    frame = adapter.fetch_player_totals_stats(season=season)
    advanced_frame = adapter.fetch_player_advanced_stats(season=season)
    rows = rows_from_frame(frame, advanced_frame, season=season, fetched_at=now or datetime.now(UTC))
    return repository.replace_season(season, REGULAR_SEASON, rows)
