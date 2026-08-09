"""SQLite referential integrity is on for every connection the app opens."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, insert, select, text

from app.migrations import run_migrations
from app.models.athlete_mapping import (
    AthleteMappingDecision,
    AthleteMappingDecisionContradiction,
)
from app.config.settings import DatabaseSettings, RuntimeSettings
from app.utils.db import get_engine


def _application_engine(tmp_path):
    """The engine the application itself would build for a SQLite database."""

    settings = RuntimeSettings(
        database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'app.sqlite3'}")
    )
    get_engine.cache_clear()
    engine = get_engine(settings)
    run_migrations(engine)
    return engine


def _decision_values(**overrides):
    values = {
        "provider": "prizepicks",
        "provider_athlete_id": "pp-15",
        "requested_season": "2024-25",
        "decision_state": "mapping_conflict",
        "provider_name": "Nikola Jokic",
        "created_at": datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return values


def test_application_sqlite_connections_enforce_foreign_keys(tmp_path):
    engine = _application_engine(tmp_path)

    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_deleting_a_decision_leaves_no_contradiction_children(tmp_path):
    engine = _application_engine(tmp_path)

    with engine.begin() as connection:
        decision_id = connection.execute(
            insert(AthleteMappingDecision.__table__).values(**_decision_values())
        ).inserted_primary_key[0]
        connection.execute(
            insert(AthleteMappingDecisionContradiction.__table__).values(
                decision_id=decision_id,
                evidence_index=0,
                provider_athlete_id="pp-15",
                provider_name="Nikola Jokic",
            )
        )

    with engine.begin() as connection:
        connection.execute(
            AthleteMappingDecision.__table__.delete().where(
                AthleteMappingDecision.__table__.c.id == decision_id
            )
        )

    with engine.connect() as connection:
        assert (
            connection.execute(
                select(AthleteMappingDecisionContradiction.__table__)
            ).fetchall()
            == []
        )


def test_a_contradiction_cannot_name_a_decision_that_does_not_exist(tmp_path):
    engine = _application_engine(tmp_path)

    with pytest.raises(Exception) as failure:
        with engine.begin() as connection:
            connection.execute(
                insert(AthleteMappingDecisionContradiction.__table__).values(
                    decision_id=4242,
                    evidence_index=0,
                )
            )
    assert "FOREIGN KEY" in str(failure.value).upper()


def test_the_invariant_holds_for_engines_the_tests_build_themselves(tmp_path):
    """Every SQLite engine in the process shares the application's invariant."""

    engine = create_engine(f"sqlite:///{tmp_path / 'direct.sqlite3'}")

    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
