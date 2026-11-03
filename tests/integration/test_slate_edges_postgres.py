"""Slate edges on PostgreSQL: each game of an omitted season composes on its own transaction.

A failed statement aborts a PostgreSQL transaction and a lost connection takes
its transaction with it, so neither may cost the games composed after it their
cells, as at 3a1a7bb9.  Skipped unless ``TEST_DATABASE_URL`` names a
disposable database.
"""

from __future__ import annotations

import os
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app.config.settings import RuntimeSettings
from app.services.request_reads import request_read_scope
from app.services.slate_edges import SlateEdgesService
from tests.test_slate_edges import GENERATION, FakeSlate, _blend_player, _game, _matchup

pytestmark = pytest.mark.integration


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_pre_ping=True)
    yield engine
    engine.dispose()


class Publications:
    def snapshot(self, stream_keys, *, season, **_):
        return SimpleNamespace(generation=GENERATION, retained_history=())


class ScopedComposer:
    """Reads each game on its session, then its connection; ``fail`` breaks ``G1``'s read.

    It owns its scope as ``MatchupService.get_matchup_from_snapshot`` does:
    the caller's, else a repeatable-read one of its own.
    """

    def __init__(self, engine, fail):
        self.engine = engine
        self.fail = fail

    def get_matchup_from_snapshot(self, *, game_id, publication_snapshot, injuries, read_scope=None, **_):
        scope = (
            nullcontext(read_scope)
            if read_scope is not None
            else request_read_scope(self.engine, repeatable_read=True)
        )
        with scope as (connection, session):
            session.execute(text("SELECT 1"))
            if game_id == "G1":
                self.fail(connection)
            value = float(connection.execute(text("SELECT 0.05")).scalar_one())
            return _matchup([_blend_player(2, "B", value)])


def _divide_by_zero(connection):
    connection.execute(text("SELECT 1 / 0"))


def _terminate_backend(connection):
    """Terminate the connection's own backend, then read on it: a genuine disconnect."""

    pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
    with connection.engine.connect() as killer:
        killer.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    connection.execute(text("SELECT 1"))


@pytest.mark.parametrize("fail", [_divide_by_zero, _terminate_backend], ids=["statement", "disconnect"])
def test_a_game_whose_read_fails_leaves_the_later_games_cells(engine, fail):
    service = SlateEdgesService(
        slates=FakeSlate([_game("G1"), _game("G2")]),
        matchups=ScopedComposer(engine, fail),
        publication_reader=Publications(),
        injuries=object(),
        settings=RuntimeSettings(environment="testing", nba={"current_season": "2025-26"}),
        engine=engine,
    )

    body = service.get_edges("2026-04-10")

    assert [(game["game_id"], game["scores"]) for game in body["games"]] == [
        ("G1", []),
        ("G2", [{"canonical_id": 2, "value": 0.05}]),
    ]
    assert body["spotlight"]["best"]["canonical_id"] == 2
