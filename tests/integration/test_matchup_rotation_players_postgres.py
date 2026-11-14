"""Rotation Players on PostgreSQL (crf04/statsplus#459).

The team's last-five-games read is a grouped, limited subquery inside ``IN``;
this runs the SQLite scenario against the production dialect.  Skipped unless
``TEST_DATABASE_URL`` names a disposable database.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.models import Base
from tests.services.test_matchup_rotation_players import assert_rotation_scenario, world

pytestmark = pytest.mark.integration


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    built = create_engine(url)
    Base.metadata.drop_all(built)
    with built.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    yield built
    Base.metadata.drop_all(built)
    with built.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    built.dispose()


def test_a_scheduled_matchup_lists_each_rotation_player_after_the_pool_on_postgres(engine):
    assert_rotation_scenario(world(engine))


def test_a_publication_committed_mid_request_never_empties_its_rotation_players_on_postgres(engine, tmp_path):
    """The game-season capture reads in the request's own repeatable-read scope."""

    from tests.services.test_selected_season_matchup import _RosterCatalog, _rotation_ids
    from tests.services.test_serving_season_matchup import GAME_ID, _log_payload
    from tests.support.season_cutover import (
        NEW,
        PLAYER_LOGS,
        THIN_CUTOFF,
        THIN_GAMES,
        THIN_PROVENANCE,
        log_rows,
    )
    from tests.support.selected_season import SelectedWorld

    served = SelectedWorld(tmp_path, engine=engine)
    served.hold_opening_week()
    served.service.athlete_catalog = _RosterCatalog(log_rows(NEW, THIN_GAMES))
    before = _rotation_ids(served.service.get_matchup(game_id=GAME_ID))
    get_event = served.service.event_catalog.get_event
    committed = []

    def read_then_commit(season, game_id):
        event = get_event(season, game_id)
        if not committed:
            payload = _log_payload(NEW, THIN_GAMES)
            for row in payload["rows"]:
                row["minutes"] += 1
            committed.append(
                served.publications.recompose_ledger(
                    PLAYER_LOGS,
                    season=NEW,
                    cutoff=THIN_CUTOFF,
                    payload=payload,
                    provenance=THIN_PROVENANCE,
                    reason="a recompose that commits while a Matchup composes",
                )
            )
        return event

    served.service.event_catalog.get_event = read_then_commit
    during = _rotation_ids(served.service.get_matchup(game_id=GAME_ID))

    assert before
    assert committed
    assert during == before
