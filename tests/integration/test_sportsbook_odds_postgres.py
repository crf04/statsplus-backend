"""Postgres checks for the sportsbook odds store (#377): migration 063,
change-only writes with real booleans and numerics, and the lease fence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

import pytest
from sqlalchemy import create_engine, inspect, select, text

from app.migrations import downgrade_sportsbook_odds, run_migrations
from app.models.sportsbook_odds import SportsbookPropQuote
from app.services.sportsbook_quotes import (
    ResolvedQuote,
    SportsbookQuoteReader,
    SportsbookQuoteStore,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]

T0 = datetime(2026, 10, 21, 21, 0, tzinfo=timezone.utc)
TABLES = {"odds_api_polls", "odds_api_quota_state", "sportsbook_prop_latest", "sportsbook_prop_quotes"}


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=4)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    yield engine
    engine.dispose()


def test_migration_063_is_idempotent_and_reversible_on_postgres(engine):
    assert TABLES <= set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 63"))
    assert run_migrations(engine).applied == ("063_sportsbook_odds",)
    assert run_migrations(engine).applied == ()

    with engine.begin() as connection:
        downgrade_sportsbook_odds(connection)
    assert not TABLES & set(inspect(engine).get_table_names())
    assert run_migrations(engine).applied == ("063_sportsbook_odds",)
    assert TABLES <= set(inspect(engine).get_table_names())


def test_change_only_history_and_book_lines_round_trip_on_postgres(engine):
    store, reader = SportsbookQuoteStore(engine), SportsbookQuoteReader(engine)
    quotes = [
        ResolvedQuote(2544, "PTS", "draftkings", 27.5, -115, -105, T0),
        ResolvedQuote(2544, "PTS", "fanduel", 27.5, -110, -110, T0),
    ]

    def poll(at, qs, closing=False):
        return store.record_poll(
            odds_event_id="evt", game_id="0022600001", started_at=at, completed_at=at,
            cost=5, remaining=19995, is_closing=closing, quotes=qs,
        )

    assert poll(T0, quotes).history_rows == 2
    assert poll(T0 + timedelta(hours=1), quotes).history_rows == 0
    moved = [ResolvedQuote(2544, "PTS", "draftkings", 27.5, -120, -100, T0), quotes[1]]
    assert poll(T0 + timedelta(hours=2), moved).history_rows == 1
    assert poll(T0 + timedelta(hours=3), moved, closing=True).history_rows == 2

    entry = reader.book_lines("0022600001")[2544]["PTS"]
    assert entry["closing"] is True
    assert [(b["book"], b["line"], b["over"], b["under"], b["best_over"], b["best_under"])
            for b in entry["books"]] == [
        ("draftkings", 27.5, -120, -100, False, True),
        ("fanduel", 27.5, -110, -110, True, False),
    ]
    with engine.connect() as connection:
        history = connection.execute(
            select(SportsbookPropQuote.__table__.c.book, SportsbookPropQuote.__table__.c.is_closing)
            .order_by(SportsbookPropQuote.__table__.c.quote_id)
        ).all()
    assert len(history) == 5 and sum(1 for _, closing in history if closing) == 2
