"""``/api/injuries`` minutes through the real dependency graph.

``build_dependencies`` wires the minutes service; only the stored report and
the schedule (injury snapshots and Event Catalog) are replaced.  The game logs
are real Publications.  The ``postgres`` case runs with ``-m integration`` and
``TEST_DATABASE_URL``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text

from app import create_app
from app.config.settings import FeatureSettings, RuntimeSettings
from app.dependencies import build_dependencies
from app.models import Base
from tests.routes.test_injuries import AFTERNOON, Events
from tests.services.test_backtest_seasons import LAST, NEW, World, _unpinned
from tests.services.test_injury_minutes import ALWAYS_FIVE, FOUR_PLUS_PRIOR, _games


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
def engine(request, tmp_path):
    if request.param == "sqlite":
        built = create_engine(f"sqlite:///{tmp_path / 'wiring.sqlite3'}")
    else:
        url = os.getenv("TEST_DATABASE_URL")
        if not url:
            pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
        built = create_engine(url)
        Base.metadata.drop_all(built)
        with built.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    yield built
    if request.param == "postgres":
        Base.metadata.drop_all(built)
        with built.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    built.dispose()


def test_the_route_returns_minutes_from_published_game_logs(engine, monkeypatch, authenticate):
    world = World(engine)
    world.compose("player_game_logs", LAST, {"rows": _games(FOUR_PLUS_PRIOR, LAST, [10, 20, 30])})
    world.compose(
        "player_game_logs",
        NEW,
        {
            "rows": _games(ALWAYS_FIVE, NEW, [30, 32, 34, 36, 38])
            + _games(FOUR_PLUS_PRIOR, NEW, [20, 20, 20, 20])
        },
    )
    settings = _unpinned().model_copy(
        update={
            "auth": _unpinned().auth.model_copy(update={"firebase_admin_disabled": True}),
            "features": FeatureSettings(injury_report_enabled=True),
            "database": _unpinned().database.model_copy(
                update={"url": engine.url.render_as_string(hide_password=False)}
            ),
        }
    )
    assert isinstance(settings, RuntimeSettings)
    monkeypatch.setattr("app.utils.db.get_engine", Mock(return_value=engine))
    monkeypatch.setattr("app.utils.cache_config.get_redis_client", Mock(return_value=None))
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    monkeypatch.setenv("FLASK_ENV", "testing")
    dependencies = build_dependencies(settings)

    snapshot = SimpleNamespace(
        normalized_entries=[
            {
                "source_player_name": "Always Five",
                "canonical_player_id": ALWAYS_FIVE,
                "tricode": "LAL",
                "canonical_status": "Out",
                "reason": "",
                "report_time": None,
            },
            {
                "source_player_name": "Four Plus Prior",
                "canonical_player_id": FOUR_PLUS_PRIOR,
                "tricode": "LAL",
                "canonical_status": "Out",
                "reason": "",
                "report_time": None,
            },
        ],
        retrieved_at=datetime(2026, 1, 15, 20, 0, tzinfo=UTC),
    )

    class Snapshots:
        def get_many(self, scopes):
            return {scopes[0]: snapshot}

    listing = dependencies.injury_listing_service
    listing.event_catalog = Events()
    listing.snapshot_repository = Snapshots()
    listing._clock = lambda: AFTERNOON
    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": dependencies,
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )

    body = app.test_client().get("/api/injuries?date=2026-01-15", headers=authenticate()).get_json()

    assert body["minutes_published_season"] == NEW
    assert [
        (r["player"], r["average_minutes"], r["games_played"], r["minutes_season"]) for r in body["injuries"]
    ] == [
        ("Always Five", 34.0, 5, NEW),
        ("Four Plus Prior", 20.0, 3, LAST),
    ]
