"""Migration 078: the athlete catalog's Position and the new Qualifier kinds.

The database predates the migration in the shape production has it: Diet
columns NOT NULL on ``target_qualifiers`` and no ``athlete_catalog.position``.
``exercise_upgrade`` is shared with the PostgreSQL run, where the same
statements run on the production dialect.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, text

from app.errors import ConflictError
from app.migrations import MIGRATIONS, run_migrations
from app.models.user import User
from app.services.user_service import UserService

pytestmark = pytest.mark.real_migrations

OWNER = "owner-uid"
CORNER_THREE = {"base": "shot_zones", "slice_key": "Corner 3", "comparator": "at_or_above", "threshold": 0.4}
WING_BIG = {"base": "position", "groups": ["Wing", "Big"]}
THREE_PA = {"base": "season_average", "stat": "3PA", "comparator": "at_or_above", "threshold": 0.3}


def _revert_to_the_pre_078_shape(engine):
    """Put the two tables back as migration 077 left them, holding one Target."""

    dialect = engine.dialect.name
    identity = "SERIAL PRIMARY KEY" if dialect == "postgresql" else "INTEGER PRIMARY KEY"
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE athlete_catalog DROP COLUMN position"))
        connection.execute(text("DROP TABLE target_qualifiers"))
        connection.execute(
            text(
                "CREATE TABLE target_qualifiers ("
                f"id {identity}, "
                "target_id INTEGER NOT NULL REFERENCES targets (id) ON DELETE CASCADE, "
                "position INTEGER NOT NULL, base VARCHAR(32) NOT NULL, "
                "slice_key VARCHAR(64) NOT NULL, comparator VARCHAR(16) NOT NULL, "
                "threshold FLOAT NOT NULL)"
            )
        )
        connection.execute(
            text("CREATE INDEX idx_target_qualifiers_target ON target_qualifiers (target_id, position)")
        )
        connection.execute(
            User.__table__.insert(),
            {
                "firebase_uid": OWNER,
                "email": "owner@example.com",
                "display_name": OWNER,
                "photo_url": None,
                "created_at": datetime(2026, 8, 1, tzinfo=UTC),
                "last_login": datetime(2026, 8, 1, tzinfo=UTC),
                "is_active": True,
            },
        )
        connection.execute(
            text(
                "INSERT INTO targets (firebase_uid, opponent, qualifier_signature, created_at, updated_at) "
                "VALUES (:uid, 'OKC', 'shot_zones:Corner 3:at_or_above:0.400000', :now, :now)"
            ),
            {"uid": OWNER, "now": datetime(2026, 8, 1, tzinfo=UTC)},
        )
        connection.execute(
            text(
                "INSERT INTO target_qualifiers (target_id, position, base, slice_key, comparator, threshold) "
                "SELECT id, 0, 'shot_zones', 'Corner 3', 'at_or_above', 0.4 FROM targets"
            )
        )
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 79"))


def exercise_upgrade(engine, settings):
    run_migrations(engine)
    _revert_to_the_pre_078_shape(engine)
    inspector = inspect(engine)
    assert "position" not in {column["name"] for column in inspector.get_columns("athlete_catalog")}
    assert not inspector.get_columns("target_qualifiers")[4]["nullable"]

    assert run_migrations(engine).applied == ("079_position_qualifiers",)
    assert run_migrations(engine).applied == ()

    inspector = inspect(engine)
    catalog = {column["name"]: column for column in inspector.get_columns("athlete_catalog")}
    assert catalog["position"]["nullable"] and str(catalog["position"]["type"]) == "VARCHAR(8)"
    qualifiers = {column["name"]: column for column in inspector.get_columns("target_qualifiers")}
    assert {name for name, column in qualifiers.items() if column["nullable"]} == {
        "slice_key",
        "comparator",
        "threshold",
        "average_stat",
        "position_groups",
    }
    assert "idx_target_qualifiers_target" in {
        index["name"] for index in inspector.get_indexes("target_qualifiers")
    }
    assert inspector.get_foreign_keys("target_qualifiers")[0]["options"]["ondelete"] == "CASCADE"

    # The Target that predates the migration reads exactly as it did.
    targets = UserService(engine, settings=settings)
    [existing] = targets.list_targets(OWNER)
    assert existing["title"] == "OKC vs Corner 3 ≥ 40%"
    assert existing["qualifiers"] == [CORNER_THREE]

    # The new kinds store, read back, and keep the duplicate rule.
    created = targets.create_target(OWNER, opponent="MEM", qualifiers=[CORNER_THREE, THREE_PA, WING_BIG])
    assert created["title"] == "MEM vs Wing/Big, Corner 3 ≥ 40%, 3PA avg ≥ 0.3"
    assert targets.get_target(OWNER, created["id"])["qualifiers"] == [CORNER_THREE, THREE_PA, WING_BIG]
    with pytest.raises(ConflictError):
        targets.create_target(
            OWNER,
            opponent="MEM",
            qualifiers=[{"base": "position", "groups": ["Big", "Wing"]}, THREE_PA, CORNER_THREE],
        )
    targets.delete_target(OWNER, created["id"])
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM target_qualifiers")).scalar_one() == 1


def test_migration_078_upgrades_a_database_with_the_old_target_and_catalog_shapes(tmp_path, runtime_settings):
    assert "079_position_qualifiers" in {migration.name for migration in MIGRATIONS}
    engine = create_engine(f"sqlite:///{tmp_path / 'at-077.sqlite3'}")
    try:
        exercise_upgrade(engine, runtime_settings)
    finally:
        engine.dispose()


def test_migration_078_finishes_a_rebuild_an_earlier_run_left_half_done(tmp_path, runtime_settings):
    engine = create_engine(f"sqlite:///{tmp_path / 'interrupted.sqlite3'}")
    try:
        run_migrations(engine)
        _revert_to_the_pre_078_shape(engine)
        # An interrupted run renamed the table and stopped before the copy.
        with engine.begin() as connection:
            connection.execute(text("DROP INDEX idx_target_qualifiers_target"))
            connection.execute(text("ALTER TABLE target_qualifiers RENAME TO target_qualifiers__079"))

        assert run_migrations(engine).applied == ("079_position_qualifiers",)

        [existing] = UserService(engine, settings=runtime_settings).list_targets(OWNER)
        assert existing["qualifiers"] == [CORNER_THREE]
        assert "target_qualifiers__079" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
