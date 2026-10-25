"""Postgres check that withdrawing misdated ledger games reads timestamptz
tips as Eastern dates and leaves correctly dated games alone."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from app.models.event_catalog import EventCatalogEntry
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository, canonical_game_from_pbp
from tests.services.test_ledger_backfill import _event, _Participants, _payload

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


def _store(engine, game_id, scheduled_at, stored_date):
    event = {**_event(), "nba_game_id": game_id, "scheduled_at": scheduled_at.isoformat()}
    with engine.begin() as connection:
        connection.execute(
            EventCatalogEntry.__table__.insert().values(
                nba_game_id=game_id,
                season="2024-25",
                home_team_id=1610612747,
                home_team_name="Lakers",
                home_team_tricode="LAL",
                away_team_id=1610612759,
                away_team_name="Spurs",
                away_team_tricode="SAS",
                scheduled_at=scheduled_at,
                status_text="Final",
                status_code=3,
                classification="Regular Season",
                first_seen_at=scheduled_at,
                last_seen_at=scheduled_at,
            )
        )
    payload = _payload()
    payload["date"] = stored_date
    CanonicalGameLedgerRepository(engine).replace_game(
        canonical_game_from_pbp(
            payload,
            event=event,
            season="2024-25",
            source_observation_id=f"seed:{game_id}",
            retrieved_at=datetime(2024, 11, 16, tzinfo=UTC),
            participant_ids_by_team=_Participants().get_participants("seed", game_id=game_id),
        )
    )


def test_only_the_late_tip_stored_on_its_utc_date_is_withdrawn(engine):
    _store(engine, "0022400001", datetime(2024, 11, 15, 1, 30, tzinfo=UTC), "2024-11-15")
    _store(engine, "0022400002", datetime(2024, 11, 15, 0, 30, tzinfo=UTC), "2024-11-14")
    _store(engine, "0022400003", datetime(2024, 11, 14, 23, 30, tzinfo=UTC), "2024-11-14")
    repository = CanonicalGameLedgerRepository(engine)

    withdrawn = repository.withdraw_misdated_games("2024-25")

    assert [(game.game_id, game.stored_game_date, game.eastern_game_date) for game in withdrawn] == [
        ("0022400001", date(2024, 11, 15), date(2024, 11, 14))
    ]
    assert repository.get_game("0022400001") is None
    assert repository.get_game("0022400002").game_date == date(2024, 11, 14)
    assert repository.get_game("0022400003").game_date == date(2024, 11, 14)
    assert repository.withdraw_misdated_games("2024-25") == ()
