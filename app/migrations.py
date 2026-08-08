"""Repeatable migrations for the application's own database schema.

The bundled NBA data is a public read-only fixture and is not managed by
these migrations.  Migrations only create or upgrade tables owned by the
application, starting with the ``users`` table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Final

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    insert,
    select,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.sql import func


MIGRATION_TABLE_NAME: Final[str] = "schema_migrations"


@dataclass(frozen=True, slots=True)
class Migration:
    """A single ordered schema migration."""

    version: int
    name: str
    upgrade: Callable[[Connection], None]


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """The result of applying zero or more pending migrations."""

    applied: tuple[str, ...]
    current_version: int


_migration_metadata = MetaData()
_schema_migrations = Table(
    MIGRATION_TABLE_NAME,
    _migration_metadata,
    Column("version", Integer, primary_key=True),
    Column("name", String(255), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


def _create_users_table(connection: Connection) -> None:
    """Create all SQLAlchemy models registered by the application."""
    from app.models import Base

    Base.metadata.create_all(connection)


MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(1, "001_create_users", _create_users_table),
)


def run_migrations(engine: Engine) -> MigrationResult:
    """Apply pending application-schema migrations to ``engine``.

    Migrations are recorded in ``schema_migrations`` and are safe to run
    repeatedly.  An existing database without the bookkeeping table is
    treated as a pre-migration database: the current model schema is created
    if needed and then marked as applied without touching existing rows.
    """
    _validate_migration_order()

    with engine.begin() as connection:
        _migration_metadata.create_all(connection)
        applied_versions = {
            row.version
            for row in connection.execute(
                select(_schema_migrations.c.version)
            )
        }
        applied_names: list[str] = []

        for migration in MIGRATIONS:
            if migration.version in applied_versions:
                continue

            migration.upgrade(connection)
            connection.execute(
                insert(_schema_migrations).values(
                    version=migration.version,
                    name=migration.name,
                )
            )
            applied_versions.add(migration.version)
            applied_names.append(migration.name)

    return MigrationResult(
        applied=tuple(applied_names),
        current_version=max(applied_versions, default=0),
    )


def _validate_migration_order() -> None:
    """Reject duplicate or out-of-order migration definitions early."""
    versions = [migration.version for migration in MIGRATIONS]
    if versions != sorted(set(versions)):
        raise ValueError("Migrations must have unique, increasing versions")

