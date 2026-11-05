"""A selected Slate captures its games' Matchup streams by label alone (crf04/statsplus-backend#695).

The Slate prints the Published Season and each read's season and status, so a
selection's capture reads every label a Matchup's capture reads, with every
check before the payload's (a candidate's ledger lineage included), and never
selects a payload.  Every test reads through the real reader of
``tests/support/selected_season``.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import event

from app.models.collection_control import CollectionObservation
from app.services.database_first_activation import SeasonUnavailable
from app.services.matchup_reuse import PublicationSnapshotReuse
from app.services.matchup_snapshot import capture_matchup_labels, capture_matchup_snapshot
from tests.support.season_cutover import NEW, NEW_GAMES, OLD
from tests.support.selected_season import SelectedWorld, settings


@pytest.fixture
def world(tmp_path):
    built = SelectedWorld(tmp_path)
    yield built
    built.engine.dispose()


def _labels(snapshot):
    return {
        key: (
            read.status,
            read.season,
            read.publication_id,
            read.version,
            read.fence,
            read.unavailable_reason,
        )
        for key, read in snapshot.reads.items()
    }


def _payload_statements(engine, capture):
    statements = []

    def record(_connection, _cursor, statement, *_args):
        if "publication_versions.payload" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        return capture(), statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_a_selected_slate_capture_reads_a_matchup_captures_labels_without_a_payload(world):
    world.hold_opening_week()
    selected = world.reader.for_season(NEW)
    (season, full), full_payloads = _payload_statements(
        world.engine, lambda: capture_matchup_snapshot(settings(), selected)
    )

    (labelled_season, labelled), payloads = _payload_statements(
        world.engine, lambda: capture_matchup_labels(settings(), selected)
    )

    assert full_payloads
    assert payloads == []
    assert (labelled_season, labelled.published_season, labelled.team_games) == (
        season,
        full.published_season,
        full.team_games,
    )
    assert _labels(labelled) == _labels(full)
    assert {read.status for read in labelled.reads.values()} >= {"candidate", "prior_season"}
    assert [read.payload for read in labelled.reads.values() if read.payload is not None] == []


def test_a_selected_slate_capture_still_judges_candidate_lineage(world):
    world.hold_ten_games()
    selected = world.reader.for_season(NEW)
    assert capture_matchup_labels(settings(), selected)[0] == NEW
    with world.engine.begin() as connection:
        connection.execute(
            CollectionObservation.__table__.update()
            .where(CollectionObservation.observation_id == f"pbp:{NEW_GAMES[0][0]}")
            .values(scope=json.dumps({"game_id": NEW_GAMES[1][0], "surface": "canonical_game_ledger"}))
        )

    with pytest.raises(SeasonUnavailable):
        capture_matchup_labels(settings(), selected)


def test_an_unselected_slate_capture_is_the_shared_full_capture(world):
    reuse = PublicationSnapshotReuse()
    captured = capture_matchup_snapshot(settings(), world.reader, reuse)

    assert capture_matchup_labels(settings(), world.reader, reuse) is captured
    assert captured[0] == OLD
