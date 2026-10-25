"""Postgres check that withdrawing misdated ledger games reads timestamptz
tips as Eastern dates, leaves correctly dated games alone, and keeps a game a
publication contains."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from tests.services.test_ledger_backfill import activate_season, publish_observation, store_dated_game

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    yield engine
    engine.dispose()


def test_only_the_unpublished_late_tip_stored_on_its_utc_date_is_withdrawn(engine):
    activate_season(engine, "2024-25")
    for game_id, tip, stored in (
        ("0022400001", datetime(2024, 11, 15, 1, 30, tzinfo=UTC), "2024-11-15"),
        ("0022400002", datetime(2024, 11, 15, 0, 30, tzinfo=UTC), "2024-11-14"),
        ("0022400003", datetime(2024, 11, 14, 23, 30, tzinfo=UTC), "2024-11-14"),
        ("0022400004", datetime(2024, 11, 15, 1, 30, tzinfo=UTC), "2024-11-15"),
    ):
        observation_id = store_dated_game(
            engine, game_id, season="2024-25", scheduled_at=tip, stored_date=stored, provider="nba_live_data"
        )
    publish_observation(engine, "publication-1", observation_id, status="superseded")
    repository = CanonicalGameLedgerRepository(engine)

    withdrawn = repository.withdraw_misdated_games("2024-25")

    assert [
        (game.game_id, game.stored_game_date, game.eastern_game_date, game.verdict) for game in withdrawn
    ] == [
        ("0022400001", date(2024, 11, 15), date(2024, 11, 14), "withdraw"),
        ("0022400004", date(2024, 11, 15), date(2024, 11, 14), "referenced_by_publication"),
    ]
    assert repository.get_game("0022400001") is None
    assert repository.get_game("0022400002").game_date == date(2024, 11, 14)
    assert repository.get_game("0022400003").game_date == date(2024, 11, 14)
    assert repository.get_game("0022400004").game_date == date(2024, 11, 15)
    assert [game.game_id for game in repository.withdraw_misdated_games("2024-25")] == ["0022400004"]
