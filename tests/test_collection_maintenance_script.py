from datetime import datetime, timezone

from sqlalchemy import create_engine, func, select

from app.migrations import run_migrations
from app.models.collection_control import CollectionObservation
from scripts.collection_maintenance import main

OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _database_with_expired_observation(tmp_path):
    url = f"sqlite:///{tmp_path / 'maintenance.sqlite3'}"
    engine = create_engine(url)
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            CollectionObservation.__table__.insert().values(
                observation_id="obs:expired",
                client_observation_id="obs:expired",
                collector_id="test",
                manifest_id=None,
                environment="testing",
                provider="pbp",
                observation_type="canonical_game_ledger",
                scope="{}",
                season="2019-20",
                cutoff=OLD,
                schema_version=1,
                checksum="a" * 64,
                payload="{}",
                payload_bytes=2,
                retrieved_at=OLD,
                accepted_at=OLD,
            )
        )
    engine.dispose()
    return url


def _observation_count(url):
    engine = create_engine(url)
    with engine.connect() as connection:
        count = connection.scalar(select(func.count()).select_from(CollectionObservation))
    engine.dispose()
    return count


def test_reconcile_only_enqueues_without_running_retention(tmp_path, capsys):
    url = _database_with_expired_observation(tmp_path)

    assert (
        main(
            [
                "--database-url",
                url,
                "--season",
                "2025-26",
                "--cutoff",
                "2026-08-21T16:26:09Z",
                "--reconcile-only",
            ]
        )
        == 0
    )

    assert capsys.readouterr().out.strip() == "{'jobs_enqueued': 0}"
    assert _observation_count(url) == 1


def test_full_maintenance_still_deletes_expired_observations(tmp_path, capsys):
    url = _database_with_expired_observation(tmp_path)

    assert (
        main(
            [
                "--database-url",
                url,
                "--season",
                "2025-26",
                "--cutoff",
                "2026-08-21T16:26:09Z",
            ]
        )
        == 0
    )

    assert _observation_count(url) == 0
