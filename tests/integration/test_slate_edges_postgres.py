"""Slate edges on PostgreSQL: every game composes on the request's one transaction.

A failed statement aborts a PostgreSQL transaction, so one game's failed read
must not cost the games composed after it their cells.  Skipped unless
``TEST_DATABASE_URL`` names a disposable database.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app.config.settings import RuntimeSettings
from app.services.slate_edges import SlateEdgesService
from tests.test_slate_edges import GENERATION, FakeSlate, _blend_player, _game, _matchup

pytestmark = pytest.mark.integration


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url)
    yield engine
    engine.dispose()


class Publications:
    def snapshot(self, stream_keys, *, season, **_):
        return SimpleNamespace(generation=GENERATION, retained_history=())


class ScopedComposer:
    """Reads each game on the request's session, then its connection; ``G1``'s read fails.

    The session's first read is ``G1``'s, so it joins the request's transaction
    while that game composes.
    """

    def get_matchup_from_snapshot(self, *, game_id, publication_snapshot, injuries, read_scope, **_):
        connection, session = read_scope
        session.execute(text("SELECT 1"))
        if game_id == "G1":
            connection.execute(text("SELECT 1 / 0"))
        value = float(connection.execute(text("SELECT 0.05")).scalar_one())
        return _matchup([_blend_player(2, "B", value)])


def test_a_game_whose_read_fails_leaves_the_later_games_cells(engine):
    service = SlateEdgesService(
        slates=FakeSlate([_game("G1"), _game("G2")]),
        matchups=ScopedComposer(),
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
