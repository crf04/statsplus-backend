"""Every pointer move retains the activation it made, in its own transaction."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select

from app.migrations import run_migrations
from app.models.collection_control import PublicationVersion
from app.services.collection_control import PublicationService
from tests.services.test_matchup_selection_service import _log_row
from tests.support.pointer_history import pointer_history

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


def test_pruning_keeps_the_publication_each_season_retains(lifecycle):
    engine, publications = lifecycle
    first = _compose(publications, "2025-26")
    second = _compose(publications, "2026-27", expected_fence=first.fence)
    third = _compose(
        publications, "2026-27", points=11, expected_fence=second.fence
    )
    fourth = _compose(
        publications, "2026-27", points=12, expected_fence=third.fence
    )

    publications.prune_history(stream_key=STREAM)

    with engine.connect() as connection:
        surviving = {
            row.publication_id
            for row in connection.execute(select(PublicationVersion))
        }
    assert surviving == {
        first.publication_id, third.publication_id, fourth.publication_id
    }
    assert pointer_history(engine, STREAM) == [
        (first.publication_id, "2025-26", 1, False),
        (third.publication_id, "2026-27", 3, False),
        (fourth.publication_id, "2026-27", 4, False),
    ]
