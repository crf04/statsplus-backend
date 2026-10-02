"""Every pointer move retains the activation it made, in its own transaction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text

from app.migrations import run_migrations
from app.models.collection_control import PublicationVersion
from app.services.collection_control import PublicationService
from app.services.player_game_log_repository import PlayerGameLogRepository
from app.services.research_season import focal_game_rows
from app.services.statistic_catalog import StatisticCatalog
from tests.services.test_matchup_selection_service import _log_row
from tests.support.pointer_history import pointer_history, revoked_fences

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


def _compose(publications, season, *, points=10, **kwargs):
    row = {**_log_row(game_id="0022500001", game_date="2026-01-02", minutes=30.0, points=points), "season": season}
    return publications.compose(
        STREAM, season=season, cutoff=CUTOFF, payload={"rows": [row]}, **kwargs
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


def test_a_rollback_revokes_the_withdrawn_publication_and_restores_the_prior_one(
    lifecycle,
):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26")
    second = _compose(publications, "2026-27", expected_fence=first.fence)

    restored = publications.rollback(
        STREAM, reason="reject the new season", expected_fence=second.fence
    )

    assert pointer_history(engine, STREAM) == [
        (first.publication_id, "2025-26", 1, False),
        (second.publication_id, "2026-27", 2, True),
        (restored.publication_id, "2025-26", 3, False),
    ]
    assert revoked_fences(engine, STREAM) == [3]


def test_pruning_deletes_only_payloads_without_unrevoked_history(lifecycle):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=10)
    second = _compose(publications, "2026-27", expected_fence=first.fence)
    restored = publications.rollback(
        STREAM, reason="reject 2026-27", expected_fence=second.fence
    )
    third = _compose(
        publications, "2026-27", points=11, expected_fence=restored.fence
    )

    publications.prune_history(stream_key=STREAM)

    with engine.connect() as connection:
        surviving = {
            row.publication_id
            for row in connection.execute(select(PublicationVersion))
        }
    # The rolled-back (revoked) 2026-27 payload goes; every publication with an
    # unrevoked history row stays, since a later rollback can make it authority.
    assert surviving == {
        first.publication_id, restored.publication_id, third.publication_id
    }
    # Pruned payloads leave their activation record behind.
    assert pointer_history(engine, STREAM) == [
        (first.publication_id, "2025-26", 1, False),
        (second.publication_id, "2026-27", 2, True),
        (restored.publication_id, "2025-26", 3, False),
        (third.publication_id, "2026-27", 4, False),
    ]
    assert [
        row.points
        for row in _repository(engine).retained_game_rows("2025-26", "0022500001")
    ] == [10]


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
    engine, publications = lifecycle
    first = _compose(publications, "2025-26", points=25)
    second = _compose(publications, "2026-27", expected_fence=first.fence)
    restored = publications.rollback(
        STREAM, reason="restore 2025-26", expected_fence=second.fence
    )
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE publication_versions SET status = 'superseded' "
            "WHERE publication_id = :id"
        ), {"id": first.publication_id})

    publications.prune_history(stream_key=STREAM)
    publications.rollback(STREAM, reason="toggle forward")  # revokes the restore

    assert restored.publication_id != first.publication_id
    rows = _repository(engine).retained_game_rows("2025-26", "0022500001")
    assert [row.points for row in rows] == [25]
