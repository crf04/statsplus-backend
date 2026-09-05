#!/usr/bin/env python3
"""Validate the tracked public SQLite demo database without mutating it."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import quote


REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_PATH: Final[Path] = REPOSITORY_ROOT / "nba_play_types.db"

# These are the stable identity columns used by the read paths.  The
# validator deliberately checks a contract rather than copying the complete
# provider-generated table definitions, whose metric columns change over
# time.
#
# The opponent team tables the Team Profile categories once read are not
# required: those categories are served from the Season publications, which
# the demo database does not carry, so they return empty here (the #198
# precedent for the game-log Team Filters).
REQUIRED_DEMO_COLUMNS: Final[dict[str, frozenset[str]]] = {
    "Player_Information": frozenset({"id", "full_name", "is_active"}),
    "Player_Per36_Stats": frozenset({"PLAYER_ID", "PLAYER_NAME", "PTS"}),
    "Player_Team_Table": frozenset({"Player", "Current Team", "Team_ID"}),
    "Team_Info": frozenset({"id", "full_name", "abbreviation"}),
    "pbp_opponent_stats": frozenset({"EntityId", "TeamId", "Name"}),
    "player_clusters": frozenset({"PlayerName", "ClusterID", "PlayerID"}),
    "player_play_types": frozenset({"PLAYER_NAME", "TEAM_ABBREVIATION"}),
    "player_shooting_zones": frozenset({"PLAYER_NAME", "Restricted Area_FGM"}),
    "users": frozenset({"firebase_uid", "email", "is_active"}),
}


@dataclass(frozen=True, slots=True)
class DemoDatabaseValidation:
    """Read-only validation results for a demo database."""

    path: Path
    issues: tuple[str, ...]
    user_count: int | None = None

    @property
    def valid(self) -> bool:
        """Whether the database satisfies the public demo contract."""
        return not self.issues


def _quote_identifier(identifier: str) -> str:
    """Quote a known SQLite identifier for introspection queries."""
    return '"' + identifier.replace('"', '""') + '"'


def _read_only_connection(database_path: Path) -> sqlite3.Connection:
    """Open an existing SQLite file in read-only mode."""
    uri = f"file:{quote(str(database_path.resolve()), safe='/')}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def validate_demo_database(database_path: str | Path = DEFAULT_DATABASE_PATH) -> DemoDatabaseValidation:
    """Validate the demo schema and ensure it contains no user records.

    The file is opened with SQLite's ``mode=ro`` URI flag.  Missing files and
    malformed databases are reported as validation issues instead of being
    created or repaired.
    """
    path = Path(database_path)
    if not path.is_file():
        return DemoDatabaseValidation(path, (f"database file does not exist: {path}",))

    issues: list[str] = []
    user_count: int | None = None
    try:
        with _read_only_connection(path) as connection:
            table_rows = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            table_names = {row[0] for row in table_rows}

            for table_name, required_columns in REQUIRED_DEMO_COLUMNS.items():
                if table_name not in table_names:
                    issues.append(f"missing required table: {table_name}")
                    continue

                columns = {
                    row[1]
                    for row in connection.execute(
                        f"PRAGMA table_info({_quote_identifier(table_name)})"
                    )
                }
                missing_columns = sorted(required_columns - columns)
                if missing_columns:
                    issues.append(
                        f"table {table_name} is missing required columns: "
                        f"{', '.join(missing_columns)}"
                    )

            if "users" in table_names:
                user_count = connection.execute(
                    'SELECT COUNT(*) FROM "users"'
                ).fetchone()[0]
                if user_count:
                    issues.append(
                        f"users table contains {user_count} record(s); "
                        "the public demo database must not contain user data"
                    )
    except sqlite3.Error as error:
        issues.append(f"could not read SQLite database: {error}")

    return DemoDatabaseValidation(path, tuple(issues), user_count)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the public demo SQLite database without changing it."
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        help="database path (default: nba_play_types.db)",
    )
    parser.add_argument(
        "--database",
        "--database-path",
        dest="database_option",
        type=Path,
        help="database path (alternative to the positional path)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the validator command and return a process exit status."""
    args = _build_parser().parse_args(argv)
    path = args.database_option or args.path or DEFAULT_DATABASE_PATH
    result = validate_demo_database(path)
    if result.valid:
        print(f"Demo database is valid: {result.path}")
        return 0

    print("Demo database validation failed:", file=sys.stderr)
    for issue in result.issues:
        print(f"- {issue}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
