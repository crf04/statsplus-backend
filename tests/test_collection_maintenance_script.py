from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select

from app.migrations import run_migrations
from app.models.collection_control import CollectionObservation, PublicationVersion
from app.services.collection_control import CollectionOperationsService, PublicationService
from scripts.collection_maintenance import main

OLD = datetime(2020, 1, 1, tzinfo=UTC)


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


def _with_four_publication_versions(url):
    """Four versions of one stream: a full maintenance run prunes the two oldest."""

    engine = create_engine(url)
    now = [datetime(2026, 8, 12, tzinfo=UTC)]
    service = PublicationService(engine, clock=lambda: now[0])
    service.register_stream(
        "scratch_stream",
        provider="nba",
        owner="collector",
        required_observations=[],
        publication_strategy="replace",
        supported_windows=["season"],
        enabled=True,
    )
    for index in range(4):
        service.compose(
            "scratch_stream",
            season="2025-26",
            cutoff=now[0] - timedelta(days=4 - index),
            payload={"i": index},
            expected_fence=index,
            reason="seed",
        )
        now[0] += timedelta(minutes=1)
    engine.dispose()


def _publication_versions(url):
    engine = create_engine(url)
    with engine.connect() as connection:
        versions = connection.scalars(
            select(PublicationVersion.version).order_by(PublicationVersion.version)
        ).all()
    engine.dispose()
    return list(versions)


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


def test_alerts_only_deletes_no_observations_and_prunes_nothing(tmp_path, capsys):
    url = _database_with_expired_observation(tmp_path)
    _with_four_publication_versions(url)

    assert main(["--database-url", url, "--alerts-only"]) == 0

    assert (
        capsys.readouterr().out.strip()
        == "{'cycles_evaluated': 0, 'cycles_attention': 0, 'cycles_completed': 0}"
    )
    assert _observation_count(url) == 1
    assert _publication_versions(url) == [1, 2, 3, 4]


def test_full_maintenance_prunes_the_same_publication_history(tmp_path):
    url = _database_with_expired_observation(tmp_path)
    _with_four_publication_versions(url)

    assert main(["--database-url", url, "--season", "2025-26", "--cutoff", "2026-08-21T16:26:09Z"]) == 0

    assert _publication_versions(url) == [3, 4]


@pytest.mark.parametrize(
    "extra",
    [
        ["--alerts-only", "--reconcile-only"],
        ["--cycle-id", "abc", "--season", "2025-26", "--cutoff", "2026-08-21T16:26:09Z"],
        [],
    ],
)
def test_option_combinations_that_make_no_sense_are_refused(tmp_path, extra):
    url = _database_with_expired_observation(tmp_path)

    with pytest.raises(SystemExit) as refused:
        main(["--database-url", url, *extra])

    assert refused.value.code == 2
    assert _observation_count(url) == 1


def _forbid_everything_but_alerts(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("alerts-only must not reconcile, collect garbage or prune")

    monkeypatch.setattr(PublicationService, "reconcile_pending", forbidden)
    monkeypatch.setattr(PublicationService, "prune_history", forbidden)
    monkeypatch.setattr(CollectionOperationsService, "gc_observations", forbidden)


def test_alerts_only_never_reconciles_collects_or_prunes(tmp_path, monkeypatch, capsys):
    url = _database_with_expired_observation(tmp_path)
    _forbid_everything_but_alerts(monkeypatch)

    assert main(["--database-url", url, "--alerts-only"]) == 0

    assert (
        capsys.readouterr().out.strip()
        == "{'cycles_evaluated': 0, 'cycles_attention': 0, 'cycles_completed': 0}"
    )


def test_alerts_only_prints_the_counts_of_the_cycles_it_evaluated(tmp_path, monkeypatch, capsys):
    from datetime import UTC, datetime

    from tests.services.test_collection_control import (
        _ledger_cycle,
        _seed_governed_catalog_evidence,
    )

    url = f"sqlite:///{tmp_path / 'cycle.sqlite3'}"
    engine = create_engine(url)
    run_migrations(engine)
    _seed_governed_catalog_evidence(engine)
    clock = [datetime(2026, 8, 12, tzinfo=UTC)]
    _, cycle, _, _ = _ledger_cycle(engine, clock)
    engine.dispose()
    _forbid_everything_but_alerts(monkeypatch)

    assert main(["--database-url", url, "--alerts-only", "--cycle-id", cycle.cycle_id]) == 0

    assert (
        capsys.readouterr().out.strip()
        == "{'cycles_evaluated': 1, 'cycles_attention': 1, 'cycles_completed': 0}"
    )
