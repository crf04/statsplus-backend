"""Postgres check that withdrawing misdated ledger games reads timestamptz
tips as Eastern dates, leaves correctly dated games alone, keeps a game a
publication contains, and serializes with a concurrent ledger composition."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, text

from app.migrations import run_migrations
from app.models.collection_control import (
    CatalogPublication,
    CollectionManifest,
    CollectionObservation,
    PublicationObservation,
)
from app.services import canonical_game_ledger
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from app.services.collection_control import ControlPlaneError, PublicationService
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


TIP = datetime(2024, 11, 15, 1, 30, tzinfo=UTC)
NOW = TIP + timedelta(days=1)
# The composing manifest pins an earlier Event Catalog that listed the tip
# on the stored day; a later refresh moved it before Eastern midnight.
PINNED_TIP = datetime(2024, 11, 15, 17, 0, tzinfo=UTC)


def misdated_game_a_ledger_stream_can_compose(engine):
    """A game the composing manifest's catalog dates as stored while the
    current Event Catalog dates it a day earlier, so a composition may
    publish it and the withdrawal flags it."""

    activate_season(engine, "2024-25")
    observation_id = store_dated_game(
        engine,
        "0022400001",
        season="2024-25",
        scheduled_at=TIP,
        stored_date="2024-11-15",
        provider="nba_live_data",
    )
    service = PublicationService(engine, clock=lambda: NOW)
    service.register_default_streams()
    catalog = json.dumps(
        {
            "events": [
                {
                    "nba_game_id": "0022400001",
                    "scheduled_at": PINNED_TIP.isoformat(),
                    "home_team_id": 1610612747,
                    "away_team_id": 1610612759,
                    "status": "Final",
                    "status_code": 3,
                }
            ]
        }
    )
    checksum = hashlib.sha256(catalog.encode()).hexdigest()
    with engine.begin() as connection:
        connection.execute(
            CatalogPublication.__table__.insert().values(
                publication_id="event-catalog",
                season="2024-25",
                catalog_type="event",
                cutoff=NOW,
                version="v1",
                checksum=checksum,
                payload=catalog,
                complete=True,
                published_at=NOW,
            )
        )
        connection.execute(
            CollectionManifest.__table__.insert().values(
                manifest_id="ledger-manifest",
                season="2024-25",
                cutoff=NOW,
                collect_before=NOW + timedelta(hours=1),
                accepted_versions="[1]",
                scopes='["canonical_game_ledger"]',
                checksum="manifest",
                event_catalog_publication_id="event-catalog",
                event_catalog_checksum=checksum,
                status="active",
                created_at=NOW,
            )
        )
        connection.execute(
            CollectionObservation.__table__.update()
            .where(CollectionObservation.observation_id == observation_id)
            .values(manifest_id="ledger-manifest")
        )
    return service, observation_id


def compose(service, observation_id, session=None):
    return service.compose_inactive_ledger(
        "traditional_opponent_season",
        season="2024-25",
        cutoff=NOW,
        payload=[{"team_id": 1610612747, "opponent_points": 99}],
        provenance={observation_id: "0022400001"},
        session=session,
    )


def wait_for_lock_wait(engine, worker):
    """Return once ``worker`` finished or another backend waits on a row lock."""

    deadline = time.monotonic() + 10
    with engine.connect() as connection:
        while worker.is_alive() and time.monotonic() < deadline:
            waiting = connection.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")
            ).scalar_one()
            if waiting:
                return
            time.sleep(0.05)


def publications_naming(engine, observation_id):
    with engine.connect() as connection:
        return connection.execute(
            select(PublicationObservation.publication_id).where(
                PublicationObservation.observation_id == observation_id
            )
        ).all()


def test_a_composition_racing_the_withdrawal_after_its_guard_cannot_publish_the_withdrawn_game(
    engine, monkeypatch
):
    service, observation_id = misdated_game_a_ledger_stream_can_compose(engine)
    composed: list[object] = []
    guard = canonical_game_ledger._publication_ids_by_game

    def compose_in_another_transaction():
        try:
            composed.append(compose(service, observation_id))
        except ControlPlaneError as error:
            composed.append(error)

    def guard_then_race(connection, game_ids):
        found = guard(connection, game_ids)
        worker = threading.Thread(target=compose_in_another_transaction)
        worker.start()
        wait_for_lock_wait(engine, worker)
        threads.append(worker)
        return found

    threads: list[threading.Thread] = []
    monkeypatch.setattr(canonical_game_ledger, "_publication_ids_by_game", guard_then_race)

    withdrawn = CanonicalGameLedgerRepository(engine).withdraw_misdated_games("2024-25")
    threads[0].join(10)

    assert [(game.game_id, game.verdict) for game in withdrawn] == [("0022400001", "withdraw")]
    assert CanonicalGameLedgerRepository(engine).get_game("0022400001") is None
    assert [str(item) for item in composed] == ["ledger_provenance_not_accepted"]
    assert publications_naming(engine, observation_id) == []


def test_a_withdrawal_racing_an_uncommitted_composition_keeps_the_game_it_publishes(engine):
    service, observation_id = misdated_game_a_ledger_stream_can_compose(engine)
    withdrawn: list[object] = []
    worker = threading.Thread(
        target=lambda: withdrawn.extend(
            CanonicalGameLedgerRepository(engine).withdraw_misdated_games("2024-25")
        )
    )

    with service.session() as session, session.begin():
        publication_id = compose(service, observation_id, session=session).publication_id
        worker.start()
        wait_for_lock_wait(engine, worker)
    worker.join(10)

    assert [(game.game_id, game.verdict, game.publication_ids) for game in withdrawn] == [
        ("0022400001", "referenced_by_publication", (publication_id,))
    ]
    assert CanonicalGameLedgerRepository(engine).get_game("0022400001").game_date == date(2024, 11, 15)
    assert publications_naming(engine, observation_id) == [(publication_id,)]
