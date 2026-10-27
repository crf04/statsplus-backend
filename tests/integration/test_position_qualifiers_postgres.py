"""Migration 078 and the Position and Average Qualifiers on PostgreSQL.

The ``DROP NOT NULL``, the added ``JSON`` column and the unique signature index
run on the production dialect here; SQLite rebuilds the table instead.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.config.settings import load_settings
from tests.test_position_qualifiers_migration import exercise_upgrade

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


def test_migration_078_upgrades_the_old_shapes_on_postgres():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        exercise_upgrade(engine, load_settings(overrides={"DATABASE_URL": url}))
    finally:
        engine.dispose()
