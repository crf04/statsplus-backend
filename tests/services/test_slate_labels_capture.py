"""A selected Slate captures its games' Matchup streams by label alone (crf04/statsplus-backend#695).

The Slate prints the Published Season and each read's season and status, so a
selection's capture reads every label a Matchup's capture reads, with every
check before the payload's (a candidate's ledger lineage included), and never
selects a payload.  Every test reads through the real reader of
``tests/support/selected_season``.
"""

from __future__ import annotations

import hashlib
import json
from copy import copy

import pytest
from sqlalchemy import event, text

from app.models.collection_control import CollectionObservation
from app.services.database_first_activation import SeasonUnavailable
from app.services.matchup_reuse import PublicationSnapshotReuse
from app.services.matchup_snapshot import capture_matchup_labels, capture_matchup_snapshot
from app.services.research_season import season_scoped
from tests.support.season_cutover import L15_STREAM, NEW, NEW_GAMES, OLD
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
    """``capture()`` and the parameters of each statement that returned a Publication's payload."""

    statements = []

    def record(_connection, cursor, statement, parameters, *_args):
        returned = (column[0] for column in cursor.description or ())
        if "publication_versions" in statement and any("payload" in column for column in returned):
            statements.append(parameters)

    event.listen(engine, "after_cursor_execute", record)
    try:
        return capture(), statements
    finally:
        event.remove(engine, "after_cursor_execute", record)


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


def test_a_fresh_reader_reads_only_its_held_candidates_payloads_once_to_judge_them(world):
    logs, defense = world.hold_opening_week()
    selected = world.reader.for_season(NEW)

    _first, judged = _payload_statements(world.engine, lambda: capture_matchup_labels(settings(), selected))
    _again, payloads = _payload_statements(world.engine, lambda: capture_matchup_labels(settings(), selected))

    assert sorted(parameters[0] for parameters in judged) == sorted(
        [logs.publication_id, defense.publication_id]
    )
    assert payloads == []


SLATE_DATE = "2026-10-26"


def _corrupt_checksum(connection, publication_id):
    connection.execute(
        text("UPDATE publication_versions SET checksum = 'corrupt' WHERE publication_id = :p"),
        {"p": publication_id},
    )


def _corrupt_payload(connection, publication_id):
    connection.execute(
        text("UPDATE publication_versions SET payload = payload || ' ' WHERE publication_id = :p"),
        {"p": publication_id},
    )


def _selected_slate(tmp_path, edit, capture):
    """A fresh world's selected Slate, its live Last 15 edited, its games' Matchup streams captured by ``capture``."""

    tmp_path.mkdir()
    world = SelectedWorld(tmp_path)
    try:
        world.hold_opening_week()
        with world.engine.begin() as connection:
            edit(connection, world.live(L15_STREAM))
        slates = copy(world.slates)
        slates.matchup_snapshot = lambda reader: capture(settings(), reader)
        return season_scoped(slates, NEW).get_slate(SLATE_DATE)
    finally:
        world.engine.dispose()


def _undecodable_payload(connection, publication_id):
    """A payload whose checksum holds and whose decoder refuses it: a team window with no rows."""

    payload = json.dumps({"rows": []})
    connection.execute(
        text(
            "UPDATE publication_versions SET payload = :payload, checksum = :checksum WHERE publication_id = :p"
        ),
        {"payload": payload, "checksum": hashlib.sha256(payload.encode()).hexdigest(), "p": publication_id},
    )


@pytest.mark.parametrize("edit", [_corrupt_checksum, _corrupt_payload])
def test_a_selected_slate_leaves_a_corrupt_publication_out_of_trailing_as_a_full_capture_does(tmp_path, edit):
    full = _selected_slate(tmp_path / "full", edit, capture_matchup_snapshot)

    labelled = _selected_slate(tmp_path / "labels", edit, capture_matchup_labels)

    assert labelled == full
    assert [family["family"] for family in labelled["trailing"]] == ["player_diets"]


def test_a_selected_slate_still_lists_a_family_whose_payload_only_its_decoder_refuses(tmp_path):
    """The one difference from a full capture, left for the owner (crf04/statsplus#408 spot 4).

    Activation runs the same decoders on the same immutable bytes, so only a
    deploy with a stricter decoder can make an active version undecodable.
    """

    full = _selected_slate(tmp_path / "full", _undecodable_payload, capture_matchup_snapshot)

    labelled = _selected_slate(tmp_path / "labels", _undecodable_payload, capture_matchup_labels)

    assert [family["family"] for family in full["trailing"]] == ["player_diets"]
    assert [family["family"] for family in labelled["trailing"]] == ["player_diets", "last_15"]
    assert {**labelled, "trailing": full["trailing"]} == full
