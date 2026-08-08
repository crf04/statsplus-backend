"""Offline tests for durable data-refresh jobs and atomic publication.

All tests use temporary SQLite databases and patched refresh callables, so the
bundled ``nba_play_types.db`` fixture and external providers are never touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Barrier, Thread

import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.errors import DuplicateOperationError, ProviderUnavailableError
from app.migrations import run_migrations
from app.models.job import JOB_STATUS_FAILED, JOB_STATUS_QUEUED, JOB_STATUS_SUCCEEDED
from app.services.job_service import (
    DEFAULT_FAILURE_SUMMARY,
    DataRefreshJobService,
    SynchronousExecutor,
    adapt_zero_arg_handler,
)
from app.services.table_publisher import AtomicTablePublisher, TablePublicationError
from app.utils import telemetry


def _fixed_clock() -> datetime:
    return datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def job_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.sqlite3'}")
    run_migrations(engine)
    return engine


@pytest.fixture
def job_service(job_engine):
    return DataRefreshJobService(
        job_engine,
        executor=SynchronousExecutor(),
        clock=_fixed_clock,
        handlers={
            "update_database": adapt_zero_arg_handler(lambda: True),
        },
    )


@pytest.fixture
def publisher_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'publication.sqlite3'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alpha (value TEXT)"))
        connection.execute(text("INSERT INTO alpha VALUES ('old-alpha')"))
        connection.execute(text("CREATE TABLE beta (value TEXT)"))
        connection.execute(text("INSERT INTO beta VALUES ('old-beta')"))
    return engine


# --- Job lifecycle -----------------------------------------------------------


def test_start_records_queued_then_completes(job_service):
    queued = job_service.start("update_database")

    assert queued["status"] == JOB_STATUS_QUEUED
    assert queued["operation"] == "update_database"
    assert queued["job_id"]
    assert queued["created_at"]

    completed = job_service.get(queued["job_id"])
    assert completed["status"] == JOB_STATUS_SUCCEEDED
    assert completed["progress"] == 1.0
    assert completed["progress_note"] == "Completed"
    assert completed["started_at"] is not None
    assert completed["finished_at"] is not None
    assert completed["failure_summary"] is None


def test_duplicate_active_operation_is_rejected(job_service, job_engine):
    from app.models.job import DataRefreshJob

    with Session(job_engine) as session:
        session.add(
            DataRefreshJob(
                job_id="active-job",
                operation="update_database",
                status="running",
                created_at=_fixed_clock(),
            )
        )
        session.commit()

    with pytest.raises(DuplicateOperationError) as raised:
        job_service.start("update_database")

    assert raised.value.status_code == 409
    assert raised.value.code == "duplicate_active_operation"


def test_provider_failure_records_safe_public_summary(job_service):
    def fail_refresh():
        raise ProviderUnavailableError(detail="stats.nba.com token=super-secret")

    failing_service = DataRefreshJobService(
        engine=job_service._engine,
        executor=SynchronousExecutor(),
        clock=_fixed_clock,
        handlers={"update_database": adapt_zero_arg_handler(fail_refresh)},
    )
    queued = failing_service.start("update_database")

    final = job_service.get(queued["job_id"])
    assert final["status"] == JOB_STATUS_FAILED
    assert final["finished_at"] is not None
    assert (
        final["failure_summary"]
        == ProviderUnavailableError().public_message
    )
    assert "super-secret" not in final["failure_summary"]


def test_unexpected_failure_collapses_to_stable_summary(job_service):
    def fail_refresh():
        raise RuntimeError("provider-secret-traceback")

    failing_service = DataRefreshJobService(
        engine=job_service._engine,
        executor=SynchronousExecutor(),
        clock=_fixed_clock,
        handlers={"update_database": adapt_zero_arg_handler(fail_refresh)},
    )
    queued = failing_service.start("update_database")

    final = job_service.get(queued["job_id"])
    assert final["status"] == JOB_STATUS_FAILED
    assert final["failure_summary"] == DEFAULT_FAILURE_SUMMARY
    assert "provider-secret" not in final["failure_summary"]
    assert "Traceback" not in final["failure_summary"]


def test_get_unknown_job_raises_not_found(job_service):
    from app.errors import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError):
        job_service.get("missing-job")


class _ManualExecutor:
    """Executor seam that records work without running it."""

    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))


def test_restart_recovers_expired_lease_but_not_healthy_running_job(job_engine):
    now = [_fixed_clock()]
    first_executor = _ManualExecutor()
    first = DataRefreshJobService(
        job_engine,
        executor=first_executor,
        handlers={"update_database": adapt_zero_arg_handler(lambda: True)},
        clock=lambda: now[0],
        dispatch_on_startup=False,
        start_poller=False,
    )
    queued = first.start("update_database")
    assert first.get(queued["job_id"])["status"] == "running"
    assert len(first_executor.calls) == 1

    second = DataRefreshJobService(
        job_engine,
        executor=SynchronousExecutor(),
        handlers={"update_database": adapt_zero_arg_handler(lambda: True)},
        clock=lambda: now[0],
        dispatch_on_startup=False,
        start_poller=False,
    )
    assert second.dispatch_once() == 0

    now[0] += timedelta(seconds=61)
    assert second.dispatch_once() == 1
    assert second.get(queued["job_id"])["status"] == JOB_STATUS_SUCCEEDED
    first.shutdown()
    second.shutdown()


def test_dispatch_claim_is_atomic_across_two_workers(job_engine):
    executor = _ManualExecutor()
    seed = DataRefreshJobService(
        job_engine,
        executor=executor,
        handlers={"update_database": adapt_zero_arg_handler(lambda: True)},
        dispatch_on_startup=False,
        start_poller=False,
    )
    queued = seed.start("update_database")
    # The first service has already claimed the job.  Release it back to the
    # queue so two fresh coordinators race over the same row.
    with job_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE data_refresh_jobs SET status='queued', "
                "lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL "
                "WHERE job_id=:job_id"
            ),
            {"job_id": queued["job_id"]},
        )

    barrier = Barrier(2)
    claimed = []
    services = [
        DataRefreshJobService(
            job_engine,
            executor=_ManualExecutor(),
            handlers={"update_database": adapt_zero_arg_handler(lambda: True)},
            dispatch_on_startup=False,
            start_poller=False,
        )
        for _ in range(2)
    ]

    def dispatch(service):
        barrier.wait()
        claimed.append(service.dispatch_once())

    threads = [Thread(target=dispatch, args=(service,)) for service in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(claimed) == [0, 1]
    seed.shutdown()
    for service in services:
        service.shutdown()


def test_worker_request_id_and_progress_are_isolated(job_engine):
    observed = []

    def refresh(progress_callback):
        observed.append(telemetry.current_request_id())
        progress_callback(0.25, "Fetched")
        progress_callback(0.75, "Transformed")
        observed.append(telemetry.current_request_id())
        return True

    service = DataRefreshJobService(
        job_engine,
        executor=SynchronousExecutor(),
        handlers={"update_database": refresh},
        clock=_fixed_clock,
        dispatch_on_startup=False,
        start_poller=False,
    )
    queued = service.start("update_database", request_id="job-request-42")
    final = service.get(queued["job_id"])

    assert observed == ["job-request-42", "job-request-42"]
    assert final["status"] == JOB_STATUS_SUCCEEDED
    assert final["progress"] == 1.0
    assert final["progress_note"] == "Completed"
    assert telemetry.current_request_id() == "-"
    service.shutdown()


# --- Atomic publication -------------------------------------------------


def test_publisher_rejects_invalid_identifiers(publisher_engine):
    publisher = AtomicTablePublisher(publisher_engine)

    with pytest.raises(TablePublicationError):
        publisher.publish({"bad name!": pd.DataFrame({"value": ["x"]})})


def test_publisher_swaps_related_tables_together(publisher_engine):
    publisher = AtomicTablePublisher(publisher_engine)

    publisher.publish(
        {
            "alpha": pd.DataFrame({"value": ["new-alpha"]}),
            "beta": pd.DataFrame({"value": ["new-beta"]}),
        }
    )

    with publisher_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT value FROM alpha")).scalar()
            == "new-alpha"
        )
        assert (
            connection.execute(text("SELECT value FROM beta")).scalar()
            == "new-beta"
        )
    staging_tables = [
        name
        for name in inspect(publisher_engine).get_table_names()
        if name.startswith("__staging_")
    ]
    assert staging_tables == []


def test_publisher_rolls_back_all_tables_on_swap_failure(
    publisher_engine, monkeypatch
):
    publisher = AtomicTablePublisher(publisher_engine)
    original_swap = publisher._swap
    swaps = 0

    def fail_second_swap(connection, staging_name, target_name):
        nonlocal swaps
        swaps += 1
        if swaps == 1:
            # The first live table has already been dropped and replaced when
            # the second swap fails; the transaction must restore it too.
            return original_swap(connection, staging_name, target_name)
        raise RuntimeError("swap failed")

    monkeypatch.setattr(publisher, "_swap", fail_second_swap)

    with pytest.raises(RuntimeError):
        publisher.publish(
            {
                "alpha": pd.DataFrame({"value": ["new-alpha"]}),
                "beta": pd.DataFrame({"value": ["new-beta"]}),
            }
        )

    assert swaps == 2

    with publisher_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT value FROM alpha")).scalar()
            == "old-alpha"
        )
        assert (
            connection.execute(text("SELECT value FROM beta")).scalar()
            == "old-beta"
        )
    staging_tables = [
        name
        for name in inspect(publisher_engine).get_table_names()
        if name.startswith("__staging_")
    ]
    assert staging_tables == []


# --- DataService publication contract ----------------------------------


def test_data_service_failure_before_publication_preserves_tables(
    job_engine, monkeypatch
):
    from app.services.data_service import DataService

    with job_engine.begin() as connection:
        connection.execute(text("CREATE TABLE player_information (id INTEGER)"))
        connection.execute(text("INSERT INTO player_information VALUES (1)"))

    service = DataService(job_engine)
    monkeypatch.setattr(
        service,
        "_collect_player_information",
        lambda: pd.DataFrame({"id": [99]}),
    )

    def fail_opponent():
        raise ProviderUnavailableError(detail="opponent provider token=secret")

    monkeypatch.setattr(service, "_fetch_opponent_data", fail_opponent)

    with pytest.raises(ProviderUnavailableError):
        service.update_all_data()

    with job_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT id FROM player_information")).scalar()
            == 1
        )


def test_data_service_publishes_related_frames_together(job_engine, monkeypatch):
    from app.services.data_service import DataService

    service = DataService(job_engine)
    monkeypatch.setattr(
        service,
        "_collect_all_frames",
        lambda: {
            "alpha": pd.DataFrame({"value": ["a"]}),
            "beta": pd.DataFrame({"value": ["b"]}),
        },
    )

    assert service.update_all_data() is True

    with job_engine.connect() as connection:
        assert connection.execute(text("SELECT value FROM alpha")).scalar() == "a"
        assert connection.execute(text("SELECT value FROM beta")).scalar() == "b"


# --- HTTP routes with a real temporary application database -------------


@pytest.fixture
def job_app(tmp_path, monkeypatch):
    from app import create_app
    from app.routes import data_update_routes, player_routes
    from app.services.job_service import (
        DataRefreshJobService,
        SynchronousExecutor,
    )
    from app.utils.db import get_engine

    database_url = f"sqlite:///{tmp_path / 'application.sqlite3'}"
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")

    app = create_app(
        {
            "DATABASE_URL": database_url,
            "TESTING": True,
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": False,
        }
    )

    engine = get_engine(app.extensions["runtime_settings"])
    sync_job_service = DataRefreshJobService(
        engine,
        executor=SynchronousExecutor(),
        clock=_fixed_clock,
        handlers={
            "update_database": lambda *, progress_callback: data_update_routes.data_service.update_all_data(
                progress_callback=progress_callback
            ),
            "player_pbp": lambda *, progress_callback: data_update_routes.data_service.fetch_PBP_data(
                "player", progress_callback=progress_callback
            ),
            "opponent_pbp": lambda *, progress_callback: data_update_routes.data_service.fetch_PBP_data(
                "opponent", progress_callback=progress_callback
            ),
            "fetch_players_with_teams": lambda *, progress_callback: data_update_routes.data_service.fetch_players_with_teams(
                progress_callback=progress_callback
            ),
            "fetch_players": lambda *, progress_callback: player_routes.player_service.store_player_information(
                progress_callback=progress_callback
            ),
        },
    )
    monkeypatch.setattr(
        data_update_routes, "data_jobs_service", sync_job_service
    )
    monkeypatch.setattr(player_routes, "player_jobs_service", sync_job_service)
    return app


def _set_token_verifier(monkeypatch, claims: dict) -> None:
    import app.utils.auth as auth

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {"uid": token, "email": f"{token}@example.com", **claims},
    )
    monkeypatch.setattr(
        auth.UserService,
        "create_or_update_user",
        lambda self, user_data: None,
    )


def _admin_headers(monkeypatch) -> dict[str, str]:
    _set_token_verifier(
        monkeypatch,
        {"admin": True, "role": "admin", "roles": ["admin"]},
    )
    return {"Authorization": "Bearer admin-token"}


def _user_headers(monkeypatch) -> dict[str, str]:
    _set_token_verifier(monkeypatch, {"role": "user"})
    return {"Authorization": "Bearer user-token"}


def test_job_routes_require_admin(job_app, monkeypatch):
    client = job_app.test_client()

    _set_token_verifier(monkeypatch, {})
    assert client.post("/api/data/update_database").status_code == 401
    assert client.get("/api/data/jobs/some-job").status_code == 401

    assert (
        client.post(
            "/api/data/update_database", headers=_user_headers(monkeypatch)
        ).status_code
        == 403
    )


def test_start_returns_202_and_status_is_visible(job_app, monkeypatch):
    from app.routes import data_update_routes

    client = job_app.test_client()
    headers = _admin_headers(monkeypatch)
    monkeypatch.setattr(
        data_update_routes.data_service,
        "update_all_data",
        lambda **kwargs: True,
    )

    response = client.post("/api/data/update_database", headers=headers)

    assert response.status_code == 202
    body = response.get_json()
    assert body["status"] == "queued"
    assert body["operation"] == "update_database"
    job_id = body["job_id"]

    status = client.get(f"/api/data/jobs/{job_id}", headers=headers)
    assert status.status_code == 200
    observed = status.get_json()
    assert observed["status"] == "succeeded"
    assert observed["progress"] == 1.0
    assert observed["failure_summary"] is None


def test_duplicate_active_operation_returns_409(job_app, monkeypatch):
    from app.config.settings import get_runtime_settings
    from app.models.job import DataRefreshJob
    from app.routes import data_update_routes
    from app.utils.db import get_engine

    client = job_app.test_client()
    headers = _admin_headers(monkeypatch)
    monkeypatch.setattr(
        data_update_routes.data_service, "update_all_data", lambda: True
    )

    engine = get_engine(get_runtime_settings())
    with Session(engine) as session:
        session.add(
            DataRefreshJob(
                job_id="active-job",
                operation="update_database",
                status="running",
                created_at=_fixed_clock(),
            )
        )
        session.commit()

    response = client.post("/api/data/update_database", headers=headers)

    assert response.status_code == 409
    assert response.get_json()["error"] == {
        "code": "duplicate_active_operation",
        "message": "An identical operation is already running or queued.",
    }


def test_provider_failure_job_reports_sanitized_summary(job_app, monkeypatch):
    from app.routes import data_update_routes

    client = job_app.test_client()
    headers = _admin_headers(monkeypatch)

    def fail_refresh():
        raise RuntimeError("stats.nba.com token=super-secret")

    monkeypatch.setattr(data_update_routes.data_service, "update_all_data", fail_refresh)

    response = client.post("/api/data/update_database", headers=headers)
    assert response.status_code == 202
    job_id = response.get_json()["job_id"]

    status = client.get(f"/api/data/jobs/{job_id}", headers=headers)
    assert status.status_code == 200
    observed = status.get_json()
    assert observed["status"] == "failed"
    assert observed["failure_summary"] == DEFAULT_FAILURE_SUMMARY
    assert "super-secret" not in observed["failure_summary"]
    assert "Traceback" not in observed["failure_summary"]


def test_get_unknown_job_returns_404(job_app, monkeypatch):
    client = job_app.test_client()

    response = client.get(
        "/api/data/jobs/unknown-job", headers=_admin_headers(monkeypatch)
    )

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "resource_not_found"
