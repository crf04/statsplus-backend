"""What each deterministic failure drill actually proves.

The drills already run in the suite: ``test_database_first_activation`` asserts
they are named and pass, and ``test_database_first_drills_script`` covers the
CLI adapter with the runner patched out.  Neither looks at what a drill
*found*, so a drill that stopped exercising its failure and reported success
would not be noticed.

Backend #87 requires the drills to prove retry, status, alert, and recovery
behavior, and that no partial or stale attempt overwrites good data.  These
tests read the per-drill evidence against a marked disposable SQLite database
and pin those specific claims.

The disposable marker is deliberately provisioned by this harness rather than
by the runner: a caller must not be able to make an arbitrary database safe by
passing a flag.  The guard cases here cover the marker states that the
constructor-level check in ``test_database_first_activation`` does not reach.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from app.services.database_first_drills import (
    DISPOSABLE_MARKER_PURPOSE,
    DISPOSABLE_MARKER_TABLE,
    FailureDrillRunner,
    run_failure_drills,
)


NONCE = "drill-harness-nonce"


def _mark_disposable(engine, *, nonce: str = NONCE, purpose: str = DISPOSABLE_MARKER_PURPOSE) -> None:
    with engine.begin() as connection:
        connection.execute(text(
            f"CREATE TABLE {DISPOSABLE_MARKER_TABLE} "
            "(marker_nonce TEXT, purpose TEXT, schema_name TEXT)"
        ))
        connection.execute(
            text(
                f"INSERT INTO {DISPOSABLE_MARKER_TABLE} "
                "(marker_nonce, purpose, schema_name) VALUES (:nonce, :purpose, NULL)"
            ),
            {"nonce": nonce, "purpose": purpose},
        )


def _disposable_database(tmp_path, name="drills.sqlite3", **marker):
    url = f"sqlite:///{tmp_path / name}"
    engine = create_engine(url)
    _mark_disposable(engine, **marker)
    run_migrations(engine)
    return url


def _run(tmp_path, **kwargs):
    url = _disposable_database(tmp_path)
    return run_failure_drills(
        database_url=url,
        environment="unit",
        disposable_marker_nonce=NONCE,
        **kwargs,
    )


def _drill(report, name):
    return next(item for item in report["drills"] if item["name"] == name)


def test_every_named_drill_runs_in_order_against_a_marked_database(tmp_path):
    # The sibling test runs the in-memory default; this one proves the same
    # holds when the runner is pointed at a real marked database URL, and
    # pins the order the report is written in.
    report = _run(tmp_path)

    assert report["status"] == "passed"
    assert tuple(item["name"] for item in report["drills"]) == FailureDrillRunner.NAMES
    assert all(item["status"] == "passed" for item in report["drills"])
    assert report["production_evidence"] is False
    assert report["environment"] == "unit"


def test_every_drill_records_a_recovery_measurement_without_claiming_an_sla(tmp_path):
    report = _run(tmp_path)

    # The artifact schema advertises these as required, so a drill that
    # silently stopped measuring would still read as "passed" without them.
    assert set(report["artifact_schema"]["required_fields"]) == {
        "recovery_time_ms", "recovery_data_point",
    }
    for item in report["drills"]:
        details = item["details"]
        assert isinstance(details["recovery_time_ms"], (int, float))
        assert details["recovery_time_ms"] >= 0
        assert details["recovery_point"]
        # A measured data point is evidence, not a service-level promise.
        assert "sla" not in details
        assert "guarantee" not in details


def test_railway_outage_retries_and_publishes_exactly_once(tmp_path):
    details = _drill(_run(tmp_path), "railway_outage_retry")["details"]

    assert details["retry_statuses"] == ["failed", "succeeded"]
    # The failed attempt must leave nothing behind: exactly one attempt
    # published, and no partial write from the attempt that errored.
    assert details["published_attempts"] == [1]
    assert details["no_partial_publish"] is True
    assert details["verified"] is True


def test_duplicate_delivery_commits_once(tmp_path):
    details = _drill(_run(tmp_path), "duplicate_delivery_idempotency")["details"]

    assert details["deliveries"] == 2
    assert details["committed"] == 1
    assert details["idempotent"] is True
    assert details["receipt_layer"] == "control_plane_observation_ingestion"


def test_collector_reboot_replays_its_outbox_without_duplicating(tmp_path):
    details = _drill(_run(tmp_path), "collector_reboot_outbox_replay")["details"]

    assert details["pending_before_reboot"] == 1
    assert details["replayed_after_reboot"] == 1
    assert details["idempotent"] is True
    assert details["acknowledged"] is True
    assert details["receipt_layer"] == "residential_outbox_sqlite"


def test_expired_credentials_are_rejected_and_write_nothing(tmp_path):
    details = _drill(_run(tmp_path), "expired_credential_rejection")["details"]

    assert details["credential_status"] == "expired"
    assert details["accepted"] is False
    assert details["writes"] == 0


def test_provider_failure_retains_last_good_and_publishes_no_partial(tmp_path):
    details = _drill(_run(tmp_path), "provider_failure_last_good_retention")["details"]

    # The failed attempt produced nothing, and the previously good value is
    # still what gets served.  This is the drill that proves a failed
    # collection never degrades a durable fact.
    assert details["failed_attempt"] == {"value": None}
    assert details["served"] == details["last_good"] == {"value": 7}
    assert details["last_good_retained"] is True
    assert details["partial_attempt_published"] is False


def test_alert_fires_and_then_recovers(tmp_path):
    details = _drill(_run(tmp_path), "alert_recovery")["details"]

    assert details["alert_transitions"] == ["failure", "recovery"]
    assert details["recovery_emitted"] is True


def test_isolated_restore_replays_idempotently_and_validates_its_evidence(tmp_path):
    details = _drill(_run(tmp_path), "isolated_restore_replay")["details"]

    assert details["restored_rows"] == details["replayed_rows"] == 2
    assert details["idempotent"] is True
    for validated in (
        "ledger_validated", "pointers_validated", "audit_validated",
        "provenance_validated", "pbp_repair_validated", "replay_validated",
        "exact_checksums_validated",
    ):
        assert details[validated] is True, validated


def test_drills_fail_closed_without_an_out_of_band_marker_nonce(tmp_path):
    url = f"sqlite:///{tmp_path / 'unmarked.sqlite3'}"
    run_migrations(create_engine(url))

    report = run_failure_drills(database_url=url, environment="unit")

    assert report["status"] == "failed"
    assert all(item["status"] == "failed" for item in report["drills"])
    assert {item["details"]["error"] for item in report["drills"]} == {
        "out_of_band_disposable_marker_nonce_required"
    }


def test_a_nonce_alone_cannot_authorize_an_unmarked_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'unmarked.sqlite3'}"
    run_migrations(create_engine(url))

    report = run_failure_drills(
        database_url=url, environment="unit", disposable_marker_nonce=NONCE,
    )

    assert report["status"] == "failed"
    assert {item["details"]["error"] for item in report["drills"]} == {
        "drill_database_disposable_marker_missing"
    }


@pytest.mark.parametrize(
    "marker,expected",
    [
        ({"nonce": "a-different-nonce"}, "drill_database_disposable_marker_mismatch"),
        ({"purpose": "something-else"}, "drill_database_disposable_marker_mismatch"),
    ],
)
def test_a_marker_that_does_not_match_fails_closed(tmp_path, marker, expected):
    url = _disposable_database(tmp_path, **marker)

    report = run_failure_drills(
        database_url=url, environment="unit", disposable_marker_nonce=NONCE,
    )

    assert report["status"] == "failed"
    assert {item["details"]["error"] for item in report["drills"]} == {expected}


def test_drills_refuse_a_database_that_already_holds_domain_rows(tmp_path):
    url = _disposable_database(tmp_path)
    with create_engine(url).begin() as connection:
        connection.execute(text(
            "INSERT INTO publication_streams "
            "(stream_key, provider, owner, required_observations, "
            " publication_strategy, supported_windows, schema_versions, "
            " completeness_rule, freshness_rule, enabled, created_at) "
            "VALUES ('traditional_opponent_season', 'ledger', 'railway', '[]', "
            "        'replace', '[]', '[1]', 'base_complete', 'cutoff_current', "
            "        0, '2026-01-01T00:00:00+00:00')"
        ))

    report = run_failure_drills(
        database_url=url, environment="unit", disposable_marker_nonce=NONCE,
    )

    assert report["status"] == "failed"
    assert {item["details"]["error"] for item in report["drills"]} == {
        "drill_database_not_empty"
    }
