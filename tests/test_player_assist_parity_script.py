"""Offline contract for the player_assist_locations operator parity script."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.migrations import run_migrations
from app.models.collection_control import PublicationVersion
from app.models.player_diet import PlayerDietFactRow
from app.services.player_diet import PlayerDietFact
from scripts.player_assist_parity import (
    STREAM_KEY,
    compare_assist_diet,
    main,
)


NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)


def _candidate(player_id, slice_key, share, volume, games_played):
    return PlayerDietFact(
        player_id, "assist_locations", slice_key, share, volume, games_played,
        "assists", "pbp_stats",
    )


def _legacy(player_id, slice_key, share, volume, games_played):
    return {
        "player_id": player_id, "slice_key": slice_key, "share": share,
        "volume": volume, "games_played": games_played,
    }


def test_matching_rows_are_reported_exact():
    report = compare_assist_diet(
        [_candidate(1, "Arc3Assists", 0.5, 2.0, 10)],
        [_legacy(1, "Arc3Assists", 0.5, 2.0, 10)],
    )

    assert report.all_exact
    assert (report.exact_count, report.differs_count) == (1, 0)
    assert report.identities[0].status == "exact"


def test_share_within_tolerance_is_still_exact():
    report = compare_assist_diet(
        [_candidate(1, "Arc3Assists", 0.500_0001, 2.0, 10)],
        [_legacy(1, "Arc3Assists", 0.5, 2.0, 10)],
        tolerance=1e-6,
    )

    assert report.all_exact


def test_a_differing_volume_is_reported_as_differs_with_both_values():
    report = compare_assist_diet(
        [_candidate(1, "Arc3Assists", 0.5, 2.0, 10)],
        [_legacy(1, "Arc3Assists", 0.5, 3.0, 10)],
    )

    assert not report.all_exact
    assert report.differs_count == 1
    identity = report.identities[0]
    assert identity.status == "differs"
    assert identity.candidate.volume == 2.0
    assert identity.legacy.volume == 3.0


def test_a_differing_share_beyond_tolerance_is_reported_as_differs():
    report = compare_assist_diet(
        [_candidate(1, "Arc3Assists", 0.51, 2.0, 10)],
        [_legacy(1, "Arc3Assists", 0.5, 2.0, 10)],
        tolerance=1e-6,
    )

    assert report.differs_count == 1


def test_a_differing_games_played_is_reported_as_differs():
    report = compare_assist_diet(
        [_candidate(1, "Arc3Assists", 0.5, 2.0, 10)],
        [_legacy(1, "Arc3Assists", 0.5, 2.0, 11)],
    )

    assert report.differs_count == 1


def test_candidate_only_and_legacy_only_identities_are_named():
    report = compare_assist_diet(
        [_candidate(1, "Arc3Assists", 0.5, 2.0, 10)],
        [_legacy(2, "Corner3Assists", 0.5, 2.0, 10)],
    )

    statuses = {(identity.player_id, identity.slice_key): identity.status for identity in report.identities}
    assert statuses[(1, "Arc3Assists")] == "candidate_only"
    assert statuses[(2, "Corner3Assists")] == "legacy_only"
    assert report.candidate_only_count == 1
    assert report.legacy_only_count == 1
    assert not report.all_exact


def test_empty_inputs_are_exact_and_empty():
    report = compare_assist_diet([], [])

    assert report.all_exact
    assert report.identities == ()


def _seed_engine(tmp_path, *, candidate_rows, legacy_rows, season="2025-26", status="candidate"):
    engine = create_engine(f"sqlite:///{tmp_path / 'parity.sqlite3'}")
    run_migrations(engine)
    payload = json.dumps({"base": "assist_locations", "rows": candidate_rows})
    with Session(engine) as session, session.begin():
        session.add(PublicationVersion(
            publication_id="candidate-1",
            stream_key=STREAM_KEY,
            season=season,
            cutoff=NOW,
            version=1,
            status=status,
            checksum="checksum",
            payload=payload,
            created_at=NOW,
            fence=0,
        ))
        for row in legacy_rows:
            session.add(PlayerDietFactRow(
                season=season, base="assist_locations", retrieved_at=NOW,
                volume_unit="assists", provider="pbp_stats", **row,
            ))
    return engine


def _row(player_id, slice_key, share, volume, games_played, *, provider="pbp_stats", volume_unit="assists"):
    return {
        "player_id": player_id, "slice_key": slice_key, "share": share,
        "volume": volume, "games_played": games_played,
        "volume_unit": volume_unit, "provider": provider,
    }


def test_main_reports_exact_and_exits_zero_when_every_identity_matches(tmp_path, capsys):
    engine = _seed_engine(
        tmp_path,
        candidate_rows=[_row(1, "Arc3Assists", 0.5, 2.0, 10)],
        legacy_rows=[{"player_id": 1, "slice_key": "Arc3Assists", "share": 0.5, "volume": 2.0, "games_played": 10}],
    )

    exit_code = main([
        "--database-url", str(engine.url),
        "--season", "2025-26",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"] == {
        "exact": 1, "differs": 0, "candidate_only": 0, "legacy_only": 0, "compared": 1,
    }
    assert payload["non_exact"] == []
    assert len(payload["rows"]) == 1


def test_main_reports_differs_and_exits_one_when_a_row_disagrees(tmp_path, capsys):
    engine = _seed_engine(
        tmp_path,
        candidate_rows=[_row(1, "Arc3Assists", 0.5, 2.0, 10)],
        legacy_rows=[{"player_id": 1, "slice_key": "Arc3Assists", "share": 0.5, "volume": 3.0, "games_played": 10}],
    )

    exit_code = main([
        "--database-url", str(engine.url),
        "--season", "2025-26",
    ])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["differs"] == 1
    assert len(payload["non_exact"]) == 1


def test_main_restricts_the_report_to_requested_players(tmp_path, capsys):
    engine = _seed_engine(
        tmp_path,
        candidate_rows=[
            _row(1, "Arc3Assists", 0.5, 2.0, 10),
            _row(2, "Corner3Assists", 0.4, 1.0, 8),
        ],
        legacy_rows=[
            {"player_id": 1, "slice_key": "Arc3Assists", "share": 0.5, "volume": 2.0, "games_played": 10},
            {"player_id": 2, "slice_key": "Corner3Assists", "share": 0.9, "volume": 9.0, "games_played": 1},
        ],
    )

    exit_code = main([
        "--database-url", str(engine.url),
        "--season", "2025-26",
        "--players", "1",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["compared"] == 1
    assert all(row["player_id"] == 1 for row in payload["rows"])


def test_main_uses_the_latest_candidate_when_publication_id_is_omitted(tmp_path, capsys):
    # F8: two candidates exist for the season with distinguishable payloads;
    # the higher ``version`` must be the one compared, not the older one
    # (whose share/volume/games_played disagree with the legacy rows below).
    engine = _seed_engine(
        tmp_path,
        candidate_rows=[_row(1, "Arc3Assists", 0.9, 9.0, 1)],
        legacy_rows=[{"player_id": 1, "slice_key": "Arc3Assists", "share": 0.5, "volume": 2.0, "games_played": 10}],
    )
    with Session(engine) as session, session.begin():
        session.add(PublicationVersion(
            publication_id="candidate-2",
            stream_key=STREAM_KEY,
            season="2025-26",
            cutoff=NOW,
            version=2,
            status="candidate",
            checksum="checksum-2",
            payload=json.dumps({
                "base": "assist_locations",
                "rows": [_row(1, "Arc3Assists", 0.5, 2.0, 10)],
            }),
            created_at=NOW,
            fence=0,
        ))

    exit_code = main(["--database-url", str(engine.url), "--season", "2025-26"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate"]["publication_id"] == "candidate-2"
    assert payload["candidate"]["version"] == 2


def test_main_rejects_an_explicit_publication_id_from_a_different_season(tmp_path, capsys):
    engine = _seed_engine(
        tmp_path,
        candidate_rows=[_row(1, "Arc3Assists", 0.5, 2.0, 10)],
        legacy_rows=[{"player_id": 1, "slice_key": "Arc3Assists", "share": 0.5, "volume": 2.0, "games_played": 10}],
        season="2024-25",
    )

    exit_code = main([
        "--database-url", str(engine.url),
        "--season", "2025-26",
        "--publication-id", "candidate-1",
    ])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "candidate-1" in captured.err
    assert "2025-26" in captured.err


@pytest.mark.parametrize("status", ["superseded", "rollback"])
def test_main_rejects_an_explicit_publication_id_that_is_not_promotable(status, tmp_path, capsys):
    # Matches stream and season but is no longer a promotable candidate: a
    # superseded or rolled-back version must not be silently compared as if
    # it were still live.
    engine = create_engine(f"sqlite:///{tmp_path / 'not-promotable.sqlite3'}")
    run_migrations(engine)
    with Session(engine) as session, session.begin():
        session.add(PublicationVersion(
            publication_id="stale-candidate",
            stream_key=STREAM_KEY,
            season="2025-26",
            cutoff=NOW,
            version=1,
            status=status,
            checksum="checksum",
            payload=json.dumps({"base": "assist_locations", "rows": []}),
            created_at=NOW,
            fence=0,
        ))

    exit_code = main([
        "--database-url", str(engine.url),
        "--season", "2025-26",
        "--publication-id", "stale-candidate",
    ])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "stale-candidate" in captured.err
    assert "2025-26" in captured.err


def test_main_rejects_an_explicit_publication_id_for_another_stream(tmp_path, capsys):
    engine = create_engine(f"sqlite:///{tmp_path / 'wrong-stream.sqlite3'}")
    run_migrations(engine)
    with Session(engine) as session, session.begin():
        session.add(PublicationVersion(
            publication_id="other-stream-candidate",
            stream_key="player_per36",
            season="2025-26",
            cutoff=NOW,
            version=1,
            status="candidate",
            checksum="checksum",
            payload="{}",
            created_at=NOW,
            fence=0,
        ))

    exit_code = main([
        "--database-url", str(engine.url),
        "--season", "2025-26",
        "--publication-id", "other-stream-candidate",
    ])

    assert exit_code == 2
    assert capsys.readouterr().out == ""


def test_main_accepts_an_explicit_publication_id_bound_to_stream_and_season(tmp_path, capsys):
    engine = _seed_engine(
        tmp_path,
        candidate_rows=[_row(1, "Arc3Assists", 0.5, 2.0, 10)],
        legacy_rows=[{"player_id": 1, "slice_key": "Arc3Assists", "share": 0.5, "volume": 2.0, "games_played": 10}],
    )

    exit_code = main([
        "--database-url", str(engine.url),
        "--season", "2025-26",
        "--publication-id", "candidate-1",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate"] == {
        "publication_id": "candidate-1",
        "status": "candidate",
        "version": 1,
        "cutoff": NOW.isoformat(),
    }


def test_main_reports_no_candidate_found_when_none_exists(tmp_path, capsys):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.sqlite3'}")
    run_migrations(engine)

    exit_code = main(["--database-url", str(engine.url), "--season", "2025-26"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "no_candidate_found"}


def test_main_never_prints_the_database_url(tmp_path, capsys):
    engine = _seed_engine(
        tmp_path,
        candidate_rows=[_row(1, "Arc3Assists", 0.5, 2.0, 10)],
        legacy_rows=[{"player_id": 1, "slice_key": "Arc3Assists", "share": 0.5, "volume": 2.0, "games_played": 10}],
    )
    database_url = str(engine.url)

    main(["--database-url", database_url, "--season", "2025-26"])

    captured = capsys.readouterr()
    assert database_url not in captured.out
    assert database_url not in captured.err
