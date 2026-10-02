"""Every pointer move retains the activation it made, in its own transaction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text

from app.migrations import run_migrations
from app.models.collection_control import PublicationVersion
from app.services.collection_control import ControlPlaneError, PublicationService
from app.services.player_game_log_repository import PlayerGameLogRepository
from app.services.research_season import focal_game_rows
from app.services.statistic_catalog import StatisticCatalog
from tests.services.test_matchup_selection_service import _log_row
from tests.support.pointer_history import (
    cite_ledger_source, pointer_history, retirements, revoked_fences,
)

STREAM = "player_game_logs"
CUTOFF = datetime(2026, 1, 15, tzinfo=timezone.utc)


@pytest.fixture
def lifecycle(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'history.sqlite3'}")
    run_migrations(engine)
    publications = PublicationService(engine, clock=lambda: CUTOFF)
    publications.register_stream(
        STREAM,
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
        freshness_rule="cutoff_current",
    )
    return engine, publications


def _compose(publications, season, *, points=10, cutoff=CUTOFF, **kwargs):
    row = {**_log_row(game_id="0022500001", game_date="2026-01-02", minutes=30.0, points=points), "season": season}
    return publications.compose(
        STREAM, season=season, cutoff=cutoff, payload={"rows": [row]}, **kwargs
    )


def test_each_compose_appends_one_history_row_for_the_publication_it_activated(
    lifecycle,
):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26")
    second = _compose(publications, "2026-27", expected_fence=first.fence)

    assert pointer_history(engine, STREAM) == [
        (first.publication_id, "2025-26", 1, False),
        (second.publication_id, "2026-27", 2, False),
    ]


def test_a_cross_season_rollback_moves_the_pointer_without_revoking(lifecycle):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26")
    second = _compose(publications, "2026-27", expected_fence=first.fence)

    restored = publications.rollback(
        STREAM, reason="back to last season", expected_fence=second.fence
    )

    assert pointer_history(engine, STREAM) == [
        (first.publication_id, "2025-26", 1, False),
        (second.publication_id, "2026-27", 2, False),
        (restored.publication_id, "2025-26", 3, False),
    ]
    assert revoked_fences(engine, STREAM) == []


def test_a_same_season_rollback_revokes_the_withdrawn_publication(lifecycle):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=10)
    second = _compose(publications, "2025-26", points=11, expected_fence=first.fence)

    restored = publications.rollback(
        STREAM, reason="reject the refresh", expected_fence=second.fence
    )

    assert pointer_history(engine, STREAM) == [
        (first.publication_id, "2025-26", 1, False),
        (second.publication_id, "2025-26", 2, True),
        (restored.publication_id, "2025-26", 3, False),
    ]
    assert revoked_fences(engine, STREAM) == [3]
    # The withdrawn content cannot serve: a later season publishes, and the
    # past season reads the restored points, not the rejected ones.
    _compose(publications, "2026-27", points=12, expected_fence=restored.fence)
    assert [
        row.points
        for row in focal_game_rows(
            _repository(engine), "2025-26", "0022500001", evidence_season="2026-27"
        )
    ] == [10]


@pytest.mark.parametrize("family", [False, True], ids=("ordinary", "family"))
def test_a_second_same_season_rollback_cannot_restore_rejected_content(
    lifecycle, family,
):
    engine, publications = lifecycle
    good = _compose(publications, "2025-26", points=10)
    _compose(publications, "2025-26", points=99, expected_fence=good.fence)

    def rollback(reason):
        if family:
            return publications.rollback_publication_family((STREAM,), reason=reason)[0]
        return publications.rollback(STREAM, reason=reason)

    restored = rollback("reject the bad refresh")
    with pytest.raises(ControlPlaneError, match="rollback_unavailable"):
        rollback("toggle back to the rejected refresh")

    # Neither the published nor the retained read ever returns the rejected 99.
    assert [
        row.points
        for row in _repository(engine).retained_game_rows("2025-26", "0022500001")
    ] == [10]
    _compose(publications, "2026-27", points=12, expected_fence=restored.fence)
    assert [
        row.points
        for row in focal_game_rows(
            _repository(engine), "2025-26", "0022500001", evidence_season="2026-27"
        )
    ] == [10]


def test_pruning_does_not_pin_versions_named_by_activation_evidence(lifecycle):
    """Activation records (family promotions, explicit activations) outlive payloads."""

    engine, publications = lifecycle
    versions = []
    fence = None
    for points in range(10, 16):
        version = _compose(publications, "2025-26", points=points, expected_fence=fence)
        fence = version.fence
        versions.append(version)
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO publication_activations (activation_id, stream_key, "
                "publication_id, actor, reason, fence, created_at) VALUES "
                "(:id, :stream, :pub, 'operator', 'promote', :fence, "
                "'2026-01-15 00:00:00.000000')"
            ), {"id": f"activation-{points}", "stream": STREAM,
                "pub": version.publication_id, "fence": version.fence})

    result = publications.prune_history(stream_key=STREAM)

    assert _surviving(engine) == {
        versions[-1].publication_id, versions[-2].publication_id
    }
    assert result.deleted == 4
    assert dict(result.kept) == {
        "active": 1, "previous": 1, "candidate": 0, "season_latest": 0,
    }
    with engine.connect() as connection:
        assert connection.scalar(text(
            "SELECT count(*) FROM publication_activations"
        )) == 6


def test_a_restore_retires_its_source_and_a_toggle_retires_the_next(lifecycle):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=25)
    second = _compose(publications, "2026-27", expected_fence=first.fence)

    restored = publications.rollback(STREAM, reason="restore 2025-26")
    assert retirements(engine, STREAM) == [
        (first.publication_id, restored.publication_id)
    ]

    toggled = publications.rollback(STREAM, reason="toggle forward")
    assert retirements(engine, STREAM) == [
        (first.publication_id, restored.publication_id),
        (second.publication_id, toggled.publication_id),
    ]
    # Retired sources neither serve reads nor pin storage.
    publications.prune_history(stream_key=STREAM)
    assert _surviving(engine) == {
        restored.publication_id, toggled.publication_id
    }


def _surviving(engine):
    with engine.connect() as connection:
        return {
            row.publication_id
            for row in connection.execute(select(PublicationVersion))
        }


def test_pruning_keeps_only_the_versions_that_can_still_serve(lifecycle):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=10)
    second = _compose(publications, "2026-27", expected_fence=first.fence)
    restored = publications.rollback(
        STREAM, reason="reject 2026-27", expected_fence=second.fence
    )
    third = _compose(
        publications, "2026-27", points=11, expected_fence=restored.fence
    )

    result = publications.prune_history(stream_key=STREAM)

    # The restore replaced its source (first), and third replaced second.
    assert _surviving(engine) == {restored.publication_id, third.publication_id}
    assert result.deleted == 2
    assert dict(result.kept) == {
        "active": 1, "previous": 1, "candidate": 0,
        "season_latest": 0,
    }
    # Pruned payloads leave their activation record behind.
    assert pointer_history(engine, STREAM) == [
        (first.publication_id, "2025-26", 1, False),
        (second.publication_id, "2026-27", 2, False),
        (restored.publication_id, "2025-26", 3, False),
        (third.publication_id, "2026-27", 4, False),
    ]
    assert [
        row.points
        for row in _repository(engine).retained_game_rows("2025-26", "0022500001")
    ] == [10]


def test_same_season_refreshes_keep_only_the_latest_and_the_rollback_target(
    lifecycle,
):
    engine, publications = lifecycle
    versions = []
    fence = None
    for points in range(10, 15):
        version = _compose(
            publications, "2025-26", points=points, expected_fence=fence
        )
        fence = version.fence
        versions.append(version)

    result = publications.prune_history(stream_key=STREAM)

    assert _surviving(engine) == {
        versions[-1].publication_id, versions[-2].publication_id
    }
    assert result.deleted == 3
    assert result.kept["active"] == 1 and result.kept["previous"] == 1
    # The rollback target still works after pruning.
    publications.rollback(STREAM, reason="reject the latest refresh")
    assert [
        row.points
        for row in _repository(engine).retained_game_rows("2025-26", "0022500001")
    ] == [13]


def test_each_season_keeps_the_version_that_serves_its_past_games(lifecycle):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=10)
    second = _compose(publications, "2025-26", points=11, expected_fence=first.fence)
    third = _compose(publications, "2026-27", points=12, expected_fence=second.fence)
    fourth = _compose(publications, "2026-27", points=13, expected_fence=third.fence)

    result = publications.prune_history(stream_key=STREAM)

    # 2025-26's latest (second), plus 2026-27's active and previous.
    assert _surviving(engine) == {
        second.publication_id, third.publication_id, fourth.publication_id
    }
    assert dict(result.kept) == {
        "active": 1, "previous": 1, "candidate": 0,
        "season_latest": 1,
    }
    assert [
        row.points
        for row in focal_game_rows(
            _repository(engine), "2025-26", "0022500001", evidence_season="2026-27"
        )
    ] == [11]


@pytest.mark.parametrize("withdrawal", ["revoked_at", "retired_at"])
def test_a_withdrawn_season_latest_falls_back_to_the_next_eligible_version(
    lifecycle, withdrawal,
):
    """A latest row that is revoked or retired neither serves nor pins retention."""

    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=10)
    second = _compose(publications, "2025-26", points=11, expected_fence=first.fence)
    third = _compose(publications, "2025-26", points=12, expected_fence=second.fence)
    fourth = _compose(publications, "2026-27", points=13, expected_fence=third.fence)
    fifth = _compose(publications, "2026-27", points=14, expected_fence=fourth.fence)
    with engine.begin() as connection:
        connection.execute(text(
            f"UPDATE publication_pointer_history SET {withdrawal} = :stamp "
            "WHERE publication_id = :id"
        ), {"stamp": "2026-05-01 12:00:00.000000", "id": third.publication_id})

    publications.prune_history(stream_key=STREAM)

    assert _surviving(engine) == {
        second.publication_id, fourth.publication_id, fifth.publication_id
    }
    assert [
        row.points
        for row in focal_game_rows(
            _repository(engine), "2025-26", "0022500001", evidence_season="2026-27"
        )
    ] == [11]


def _insert_candidate(engine, publication_id, *, version):
    stamp = "2026-01-15 00:00:00.000000"
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO publication_versions (publication_id, stream_key, season, "
            "cutoff, version, status, checksum, payload, created_at, fence) "
            "VALUES (:id, :stream, '2025-26', :stamp, :version, 'candidate', 'c', "
            "'{}', :stamp, 0)"
        ), {"id": publication_id, "stream": STREAM, "stamp": stamp, "version": version})


def test_a_candidate_is_kept_until_it_is_replaced(lifecycle):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=10)
    _insert_candidate(engine, "awaiting-activation", version=2)

    result = publications.prune_history(stream_key=STREAM)

    assert _surviving(engine) == {first.publication_id, "awaiting-activation"}
    assert result.deleted == 0 and result.kept["candidate"] == 1

    # Composing a replacement supersedes the candidate, which then goes.
    second = _compose(publications, "2025-26", points=11, expected_fence=first.fence)
    result = publications.prune_history(stream_key=STREAM)
    assert _surviving(engine) == {first.publication_id, second.publication_id}
    assert result.deleted == 1


def _repository(engine):
    return PlayerGameLogRepository(
        engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season="2025-26",
        stats_surface_max_age=timedelta(hours=30),
    )


def test_a_past_season_reads_its_latest_unrevoked_activation(lifecycle):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=10)
    second = _compose(
        publications, "2025-26", points=11, expected_fence=first.fence
    )
    _compose(publications, "2026-27", points=12, expected_fence=second.fence)

    rows = focal_game_rows(
        _repository(engine), "2025-26", "0022500001", evidence_season="2026-27"
    )

    assert [row.points for row in rows] == [11]


def test_a_retained_publication_without_its_projection_stays_unavailable(lifecycle):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26")
    _compose(publications, "2026-27", expected_fence=first.fence)
    with engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM publication_player_game_logs WHERE publication_id = :id"
        ), {"id": first.publication_id})

    assert focal_game_rows(
        _repository(engine), "2025-26", "0022500001", evidence_season="2026-27"
    ) is None


def test_a_game_of_a_later_season_than_the_evidence_is_never_retained():
    class Everything:
        def retained_game_rows(self, season, game_id, *, connection=None):
            return ("rows",)

    assert focal_game_rows(
        Everything(), "2026-27", "0022600001", evidence_season="2025-26"
    ) is None


def test_pruning_then_toggling_keeps_the_publication_that_becomes_authoritative(
    lifecycle,
):
    """Round 5 of #324's review: prune A, then toggle, and the read still works."""

    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=25)
    second = _compose(publications, "2026-27", expected_fence=first.fence)
    restored = publications.rollback(
        STREAM, reason="restore 2025-26", expected_fence=second.fence
    )

    publications.prune_history(stream_key=STREAM)
    assert first.publication_id not in _surviving(engine)
    publications.rollback(STREAM, reason="toggle forward")  # withdraws the restore

    assert restored.publication_id != first.publication_id
    rows = _repository(engine).retained_game_rows("2025-26", "0022500001")
    assert [row.points for row in rows] == [25]
    assert [
        row.points
        for row in focal_game_rows(
            _repository(engine), "2025-26", "0022500001", evidence_season="2026-27"
        )
    ] == [25]


@pytest.mark.parametrize("first", ["prune", "rollback"])
def test_a_concurrent_prune_and_rollback_serialize_on_postgres(first):
    """Pruning and rolling back queue on the pointer lock; neither deadlocks.

    Whichever starts first finishes before the other reads the pointer, so the
    prune never decides from a pointer a rollback is about to move, and the
    rollback never reads a target the prune is deleting.
    """

    import os
    import threading

    from sqlalchemy import event

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration test")
    engine = create_engine(url)

    def reset_schema():
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))

    reset_schema()
    run_migrations(engine)
    try:
        publications = PublicationService(engine, clock=lambda: CUTOFF)
        publications.register_stream(
            STREAM, provider="ledger", owner="railway", required_observations=(),
            publication_strategy="replace", enabled=True,
            freshness_rule="cutoff_current",
        )
        v1 = _compose(publications, "2025-26", points=10)
        v2 = _compose(publications, "2025-26", points=11, expected_fence=v1.fence)
        v3 = _compose(publications, "2025-26", points=12, expected_fence=v2.fence)

        main = threading.current_thread()
        racing = {"thread": None, "outcome": [], "alive_while_locked": None}

        def other():
            service = PublicationService(engine, clock=lambda: CUTOFF)
            try:
                if first == "prune":
                    racing["outcome"].append(
                        service.rollback(STREAM, reason="concurrent rollback")
                    )
                else:
                    racing["outcome"].append(service.prune_history(stream_key=STREAM))
            except Exception as error:  # pragma: no cover - failure detail
                racing["outcome"].append(error)

        @event.listens_for(engine, "after_cursor_execute")
        def interleave(connection, cursor, statement, *_):
            locked_pointer = (
                "FROM publication_pointers" in statement and "FOR UPDATE" in statement
            )
            if (
                locked_pointer
                and threading.current_thread() is main
                and racing["thread"] is None
            ):
                racing["thread"] = threading.Thread(target=other)
                racing["thread"].start()
                racing["thread"].join(timeout=2)
                racing["alive_while_locked"] = racing["thread"].is_alive()

        if first == "prune":
            result = publications.prune_history(stream_key=STREAM)
        else:
            rolled = publications.rollback(STREAM, reason="first rollback")
        racing["thread"].join(timeout=30)
        assert not racing["thread"].is_alive()
        # The second operation waited for the first one's pointer lock.
        assert racing["alive_while_locked"] is True
        assert len(racing["outcome"]) == 1
        outcome = racing["outcome"][0]
        assert not isinstance(outcome, Exception), outcome

        if first == "prune":
            rolled = outcome
            # Prune ran on the pre-rollback pointer: v1 goes, v2 (target) stays.
            assert result.deleted == 1
        else:
            result = outcome
            # Prune saw the post-rollback pointer: only the clone is active, the
            # rejected v3 is no rollback target, and v1 and v2 are not needed.
            assert result.deleted == 3
        surviving = _surviving(engine)
        assert rolled.publication_id in surviving
        assert (v3.publication_id in surviving) == (first == "prune")
        assert [
            row.points
            for row in _repository(engine).retained_game_rows("2025-26", "0022500001")
        ] == [11]
    finally:
        reset_schema()
        engine.dispose()


# --- Migration 058 against a lifecycle rewound to what #324 recorded ---------


def _rewind_to_324(engine, *, withdrawn, restore, revoked_at_of=None):
    """Rewrite the history as #324 wrote it: the withdrawn row revoked at the
    restore's fence and instant, and no row retired."""

    with engine.begin() as connection:
        activated_at, fence = connection.execute(text(
            "SELECT activated_at, fence FROM publication_pointer_history "
            "WHERE publication_id = :id"
        ), {"id": (revoked_at_of or restore).publication_id}).one()
        connection.execute(text(
            "UPDATE publication_pointer_history SET revoked_at = :at, "
            "revoked_fence = :fence WHERE publication_id = :id"
        ), {"at": activated_at, "fence": fence, "id": withdrawn.publication_id})
        connection.execute(text(
            "UPDATE publication_pointer_history SET retired_at = NULL, retired_by = NULL"
        ))


def _reclassify(engine):
    from app.migrations import _reclassify_publication_pointer_history

    with engine.begin() as connection:
        _reclassify_publication_pointer_history(connection)


def _points(engine, season):
    rows = _repository(engine).retained_game_rows(season, "0022500001")
    return None if rows is None else [row.points for row in rows]


def test_migration_lifts_a_cross_season_withdrawal_whose_lineage_is_current(lifecycle):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=25)
    second = _compose(publications, "2026-27", points=26, expected_fence=first.fence)
    restored = publications.rollback(STREAM, reason="restore 2025-26")
    cite_ledger_source(engine, second.publication_id, game_id="g1", observation_id="obs-2")
    _rewind_to_324(engine, withdrawn=second, restore=restored)
    assert _points(engine, "2026-27") is None

    _reclassify(engine)

    assert _points(engine, "2026-27") == [26]
    assert _points(engine, "2025-26") == [25]
    assert retirements(engine, STREAM) == [
        (first.publication_id, restored.publication_id)
    ]


@pytest.mark.parametrize("lineage", ["corrected_later", "unrecorded"])
def test_migration_keeps_a_withdrawal_it_cannot_prove_current(lifecycle, lineage):
    """A later correction could not re-stamp the rollback-revoked row, so the
    migration proves it is not stale before clearing it."""

    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=25)
    second = _compose(publications, "2026-27", points=99, expected_fence=first.fence)
    restored = publications.rollback(STREAM, reason="restore 2025-26")
    if lineage == "corrected_later":
        # The ledger's source for the game moved on after B was published.
        cite_ledger_source(
            engine, second.publication_id, game_id="g1", observation_id="obs-2",
            current=False,
        )
    _rewind_to_324(engine, withdrawn=second, restore=restored)

    _reclassify(engine)

    assert _points(engine, "2026-27") is None
    future = _compose(publications, "2027-28", points=30, expected_fence=restored.fence)
    assert not focal_game_rows(
        _repository(engine), "2026-27", "0022500001", evidence_season=future.season
    )


def test_migration_leaves_a_restore_it_cannot_prove_alone(lifecycle):
    """A restore a later activation superseded looks like an ordinary activation.

    #324 recorded nothing durable that tells it from a correction's revocation,
    so the safe direction is to change nothing.
    """

    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=25)
    second = _compose(publications, "2026-27", points=26, expected_fence=first.fence)
    restored = publications.rollback(STREAM, reason="restore 2025-26")
    _compose(publications, "2027-28", points=27, expected_fence=restored.fence)
    _rewind_to_324(engine, withdrawn=second, restore=restored)
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE publication_versions SET status = 'superseded' "
            "WHERE publication_id = :id"
        ), {"id": restored.publication_id})

    _reclassify(engine)

    assert _points(engine, "2026-27") is None
    assert retirements(engine, STREAM) == []


def test_migration_never_clears_a_correction_revocation_beside_an_identical_activation(
    lifecycle,
):
    """An ordinary activation with the same content and cutoff as an older
    version is not a restore, even when a correction revoked a withdrawn row at
    its fence and instant."""

    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=25)
    second = _compose(publications, "2026-27", points=26, expected_fence=first.fence)
    ordinary = _compose(
        publications, "2025-26", points=25, expected_fence=second.fence
    )
    _rewind_to_324(engine, withdrawn=second, restore=ordinary)

    _reclassify(engine)

    assert _points(engine, "2026-27") is None
    assert retirements(engine, STREAM) == []


def test_migration_leaves_a_withdrawal_whose_payload_was_already_pruned(lifecycle):
    engine, publications = lifecycle
    older = _compose(publications, "2026-27", points=20)
    first = _compose(publications, "2025-26", points=25, expected_fence=older.fence)
    second = _compose(publications, "2026-27", points=26, expected_fence=first.fence)
    restored = publications.rollback(STREAM, reason="restore 2025-26")
    _rewind_to_324(engine, withdrawn=second, restore=restored)
    with engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM publication_player_game_logs WHERE publication_id = :id"
        ), {"id": second.publication_id})
        connection.execute(text(
            "DELETE FROM publication_versions WHERE publication_id = :id"
        ), {"id": second.publication_id})

    _reclassify(engine)

    # Pruned lineage cannot be proven current, so the row stays revoked, and the
    # read falls back to the older 2026-27 version that still exists.
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT revoked_at IS NOT NULL, revoked_fence FROM publication_pointer_history "
            "WHERE publication_id = :id"
        ), {"id": second.publication_id}).one() == (1, 4)
    assert _points(engine, "2026-27") == [20]
    assert _points(engine, "2025-26") == [25]


def test_migration_never_clears_a_correction_revocation_beside_an_ordinary_activation(
    lifecycle,
):
    """An ordinary refresh whose content equals an older version is no restore."""

    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=25)
    second = _compose(publications, "2026-27", points=26, expected_fence=first.fence)
    ordinary = _compose(
        publications, "2025-26", points=25, expected_fence=second.fence,
        cutoff=CUTOFF + timedelta(days=1),
    )
    # A correction revoked 2026-27 at the ordinary activation's fence and instant.
    _rewind_to_324(engine, withdrawn=second, restore=ordinary)

    _reclassify(engine)

    assert _points(engine, "2026-27") is None
    assert retirements(engine, STREAM) == []


def test_migration_needs_a_matching_source_to_call_a_row_a_restore(lifecycle):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=25)
    second = _compose(publications, "2026-27", points=26, expected_fence=first.fence)
    restored = publications.rollback(STREAM, reason="restore 2025-26")
    _rewind_to_324(engine, withdrawn=second, restore=restored)
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE publication_versions SET checksum = 'different' WHERE publication_id = :id"
        ), {"id": first.publication_id})

    _reclassify(engine)

    assert _points(engine, "2026-27") is None
    assert retirements(engine, STREAM) == []


def test_a_pruned_history_row_never_shadows_the_season_latest_that_still_exists(
    lifecycle,
):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=10)
    second = _compose(publications, "2025-26", points=11, expected_fence=first.fence)
    third = _compose(publications, "2025-26", points=12, expected_fence=second.fence)
    fourth = _compose(publications, "2026-27", points=13, expected_fence=third.fence)
    fifth = _compose(publications, "2026-27", points=14, expected_fence=fourth.fence)
    with engine.begin() as connection:  # #324 or an operator removed the payload
        for table in ("publication_player_game_logs", "publication_versions"):
            connection.execute(text(
                f"DELETE FROM {table} WHERE publication_id = :id"
            ), {"id": third.publication_id})

    assert _points(engine, "2025-26") == [11]
    publications.prune_history(stream_key=STREAM)
    assert _surviving(engine) == {
        second.publication_id, fourth.publication_id, fifth.publication_id
    }


@pytest.mark.parametrize("family", [False, True], ids=("ordinary", "family"))
def test_rollback_refuses_a_target_every_activation_of_which_was_revoked(
    lifecycle, family,
):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=10)
    _compose(publications, "2025-26", points=11, expected_fence=first.fence)
    # The rollback target (first) is still the pointer's previous publication,
    # but its only activation was withdrawn.
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE publication_pointer_history SET revoked_at = '2026-05-01 00:00:00.000000', "
            "revoked_fence = 2 WHERE publication_id = :id"
        ), {"id": first.publication_id})

    with pytest.raises(ControlPlaneError, match="rollback_unavailable"):
        if family:
            publications.rollback_publication_family((STREAM,), reason="restore withdrawn")
        else:
            publications.rollback(STREAM, reason="restore withdrawn")

    assert [row.points for row in _repository(engine).retained_game_rows("2025-26", "0022500001")] == [11]


@pytest.mark.parametrize("family", [False, True], ids=("ordinary", "family"))
def test_a_same_season_rollback_leaves_only_the_restored_version_after_pruning(
    lifecycle, family,
):
    engine, publications = lifecycle
    good = _compose(publications, "2025-26", points=10)
    _compose(publications, "2025-26", points=99, expected_fence=good.fence)
    if family:
        restored = publications.rollback_publication_family((STREAM,), reason="reject")[0]
    else:
        restored = publications.rollback(STREAM, reason="reject")

    result = publications.prune_history(stream_key=STREAM)

    # The rejected 99 is no rollback target, and the source is retired.
    assert _surviving(engine) == {restored.publication_id}
    assert result.deleted == 2


def test_a_first_composition_waits_for_a_prune_of_its_pointerless_stream_on_postgres():
    """Prune finds no pointer to lock, so the stream row is what orders it
    against the first composition; the new previous target must survive."""

    import os
    import threading

    from sqlalchemy import event

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration test")
    engine = create_engine(url)

    def reset_schema():
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))

    reset_schema()
    run_migrations(engine)
    try:
        publications = PublicationService(engine, clock=lambda: CUTOFF)
        publications.register_stream(
            STREAM, provider="ledger", owner="railway", required_observations=(),
            publication_strategy="replace", enabled=True,
            freshness_rule="cutoff_current",
        )
        main = threading.current_thread()
        racing = {"thread": None, "blocked": None}

        def compose_twice():
            service = PublicationService(engine, clock=lambda: CUTOFF)
            first = _compose(service, "2025-26", points=10)
            _compose(service, "2025-26", points=11, expected_fence=first.fence)

        @event.listens_for(engine, "after_cursor_execute")
        def interleave(connection, cursor, statement, *_):
            if (
                "FROM publication_pointers" in statement
                and threading.current_thread() is main
                and racing["thread"] is None
            ):
                # The pointer query found no row; the stream now publishes twice.
                racing["thread"] = threading.Thread(target=compose_twice)
                racing["thread"].start()
                racing["thread"].join(timeout=2)
                racing["blocked"] = racing["thread"].is_alive()

        result = publications.prune_history(stream_key=STREAM)

        racing["thread"].join(timeout=30)
        assert not racing["thread"].is_alive()
        assert racing["blocked"] is True  # waited on the stream lock
        assert result.deleted == 0
        restored = publications.rollback(STREAM, reason="previous target survived")
        assert restored.season == "2025-26"
        assert [
            row.points
            for row in _repository(engine).retained_game_rows("2025-26", "0022500001")
        ] == [10]
    finally:
        reset_schema()
        engine.dispose()


@pytest.mark.parametrize("withdrawal", ["revoked_at", "retired_at"])
def test_a_revoked_or_retired_latest_row_never_serves_without_pruning(
    lifecycle, withdrawal,
):
    """The read's own eligibility filters, with the withdrawn payload still stored."""

    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=10)
    second = _compose(publications, "2025-26", points=11, expected_fence=first.fence)
    with engine.begin() as connection:
        connection.execute(text(
            f"UPDATE publication_pointer_history SET {withdrawal} = '2026-05-01 00:00:00.000000' "
            "WHERE publication_id = :id"
        ), {"id": second.publication_id})

    assert _points(engine, "2025-26") == [10]


def test_prune_applies_the_keep_set_to_a_stream_with_no_pointer(lifecycle):
    engine, publications = lifecycle
    publications.register_stream(
        "inactive_ledger", provider="ledger", owner="railway",
        required_observations=(), publication_strategy="replace", enabled=False,
        freshness_rule="cutoff_current",
    )
    stamp = "2026-01-15 00:00:00.000000"
    with engine.begin() as connection:
        for publication_id, status, version in (
            ("old-1", "superseded", 1), ("old-2", "superseded", 2),
            ("awaiting", "candidate", 3),
        ):
            connection.execute(text(
                "INSERT INTO publication_versions (publication_id, stream_key, season, "
                "cutoff, version, status, checksum, payload, created_at, fence) "
                "VALUES (:id, 'inactive_ledger', '2025-26', :stamp, :version, :status, "
                "'c', '{}', :stamp, 0)"
            ), {"id": publication_id, "stamp": stamp, "version": version, "status": status})

    result = publications.prune_history(stream_key="inactive_ledger")

    assert result.deleted == 2
    assert dict(result.kept) == {
        "active": 0, "previous": 0, "candidate": 1, "season_latest": 0,
    }
    assert _surviving(engine) == {"awaiting"}


def test_the_maintenance_command_prints_what_pruning_deleted_and_kept(
    tmp_path, capsys,
):
    from scripts.collection_maintenance import main

    path = tmp_path / "maintenance.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    run_migrations(engine)
    publications = PublicationService(engine, clock=lambda: CUTOFF)
    publications.register_stream(
        STREAM, provider="ledger", owner="railway", required_observations=(),
        publication_strategy="replace", enabled=True,
        freshness_rule="cutoff_current",
    )
    fence = None
    for points in (10, 11, 12, 13):
        fence = _compose(publications, "2025-26", points=points, expected_fence=fence).fence
    engine.dispose()

    assert main([
        "--database-url", f"sqlite:///{path}", "--season", "2025-26",
        "--cutoff", "2026-01-15T00:00:00Z",
    ]) == 0

    printed = capsys.readouterr().out
    assert "'publications_pruned': 2" in printed
    assert (
        "'publications_kept': {'active': 1, 'previous': 1, 'candidate': 0, "
        "'season_latest': 0}" in printed
    )
