"""One-time migration from SQLite to Postgres with table name normalization.

Usage (locally):
    export DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DB
    python -m scripts.migrate_sqlite_to_postgres --sqlite nba_play_types.db

Notes:
    - Table names are normalized via `app.utils.tables.normalize_table_name` to
      match the codebase's Postgres-friendly snake_case names.
    - This performs simple row-copy using pandas; primary keys, indexes, and
      constraints are not recreated. Re-add them later if needed.
    - Run this once to seed your Railway Postgres. Subsequent app runs will
      maintain data via existing endpoints.
"""

from __future__ import annotations

import argparse
import os
from typing import Dict, List, Tuple
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

# Import normalization mapping from the app package
from app.utils.tables import normalize_table_name
from dotenv import load_dotenv


def load_env_files() -> None:
    """Load environment variables from .env files.

    Attempts to load:
    - Current working directory .env
    - Project root .env (parent of this script's directory)
    """
    # Load from CWD if present
    load_dotenv()
    # Load from project root (nba-backend/.env) if present
    project_root = Path(__file__).resolve().parents[1]
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)


def create_sqlite_engine(sqlite_path: str) -> Engine:
    """Create a SQLAlchemy engine for the SQLite database file."""
    return create_engine(f"sqlite:///{sqlite_path}")


def create_postgres_engine(database_url: str) -> Engine:
    """Create a SQLAlchemy engine for Postgres.

    Also normalizes the postgres URL scheme if needed.
    """
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif database_url.startswith("postgresql://") and "+" not in database_url:
        database_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return create_engine(database_url)


def list_sqlite_tables(engine: Engine) -> List[str]:
    """List all table names in the SQLite database."""
    inspector = inspect(engine)
    return inspector.get_table_names()


def read_sqlite_table(engine: Engine, table_name: str) -> pd.DataFrame:
    """Read a SQLite table into a DataFrame, handling names with spaces."""
    try:
        return pd.read_sql_table(table_name, con=engine)
    except Exception:
        # Fall back to quoted identifier for names with spaces/special chars
        return pd.read_sql(f"SELECT * FROM '{table_name}'", con=engine)


def write_postgres_table(engine: Engine, df: pd.DataFrame, table_name: str) -> None:
    """Write DataFrame to Postgres, replacing existing table contents."""
    df.to_sql(table_name, con=engine, if_exists="replace", index=False)


def migrate(sqlite_path: str, database_url: str) -> None:
    """Migrate all SQLite tables to Postgres with normalized names.

    Parameters
    ----------
    sqlite_path: str
        Path to the SQLite database file.
    database_url: str
        Postgres connection URL (e.g., from Railway `DATABASE_URL`).
    """

    sqlite_engine = create_sqlite_engine(sqlite_path)
    pg_engine = create_postgres_engine(database_url)

    tables = list_sqlite_tables(sqlite_engine)
    if not tables:
        print("No tables found in SQLite database.")
        return

    print(f"Found {len(tables)} tables in SQLite. Starting migration...\n")

    for original_name in tables:
        normalized_name = normalize_table_name(original_name)
        print(f"- {original_name}  ->  {normalized_name}")

        df = read_sqlite_table(sqlite_engine, original_name)
        if df.empty:
            print("  (skipped; table is empty)")
            continue

        write_postgres_table(pg_engine, df, normalized_name)
        print(f"  migrated {len(df)} rows")

    # Simple connectivity check
    try:
        with pg_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("\nMigration complete and Postgres connection verified.")
    except Exception as err:
        print(f"\nMigration complete, but Postgres health check failed: {err}")


def main() -> None:
    """CLI entrypoint."""
    # Ensure .env values are available as defaults
    load_env_files()

    parser = argparse.ArgumentParser(description="Migrate SQLite tables to Postgres.")
    parser.add_argument(
        "--sqlite",
        dest="sqlite_path",
        default=os.getenv("SQLITE_PATH", "nba_play_types.db"),
        help="Path to SQLite DB file (default: nba_play_types.db)",
    )
    parser.add_argument(
        "--database-url",
        dest="database_url",
        default=os.getenv("DATABASE_URL", ""),
        help="Postgres DATABASE_URL (overrides env if provided)",
    )

    args = parser.parse_args()
    if not args.database_url:
        env_val = os.getenv("DATABASE_URL", "")
        if not env_val:
            raise SystemExit("DATABASE_URL is required (env var or --database-url)")
        args.database_url = env_val

    migrate(sqlite_path=args.sqlite_path, database_url=args.database_url)


if __name__ == "__main__":
    main()

