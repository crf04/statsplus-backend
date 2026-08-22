"""Offline contract tests for the Canonical Game Ledger packet."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from app.migrations import run_migrations
from app.models.canonical_game_ledger import (
    CanonicalGameLedgerGame,
    CanonicalGameLedgerPlayerFact,
    CanonicalGameLedgerTeamFact,
    LedgerGameRowEvidence,
    LedgerObservationEvidence,
    LedgerPublication,
)
from app.models.collection_control import (
    CollectionManifest,
    CollectionObservation,
    CompositionJob,
    ReconciliationItem,
)
from app.services.canonical_game_ledger import (
    CanonicalGameLedgerRepository,
    LEDGER_GOVERNED_FULLGAME_FIELDS,
    LedgerValidationError,
    LedgerPublicationRecord,
    LedgerSchemaUnavailable,
    canonical_game_from_pbp,
    canonical_row_checksum,
    raw_rows_from_facts,
    record_schema_drift,
)
from app.services.collection_control import CollectionOperationsService
from app.services.ledger_materialization import LedgerCorrectionQueue
from app.services.ledger_derivations import (
    derive_assist_location_facts,
    derive_player_per36_facts,
)
from app.providers.pbp_game_logs import PBPGameLogAdapter
from app.utils.telemetry import ProviderResponseError


def _event() -> dict[str, object]:
    return {
        "nba_game_id": "0022400001",
        "season": "2024-25",
        "classification": "Regular Season",
        "scheduled_at": "2024-11-15T00:00:00+00:00",
        "home_team_id": 1610612747,
        "home_team_tricode": "LAL",
        "away_team_id": 1610612759,
        "away_team_tricode": "SAS",
        "status_code": 3,
        "status_text": "Final",
    }


def _game():
    payload = json.loads("""{
      "stats": {"Home": {"FullGame": [
        {"EntityId": "0", "Name": "Team", "Minutes": "00:00"},
        {"EntityId": "101", "Name": "Home One", "Minutes": "20:00", "FG2M": 2, "FG2A": 4, "FG3M": 1, "FG3A": 2, "FtPoints": 1, "FTA": 2, "OffRebounds": 1, "DefRebounds": 2, "Assists": 3, "Turnovers": 1, "Steals": 1, "Blocks": 0, "Fouls": 1, "Points": 8},
        {"EntityId": "102", "Name": "Home Two", "Minutes": "20:00", "FG2M": 2, "FG2A": 4, "FG3M": 1, "FG3A": 2, "FtPoints": 1, "FTA": 2, "OffRebounds": 1, "DefRebounds": 2, "Assists": 3, "Turnovers": 1, "Steals": 1, "Blocks": 0, "Fouls": 1, "Points": 8}
      ]}, "Away": {"FullGame": [
        {"EntityId": "0", "Name": "Team", "Minutes": "00:00"},
        {"EntityId": "201", "Name": "Away One", "Minutes": "20:00", "FG2M": 2, "FG2A": 4, "FG3M": 1, "FG3A": 2, "FtPoints": 1, "FTA": 2, "OffRebounds": 1, "DefRebounds": 2, "Assists": 3, "Turnovers": 1, "Steals": 1, "Blocks": 0, "Fouls": 1, "Points": 8},
        {"EntityId": "202", "Name": "Away Two", "Minutes": "20:00", "FG2M": 2, "FG2A": 4, "FG3M": 1, "FG3A": 2, "FtPoints": 1, "FTA": 2, "OffRebounds": 1, "DefRebounds": 2, "Assists": 3, "Turnovers": 1, "Steals": 1, "Blocks": 0, "Fouls": 1, "Points": 8}
      ]}},
      "team_results": {"Home": {"FullGame": {"Points": 16, "FG2M": 4, "FG2A": 8, "FG3M": 2, "FG3A": 4, "FtPoints": 2, "FTA": 4, "OffRebounds": 2, "DefRebounds": 4, "Rebounds": 6, "Assists": 6, "Turnovers": 2, "Steals": 2, "Blocks": 0, "Fouls": 2}}, "Away": {"FullGame": {"Points": 16, "FG2M": 4, "FG2A": 8, "FG3M": 2, "FG3A": 4, "FtPoints": 2, "FTA": 4, "OffRebounds": 2, "DefRebounds": 4, "Rebounds": 6, "Assists": 6, "Turnovers": 2, "Steals": 2, "Blocks": 0, "Fouls": 2}}},
      "home_team_abbreviation": "LAL", "away_team_abbreviation": "SAS", "date": "2024-11-15",
      "participant_ids_by_team": {"1610612747": [101, 102], "1610612759": [201, 202]}
    }""")
    return canonical_game_from_pbp(
        payload,
        event=_event(),
        retrieved_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
    )


def test_repository_requires_versioned_ledger_migration(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'unmigrated.sqlite3'}")
    try:
        CanonicalGameLedgerRepository(engine)
    except LedgerSchemaUnavailable as error:
        assert "migration 032" in str(error)
    else:
        raise AssertionError("unmigrated ledger repository unexpectedly constructed")


def test_complete_game_is_inserted_idempotently_and_correction_replaces_all_facts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()

    first = repository.replace_game(game)
    repeated = repository.replace_game(game)
    corrected = replace(
        game,
        team_facts=(replace(game.team_facts[0], points=17), game.team_facts[1]),
        player_facts=(replace(game.player_facts[0], points=9), *game.player_facts[1:]),
    )
    corrected = replace(corrected, raw_rows=raw_rows_from_facts(corrected)).with_checksum()
    correction = repository.replace_game(corrected)

    assert first.inserted and not first.replaced
    assert not repeated.inserted and not repeated.replaced
    assert correction.replaced and correction.checksum != first.checksum
    stored = repository.get_game(game.game_id)
    assert stored is not None
    assert stored.player_facts[0].points == 9
    assert len(stored.team_facts) == 2


def test_incomplete_participants_fail_before_any_row_is_written(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    incomplete = replace(game, player_facts=game.player_facts[:-1]).with_checksum()

    try:
        repository.replace_game(incomplete)
    except LedgerValidationError as error:
        assert "participant evidence" in str(error)
    else:
        raise AssertionError("incomplete game unexpectedly published")
    assert repository.get_game(game.game_id) is None


def test_failed_batch_keeps_prior_game_and_does_not_leak_staged_correction(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    repository.replace_game(game)
    corrected = replace(
        game,
        team_facts=(replace(game.team_facts[0], points=18), game.team_facts[1]),
        player_facts=(replace(game.player_facts[0], points=10), *game.player_facts[1:]),
    )
    corrected = replace(corrected, raw_rows=raw_rows_from_facts(corrected)).with_checksum()
    bad = replace(game, game_id="0022400002", player_facts=game.player_facts[:-1]).with_checksum()

    try:
        repository.replace_games_atomic((corrected, bad))
    except LedgerValidationError:
        pass
    else:
        raise AssertionError("invalid batch unexpectedly published")
    stored = repository.get_game(game.game_id)
    assert stored is not None
    assert stored.player_facts[0].points == game.player_facts[0].points
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all()
        assert connection.execute(select(CanonicalGameLedgerTeamFact)).all()
        assert connection.execute(select(CanonicalGameLedgerPlayerFact)).all()


def test_correction_atomically_enqueues_every_affected_materialization(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'correction.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(
        engine,
        correction_sink=LedgerCorrectionQueue(
            clock=lambda: datetime(2024, 11, 17, tzinfo=timezone.utc)
        ),
    )
    game = _game()
    repository.replace_game(game)
    corrected = replace(
        game,
        team_facts=(replace(game.team_facts[0], points=17), game.team_facts[1]),
        player_facts=(replace(game.player_facts[0], points=9), *game.player_facts[1:]),
    )
    corrected = replace(corrected, raw_rows=raw_rows_from_facts(corrected)).with_checksum()

    repository.replace_game(corrected)

    with engine.connect() as connection:
        jobs = connection.execute(select(CompositionJob)).scalars().all()
    assert len(jobs) == 6


def test_full_game_preserves_optional_assist_locations_and_fences_envelope_identity():
    payload = json.loads(
        json.dumps(
            {
                "stats": {
                    "Home": {
                        "FullGame": [
                            {
                                "EntityId": "0",
                                "Name": "Team",
                                "Minutes": "00:00",
                            },
                            {
                                "EntityId": "101",
                                "Name": "Home One",
                                "Minutes": "20:00",
                                "FG2M": 2,
                                "FG2A": 4,
                                "FG3M": 1,
                                "FG3A": 2,
                                "FtPoints": 1,
                                "FTA": 2,
                                "OffRebounds": 1,
                                "DefRebounds": 2,
                                "Assists": 3,
                                "TwoPtAssists": 1,
                                "ThreePtAssists": 2,
                                "Arc3Assists": 1,
                                "Corner3Assists": 0,
                                "AtRimAssists": 1,
                                "ShortMidRangeAssists": 0,
                                "LongMidRangeAssists": 0,
                                "Turnovers": 1,
                                "Steals": 1,
                                "Blocks": 0,
                                "Fouls": 1,
                                "Points": 8,
                            },
                            {
                                "EntityId": "102",
                                "Name": "Home Two",
                                "Minutes": "20:00",
                                "FG2M": 2,
                                "FG2A": 4,
                                "FG3M": 1,
                                "FG3A": 2,
                                "FtPoints": 1,
                                "FTA": 2,
                                "OffRebounds": 1,
                                "DefRebounds": 2,
                                "Assists": 3,
                                "Turnovers": 1,
                                "Steals": 1,
                                "Blocks": 0,
                                "Fouls": 1,
                                "Points": 8,
                            },
                        ]
                    },
                    "Away": {
                        "FullGame": [
                            {
                                "EntityId": "0",
                                "Name": "Team",
                                "Minutes": "00:00",
                            },
                            {
                                "EntityId": "201",
                                "Name": "Away One",
                                "Minutes": "20:00",
                                "FG2M": 2,
                                "FG2A": 4,
                                "FG3M": 1,
                                "FG3A": 2,
                                "FtPoints": 1,
                                "FTA": 2,
                                "OffRebounds": 1,
                                "DefRebounds": 2,
                                "Assists": 3,
                                "Turnovers": 1,
                                "Steals": 1,
                                "Blocks": 0,
                                "Fouls": 1,
                                "Points": 8,
                            },
                            {
                                "EntityId": "202",
                                "Name": "Away Two",
                                "Minutes": "20:00",
                                "FG2M": 2,
                                "FG2A": 4,
                                "FG3M": 1,
                                "FG3A": 2,
                                "FtPoints": 1,
                                "FTA": 2,
                                "OffRebounds": 1,
                                "DefRebounds": 2,
                                "Assists": 3,
                                "Turnovers": 1,
                                "Steals": 1,
                                "Blocks": 0,
                                "Fouls": 1,
                                "Points": 8,
                            },
                        ]
                    },
                },
                "home_team_abbreviation": "LAL",
                "away_team_abbreviation": "SAS",
                "home_team_id": "1610612747",
                "away_team_id": "1610612759",
                "date": "2024-11-15",
                "team_results": {
                    "Home": {
                        "FullGame": {
                            "Points": 16, "FG2M": 4, "FG2A": 8, "FG3M": 2, "FG3A": 4,
                            "FtPoints": 2, "FTA": 4, "OffRebounds": 2, "DefRebounds": 4,
                            "Rebounds": 6, "Assists": 6, "Turnovers": 2, "Steals": 2,
                            "Blocks": 0, "Fouls": 2,
                        }
                    },
                    "Away": {
                        "FullGame": {
                            "Points": 16, "FG2M": 4, "FG2A": 8, "FG3M": 2, "FG3A": 4,
                            "FtPoints": 2, "FTA": 4, "OffRebounds": 2, "DefRebounds": 4,
                            "Rebounds": 6, "Assists": 6, "Turnovers": 2, "Steals": 2,
                            "Blocks": 0, "Fouls": 2,
                        }
                    },
                },
                "participant_ids_by_team": {
                    "1610612747": [101, 102],
                    "1610612759": [201, 202],
                },
            }
        )
    )
    game = canonical_game_from_pbp(payload, event=_event())
    assert game.player_facts[0].two_point_assists == 1
    assert game.player_facts[0].long_mid_range_assists == 0

    payload["home_team_id"] = "1610612738"
    try:
        canonical_game_from_pbp(payload, event=_event())
    except LedgerValidationError as error:
        assert "team identity" in str(error)
    else:
        raise AssertionError("contradictory FullGame envelope unexpectedly passed")


def test_recorded_game_stats_dataframe_preserves_assist_locations_for_ledger_derivation():
    payload = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "pbp_stats" / "game_stats.valid.json")
        .read_text(encoding="utf-8")
    )
    frame = PBPGameLogAdapter.parse_game_stats(payload, game_id="0022400001")

    game = canonical_game_from_pbp(
        frame,
        event=_event(),
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )

    # The provider wire is sparse: observed-zero additive counters are omitted
    # from player rows, so present location evidence survives (typed facts are
    # non-null) while an omitted location stays absent (null) in the ledger.
    # The location derivation treats the omission as a governed zero only
    # because the retained split proves it arithmetically.
    leon = next(fact for fact in game.player_facts if fact.player_id == 2544)
    assert leon.two_point_assists == 5
    assert leon.three_point_assists == 3
    assert leon.arc3_assists == 2
    assert leon.corner3_assists == 1
    assert leon.at_rim_assists == 2
    assert leon.short_mid_range_assists == 2
    assert leon.long_mid_range_assists == 1
    reaves = next(fact for fact in game.player_facts if fact.player_id == 203507)
    assert reaves.long_mid_range_assists is None
    assert reaves.at_rim_assists == 2
    derived = {fact.player_id: fact for fact in derive_assist_location_facts((game,))}
    assert derived[203507].long_mid_range_assists == 0
    assert derived[203507].location_total == reaves.assists
    assert len(derive_player_per36_facts((game,))) == 3


def test_complete_assist_location_observation_derives_with_sparse_core_counts():
    payload = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "pbp_stats" / "game_stats.valid.json")
        .read_text(encoding="utf-8")
    )
    # Reaves' row is sparse (no core Blocks/OffRebounds/... keys) but carries
    # every assist-location field; LeBron and Wembanyama already carry a
    # complete location observation.  A complete location observation derives
    # even when core counts are sparse.
    for side in ("Home", "Away"):
        for row in payload["stats"][side]["FullGame"]:
            if row.get("EntityId") in (None, "0", 0):
                continue
            for key in (
                "TwoPtAssists", "ThreePtAssists", "Arc3Assists",
                "Corner3Assists", "AtRimAssists", "ShortMidRangeAssists",
                "LongMidRangeAssists",
            ):
                row.setdefault(key, 0)
    game = canonical_game_from_pbp(
        payload,
        event=_event(),
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    facts = derive_assist_location_facts((game,))
    assert len(facts) == len(game.player_facts)
    by_id = {fact.player_id: fact for fact in facts}
    assert by_id[2544].two_point_assists == 5
    assert by_id[203507].two_point_assists == 3
    assert by_id[203507].long_mid_range_assists == 0
    assert by_id[201935].three_point_assists == 1
    assert all(fact.location_total <= fact.assists for fact in facts)


def test_game_date_uses_nba_calendar_day_for_after_midnight_utc_tipoff():
    payload = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "pbp_stats" / "game_stats.valid.json")
        .read_text(encoding="utf-8")
    )
    event = {**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"}

    game = canonical_game_from_pbp(
        payload,
        event=event,
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )

    assert game.game_date == date(2024, 11, 15)


def test_publication_metadata_batch_is_atomic_and_idempotently_replaced(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    retrieved = datetime(2024, 11, 16, tzinfo=timezone.utc)
    records = tuple(
        LedgerPublicationRecord(
            stream_key=stream,
            season="2024-25",
            window_kind="season",
            window_games=0,
            as_of=date(2024, 11, 15),
            status="complete",
            checksum=stream,
            game_count=1,
            team_count=30,
            retrieved_at=retrieved,
        )
        for stream in ("traditional_opponent", "player_per36")
    )
    repository.publish_metadata_batch(records)
    repository.publish_metadata_batch(records)
    with engine.connect() as connection:
        assert len(connection.execute(select(LedgerPublication)).all()) == 2


def _clean_observation() -> dict[str, object]:
    payload = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "pbp_stats" / "game_stats.valid.json")
        .read_text(encoding="utf-8")
    )
    return payload


def _raw_observation_with_unknown_fields() -> dict[str, object]:
    payload = _clean_observation()
    payload["stats"]["Home"]["FullGame"][0]["Possessions"] = 95.5
    payload["stats"]["Home"]["FullGame"][1]["UnknownAdditiveField"] = "future-proof"
    return payload


def _install_ledger_manifest(engine, *, cutoff=None, collect_before=None):
    cutoff = cutoff or datetime(2024, 11, 16, tzinfo=timezone.utc)
    collect_before = collect_before or cutoff + timedelta(days=45)
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="ledger-manifest", season="2024-25", cutoff=cutoff,
            collect_before=collect_before, accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum=f"manifest:{cutoff.isoformat()}",
            status="active", created_at=cutoff,
        ))


def _observation_values(payload, *, observation_id, game_id, accepted_at=None, retrieved_at=None):
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    accepted_at = accepted_at or datetime(2024, 11, 16, tzinfo=timezone.utc)
    retrieved_at = retrieved_at or datetime(2024, 11, 16, tzinfo=timezone.utc)
    return {
        "observation_id": observation_id,
        "client_observation_id": f"ledger:{game_id}:{observation_id}",
        "collector_id": "railway-ledger",
        "manifest_id": "ledger-manifest",
        "environment": "server",
        "provider": "pbp",
        "observation_type": "canonical_game_ledger",
        "scope": json.dumps(
            {"game_id": game_id, "surface": "canonical_game_ledger"},
            sort_keys=True,
        ),
        "season": "2024-25",
        "cutoff": datetime(2024, 11, 16, tzinfo=timezone.utc),
        "schema_version": 1,
        "checksum": hashlib.sha256(text.encode()).hexdigest(),
        "payload": text,
        "payload_bytes": len(text.encode()),
        "retrieved_at": retrieved_at,
        "accepted_at": accepted_at,
    }


def test_full_game_archives_team_summary_and_player_rows_with_unknown_fields(tmp_path):
    payload = _raw_observation_with_unknown_fields()
    event = {**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"}
    game = canonical_game_from_pbp(
        payload,
        event=event,
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    assert len(game.raw_rows) == 5
    assert [row.row_type for row in game.raw_rows] == [
        "team", "player", "player", "team", "player",
    ]
    team_rows = {row.side: row for row in game.raw_rows if row.row_type == "team"}
    assert team_rows.keys() == {"Home", "Away"}
    home_team_row = team_rows["Home"]
    assert home_team_row.team_id == 1610612747
    assert home_team_row.entity_id is None
    assert "Possessions" in home_team_row.payload
    leon_row = next(
        row for row in game.raw_rows
        if row.row_type == "player" and row.entity_id == 2544
    )
    assert leon_row.payload.get("UnknownAdditiveField") == "future-proof"
    assert "UnknownAdditiveField" in leon_row.observed_fields
    assert home_team_row.observed_fields == tuple(sorted(home_team_row.payload))
    assert game.raw_checksum is not None
    assert all(row.schema_version == 1 for row in game.raw_rows)

    engine = create_engine(f"sqlite:///{tmp_path / 'raw.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    repository.replace_game(game)
    with engine.connect() as connection:
        rows = connection.execute(select(LedgerGameRowEvidence)).mappings().all()
    assert len(rows) == 5
    assert [row["row_type"] for row in rows] == ["team", "player", "player", "team", "player"]
    assert all(row["schema_version"] == 1 for row in rows)
    assert {row["side"] for row in rows} == {"Home", "Away"}
    assert sum(row["row_type"] == "team" for row in rows) == 2
    assert any(
        row["entity_id"] == 2544 and json.loads(row["payload"]).get("UnknownAdditiveField") == "future-proof"
        for row in rows
    )
    assert any(
        row["row_type"] == "team" and json.loads(row["payload"]).get("Possessions") == 95.5
        for row in rows
    )


def test_raw_evidence_canonicalization_is_replay_stable(tmp_path):
    first = _raw_observation_with_unknown_fields()
    second = json.loads(json.dumps(first, sort_keys=True))
    game_one = canonical_game_from_pbp(
        first,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    game_two = canonical_game_from_pbp(
        second,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    assert game_one.raw_checksum == game_two.raw_checksum
    assert tuple(row.checksum for row in game_one.raw_rows) == tuple(
        row.checksum for row in game_two.raw_rows
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'replay.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    repository.replace_game(game_one)
    repeated = repository.replace_game(game_two)
    assert not repeated.inserted and not repeated.replaced
    with engine.connect() as connection:
        assert len(connection.execute(select(LedgerGameRowEvidence)).all()) == 5


def test_reload_then_replace_unchanged_is_idempotent_and_changes_no_evidence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reload_idempotent.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    repository.replace_game(game)

    stored = repository.get_game(game.game_id)
    assert stored is not None
    # Reloaded raw rows come back in the same canonical Home-then-Away order
    # used at ingestion, so raw_checksum stays stable across reload.
    assert [row.side for row in stored.raw_rows] == [row.side for row in game.raw_rows]
    assert stored.raw_checksum == game.raw_checksum

    result = repository.replace_game(stored)
    assert not result.inserted and not result.replaced
    with engine.connect() as connection:
        game_row = connection.execute(select(CanonicalGameLedgerGame).where(
            CanonicalGameLedgerGame.game_id == game.game_id
        )).mappings().one()
        raw_rows = connection.execute(select(LedgerGameRowEvidence)).mappings().all()
    assert game_row["checksum"] == game.checksum
    assert game_row["raw_checksum"] == game.raw_checksum
    assert len(raw_rows) == len(game.raw_rows)
    assert {row["checksum"] for row in raw_rows} == {row.checksum for row in game.raw_rows}


def test_reordered_raw_rows_hash_stably_and_replay_idempotently(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reordered.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    reordered = replace(
        game,
        raw_rows=tuple(reversed(game.raw_rows)),
        checksum=None,
    ).with_checksum()

    # Identical evidence arriving in a different order must hash identically
    # and replay idempotently rather than registering as a replacement.
    assert reordered.raw_checksum == game.raw_checksum
    result = repository.replace_game(reordered)
    assert result.inserted
    repeated = repository.replace_game(reordered)
    assert not repeated.inserted and not repeated.replaced
    stored = repository.get_game(game.game_id)
    assert stored.raw_checksum == game.raw_checksum


def test_duplicate_side_index_raw_rows_reject_at_repository_boundary(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'duplicate_index.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    # The Home team-summary row occupies (Home, 0); a player row sharing that
    # provider array position must be rejected even though row_type differs,
    # because the canonical raw-row order and checksum are ambiguous otherwise.
    duplicated = replace(
        game,
        raw_rows=tuple(
            replace(row, row_index=0) if row.entity_id == 101 else row
            for row in game.raw_rows
        ),
        checksum=None,
    ).with_checksum()

    try:
        repository.replace_game(duplicated)
    except LedgerValidationError as error:
        assert "one archived row per side and provider index" in str(error)
    else:
        raise AssertionError("raw evidence with duplicate side/provider index unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


@pytest.mark.parametrize("bad_index", [-1, True, 1.5, 99])
def test_invalid_raw_row_index_rejects_at_repository_boundary(tmp_path, bad_index):
    engine = create_engine(f"sqlite:///{tmp_path / 'invalid_index.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    invalid = replace(
        game,
        raw_rows=tuple(
            replace(row, row_index=bad_index) if row.entity_id == 101 else row
            for row in game.raw_rows
        ),
        checksum=None,
    ).with_checksum()

    try:
        repository.replace_game(invalid)
    except LedgerValidationError as error:
        message = str(error)
        assert "row_index must be a non-negative integer" in message or "contiguous" in message
    else:
        raise AssertionError("raw evidence with invalid row_index unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_team_row_metadata_contradicts_player_payload_rejects(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'team_label_player.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    player_payload = next(
        row.payload for row in game.raw_rows
        if row.row_type == "player" and row.entity_id == 101
    )
    mislabeled = replace(
        game,
        raw_rows=tuple(
            replace(
                row,
                payload=player_payload,
                checksum=canonical_row_checksum(player_payload),
                observed_fields=tuple(sorted(player_payload)),
            )
            if row.row_type == "team" and row.side == "Home"
            else row
            for row in game.raw_rows
        ),
        checksum=None,
    ).with_checksum()

    try:
        repository.replace_game(mislabeled)
    except LedgerValidationError as error:
        assert "team-summary row metadata contradicts the provider payload identity" in str(error)
    else:
        raise AssertionError("team row labeled with a player payload unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_player_row_entity_id_mismatch_rejects(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'player_id_mismatch.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    mismatched = replace(
        game,
        raw_rows=tuple(
            replace(row, entity_id=999) if row.entity_id == 101 else row
            for row in game.raw_rows
        ),
        checksum=None,
    ).with_checksum()

    try:
        repository.replace_game(mismatched)
    except LedgerValidationError as error:
        assert "player row metadata must match the provider payload identity" in str(error)
    else:
        raise AssertionError("player row entity metadata that contradicts the payload unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_player_row_entity_name_mismatch_rejects(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'player_name_mismatch.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    mismatched = replace(
        game,
        raw_rows=tuple(
            replace(row, entity_name="Bogus") if row.entity_id == 101 else row
            for row in game.raw_rows
        ),
        checksum=None,
    ).with_checksum()

    try:
        repository.replace_game(mismatched)
    except LedgerValidationError as error:
        assert "player row metadata must match the provider payload identity" in str(error)
    else:
        raise AssertionError("player row name metadata that contradicts the payload unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_team_summary_row_is_team_fact_authority_with_team_rebounds(tmp_path):
    payload = _raw_observation_with_unknown_fields()
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    home_team_fact = next(fact for fact in game.team_facts if fact.team_id == 1610612747)
    # The real team-summary row is sparse: it carries only team-only residuals
    # (the rebound credits no player is credited with), so every complete team
    # count is the participating-player sum plus that residual.  Home player
    # rebound sums are 1 OREB / 11 DREB; the team row adds a 2 OREB team-only
    # residual (and no defensive residual), while additive-equivalent facts
    # (points, assists, ...) equal player sums exactly.  Possessions is read
    # from the team-summary row.
    assert home_team_fact.offensive_rebounds == 3
    assert home_team_fact.defensive_rebounds == 11
    assert home_team_fact.rebounds == 14
    assert home_team_fact.points == 40
    assert home_team_fact.assists == 13
    assert home_team_fact.possessions == 95.5


def test_team_results_diagnostic_must_reconcile_with_player_sums_and_residuals():
    payload = _raw_observation_with_unknown_fields()
    # The diagnostic envelope is complete but contradicts the declared team
    # authority: Home player points sum to 40 with no team residual, while the
    # diagnostic publishes 99.  The parity check must reject atomically.
    payload["team_results"]["Home"]["FullGame"]["Points"] = 99
    try:
        canonical_game_from_pbp(
            payload,
            event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
            participant_ids_by_team={
                1610612747: (2544, 203507),
                1610612759: (201935,),
            },
        )
    except LedgerValidationError as error:
        assert "does not reconcile" in str(error)
    else:
        raise AssertionError("contradictory team_results evidence unexpectedly passed")


def test_team_results_reconciles_across_all_count_fields():
    payload = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "pbp_stats" / "game_stats.valid.json")
        .read_text(encoding="utf-8")
    )
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    home = next(fact for fact in game.team_facts if fact.team_id == 1610612747)
    assert home.rebounds == 14
    assert home.offensive_rebounds == 3
    assert home.defensive_rebounds == 11
    assert home.points == 40
    away = next(fact for fact in game.team_facts if fact.team_id == 1610612759)
    assert away.offensive_rebounds == 6
    assert away.defensive_rebounds == 13
    assert away.rebounds == 19
    assert away.points == 25


def test_correction_atomically_replaces_raw_and_typed_evidence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'correction_raw.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    payload = _raw_observation_with_unknown_fields()
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    repository.replace_game(game)

    corrected_payload = json.loads(json.dumps(payload))
    corrected_payload["stats"]["Home"]["FullGame"][1]["Points"] = 26
    corrected_payload["team_results"]["Home"]["FullGame"]["Points"] = 41
    corrected = canonical_game_from_pbp(
        corrected_payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    result = repository.replace_game(corrected)
    assert result.replaced
    with engine.connect() as connection:
        game_row = connection.execute(select(CanonicalGameLedgerGame).where(
            CanonicalGameLedgerGame.game_id == corrected.game_id
        )).mappings().one()
        raw_rows = connection.execute(select(LedgerGameRowEvidence)).mappings().all()
    assert game_row["checksum"] == corrected.checksum
    assert game_row["raw_checksum"] == corrected.raw_checksum
    assert len(raw_rows) == 5
    by_entity = {row["entity_id"]: row for row in raw_rows}
    assert json.loads(by_entity[2544]["payload"])["Points"] == 26


def test_raw_only_correction_still_replaces_evidence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'raw_only.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    payload = _raw_observation_with_unknown_fields()
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    repository.replace_game(game)
    changed = json.loads(json.dumps(payload))
    changed["stats"]["Home"]["FullGame"][1]["UnknownAdditiveField"] = "drifted"
    corrected = canonical_game_from_pbp(
        changed,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    # Typed primitives are unchanged, so the typed checksum is identical; the
    # raw evidence changed, so the complete game must still be replaced.
    assert corrected.checksum == game.checksum
    result = repository.replace_game(corrected)
    assert result.replaced
    with engine.connect() as connection:
        game_row = connection.execute(select(CanonicalGameLedgerGame).where(
            CanonicalGameLedgerGame.game_id == corrected.game_id
        )).mappings().one()
    assert game_row["raw_checksum"] == corrected.raw_checksum
    assert game_row["checksum"] == corrected.checksum


def test_duplicate_team_summary_row_rejects_the_complete_game(tmp_path):
    payload = _raw_observation_with_unknown_fields()
    payload["stats"]["Away"]["FullGame"].insert(
        0,
        {
            "EntityId": "0",
            "Name": "Team",
            "Minutes": "00:00",
            "OffRebounds": 4,
            "DefRebounds": 1,
        },
    )
    try:
        canonical_game_from_pbp(
            payload,
            event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
            participant_ids_by_team={
                1610612747: (2544, 203507),
                1610612759: (201935,),
            },
        )
    except LedgerValidationError as error:
        assert "team-summary" in str(error)
    else:
        raise AssertionError("duplicate team-summary evidence unexpectedly passed")


def test_missing_away_team_summary_row_rejects_the_complete_game():
    payload = _raw_observation_with_unknown_fields()
    payload["stats"]["Away"]["FullGame"] = payload["stats"]["Away"]["FullGame"][1:]
    try:
        canonical_game_from_pbp(
            payload,
            event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
            participant_ids_by_team={
                1610612747: (2544, 203507),
                1610612759: (201935,),
            },
        )
    except LedgerValidationError as error:
        assert "team-summary" in str(error)
    else:
        raise AssertionError("accepted evidence missing an Away team-summary row")


def test_both_provider_team_summary_rows_are_archived(tmp_path):
    payload = _raw_observation_with_unknown_fields()
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    assert len(game.raw_rows) == 5
    team_rows = {row.side: row for row in game.raw_rows if row.row_type == "team"}
    assert team_rows.keys() == {"Home", "Away"}
    assert team_rows["Away"].team_id == 1610612759
    assert team_rows["Away"].entity_id is None

    engine = create_engine(f"sqlite:///{tmp_path / 'both_team_rows.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    repository.replace_game(game)
    with engine.connect() as connection:
        rows = connection.execute(select(LedgerGameRowEvidence)).mappings().all()
    assert len(rows) == 5
    assert sum(row["row_type"] == "team" for row in rows) == 2
    assert {row["side"] for row in rows if row["row_type"] == "team"} == {"Home", "Away"}


def test_missing_optional_expanded_field_preserves_game_and_dependent_fact():
    payload = _raw_observation_with_unknown_fields()
    for row in payload["stats"]["Home"]["FullGame"] + payload["stats"]["Away"]["FullGame"]:
        if row.get("EntityId") == "2544":
            row.pop("Arc3Assists", None)
            row.pop("Corner3Assists", None)
            row.pop("AtRimAssists", None)
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    leon = next(fact for fact in game.player_facts if fact.player_id == 2544)
    assert leon.arc3_assists is None
    assert leon.corner3_assists is None
    assert leon.at_rim_assists is None
    assert leon.assists == 8
    archived = next(row for row in game.raw_rows if row.entity_id == 2544)
    assert "Arc3Assists" not in archived.observed_fields
    assert "Arc3Assists" not in archived.payload


def test_schema_drift_is_recorded_and_alerted_when_field_sets_change(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'schema_drift.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(
        engine,
        schema_drift_sink=record_schema_drift,
    )
    payload = _clean_observation()
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    repository.replace_game(game)

    drifted = json.loads(json.dumps(payload))
    drifted["stats"]["Home"]["FullGame"][1]["ProviderAddedField"] = 7
    corrected = canonical_game_from_pbp(
        drifted,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    original = next(row for row in game.raw_rows if row.entity_id == 2544)
    updated = next(row for row in corrected.raw_rows if row.entity_id == 2544)
    assert "ProviderAddedField" not in original.observed_fields
    assert "ProviderAddedField" in updated.observed_fields
    assert updated.observed_fields == tuple(sorted(updated.payload))

    assert repository.replace_game(corrected).replaced
    with engine.connect() as connection:
        row = connection.execute(select(LedgerGameRowEvidence).where(
            LedgerGameRowEvidence.entity_id == 2544
        )).mappings().one()
        alert = connection.execute(select(ReconciliationItem).where(
            ReconciliationItem.kind == "schema_drift"
        )).mappings().one()
    assert "ProviderAddedField" in json.loads(row["observed_fields"])
    assert json.loads(row["payload"])["ProviderAddedField"] == 7
    assert alert["season"] == "2024-25"
    assert alert["reason"] == "field_set_change"
    assert alert["status"] == "open"
    assert json.loads(alert["details"])["added_fields"] == ["ProviderAddedField"]
    assert json.loads(alert["details"])["removed_fields"] == []


def test_unchanged_replacement_emits_no_schema_drift_alert(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'no-drift.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(
        engine,
        schema_drift_sink=record_schema_drift,
    )
    game = canonical_game_from_pbp(
        _clean_observation(),
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    repository.replace_game(game)
    assert repository.replace_game(game).inserted is False
    assert repository.replace_game(game).replaced is False
    with engine.connect() as connection:
        assert connection.execute(select(ReconciliationItem)).all() == []


def test_first_seen_game_with_unknown_additive_field_alerts_schema_drift(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'first-seen-drift.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(
        engine,
        schema_drift_sink=record_schema_drift,
    )
    game = canonical_game_from_pbp(
        _raw_observation_with_unknown_fields(),
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )

    assert repository.replace_game(game).inserted
    with engine.connect() as connection:
        row = connection.execute(select(ReconciliationItem).where(
            ReconciliationItem.kind == "schema_drift"
        )).mappings().one()
    assert row["reason"] == "unknown_field"
    assert row["status"] == "open"
    assert json.loads(row["details"])["added_fields"] == ["UnknownAdditiveField"]
    assert json.loads(row["details"])["removed_fields"] == []


def test_first_seen_game_inside_the_governed_baseline_emits_no_alert(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'first-seen-clean.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(
        engine,
        schema_drift_sink=record_schema_drift,
    )
    game = canonical_game_from_pbp(
        _clean_observation(),
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )

    assert repository.replace_game(game).inserted
    with engine.connect() as connection:
        assert connection.execute(select(ReconciliationItem)).all() == []


def test_pre_migration_first_raw_archive_with_unknown_field_alerts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pre032-drift.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(
        engine,
        schema_drift_sink=record_schema_drift,
    )
    with engine.begin() as connection:
        connection.execute(CanonicalGameLedgerGame.__table__.insert().values(
            game_id="0022400001", season="2024-25", season_type="Regular Season",
            game_date=date(2024, 11, 15),
            home_team_id=1610612747, home_team_tricode="LAL",
            away_team_id=1610612759, away_team_tricode="SAS",
            status="final", source_observation_id="legacy:0022400001",
            checksum="legacy-typed-checksum", raw_checksum=None,
            retrieved_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
            updated_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
        ))
    replacement = canonical_game_from_pbp(
        _raw_observation_with_unknown_fields(),
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )

    assert repository.replace_game(replacement).replaced
    with engine.connect() as connection:
        row = connection.execute(select(ReconciliationItem).where(
            ReconciliationItem.kind == "schema_drift"
        )).mappings().one()
    assert row["reason"] == "unknown_field"


def test_accepted_observation_is_cryptographically_bound_to_the_candidate(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'binding.sqlite3'}")
    run_migrations(engine)
    _install_ledger_manifest(engine)
    repository = CanonicalGameLedgerRepository(engine)
    payload = _clean_observation()
    observation_id = "obs-bound-000000000001"
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
        source_observation_id=observation_id,
        retrieved_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
    )

    result = repository.replace_games_atomic(
        (game,),
        accepted_observations={
            observation_id: _observation_values(
                payload, observation_id=observation_id, game_id=game.game_id,
            ),
        },
    )

    assert result[0].inserted
    stored = repository.get_game(game.game_id)
    assert stored is not None
    assert stored.source_observation_id == observation_id
    with engine.connect() as connection:
        assert connection.execute(select(CollectionObservation).where(
            CollectionObservation.observation_id == observation_id
        )).one()


def test_observation_for_a_different_document_is_rejected_without_any_write(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'binding-mismatch.sqlite3'}")
    run_migrations(engine)
    _install_ledger_manifest(engine)
    repository = CanonicalGameLedgerRepository(engine)
    payload = _clean_observation()
    foreign = json.loads(json.dumps(payload))
    foreign["stats"]["Home"]["FullGame"][1]["Points"] = 99
    observation_id = "obs-mismatch-0000000001"
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
        source_observation_id=observation_id,
        retrieved_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
    )

    try:
        repository.replace_games_atomic(
            (game,),
            accepted_observations={
                observation_id: _observation_values(
                    foreign, observation_id=observation_id, game_id=game.game_id,
                ),
            },
        )
    except LedgerValidationError as error:
        assert "not bound to the candidate raw evidence" in str(error)
    else:
        raise AssertionError("observation with a foreign payload unexpectedly accepted")
    assert repository.get_game(game.game_id) is None
    with engine.connect() as connection:
        assert connection.execute(select(CollectionObservation)).all() == []
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_observation_with_tampered_checksum_is_rejected(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'binding-tamper.sqlite3'}")
    run_migrations(engine)
    _install_ledger_manifest(engine)
    repository = CanonicalGameLedgerRepository(engine)
    payload = _clean_observation()
    observation_id = "obs-tampered-0000000001"
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
        source_observation_id=observation_id,
        retrieved_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
    )
    values = _observation_values(
        payload, observation_id=observation_id, game_id=game.game_id,
    )
    values["checksum"] = "deadbeef"

    try:
        repository.replace_games_atomic(
            (game,),
            accepted_observations={observation_id: values},
        )
    except LedgerValidationError as error:
        assert "checksum does not match its payload" in str(error)
    else:
        raise AssertionError("observation with a tampered checksum unexpectedly accepted")
    assert repository.get_game(game.game_id) is None


def test_idempotent_replay_with_bound_observation_persists_no_new_observation(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'binding-replay.sqlite3'}")
    run_migrations(engine)
    _install_ledger_manifest(engine)
    repository = CanonicalGameLedgerRepository(engine)
    payload = _clean_observation()
    observation_id = "obs-replay-000000000001"
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
        source_observation_id=observation_id,
        retrieved_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
    )
    values = _observation_values(
        payload, observation_id=observation_id, game_id=game.game_id,
    )

    assert repository.replace_games_atomic(
        (game,), accepted_observations={observation_id: values},
    )[0].inserted
    replayed = repository.replace_games_atomic(
        (game,), accepted_observations={observation_id: values},
    )[0]

    assert not replayed.inserted and not replayed.replaced
    with engine.connect() as connection:
        assert len(connection.execute(select(CollectionObservation)).all()) == 1
        assert len(connection.execute(select(LedgerGameRowEvidence)).all()) == 5


def test_stale_or_equal_bound_correction_is_a_noop_without_observation_or_queue(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stale-correction.sqlite3'}")
    run_migrations(engine)
    _install_ledger_manifest(engine)
    repository = CanonicalGameLedgerRepository(
        engine,
        correction_sink=LedgerCorrectionQueue(
            clock=lambda: datetime(2024, 11, 18, tzinfo=timezone.utc),
        ),
    )
    payload = _clean_observation()
    first = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
        source_observation_id="obs-stale-first",
        retrieved_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
    )
    first_values = _observation_values(
        payload,
        observation_id=first.source_observation_id,
        game_id=first.game_id,
        accepted_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
        retrieved_at=first.retrieved_at,
    )
    assert repository.replace_games_atomic(
        (first,), accepted_observations={first.source_observation_id: first_values}
    )[0].inserted

    corrected_payload = json.loads(json.dumps(payload))
    corrected_payload["stats"]["Home"]["FullGame"][1]["Points"] = 26
    corrected_payload["team_results"]["Home"]["FullGame"]["Points"] = 41
    second = canonical_game_from_pbp(
        corrected_payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
        source_observation_id="obs-stale-second",
        retrieved_at=datetime(2024, 11, 17, tzinfo=timezone.utc),
    )
    second_values = _observation_values(
        corrected_payload,
        observation_id=second.source_observation_id,
        game_id=second.game_id,
        accepted_at=datetime(2024, 11, 17, tzinfo=timezone.utc),
        retrieved_at=second.retrieved_at,
    )
    assert repository.replace_games_atomic(
        (second,), accepted_observations={second.source_observation_id: second_values}
    )[0].replaced

    stale_payload = json.loads(json.dumps(corrected_payload))
    stale_payload["stats"]["Home"]["FullGame"][1]["Points"] = 27
    stale_payload["team_results"]["Home"]["FullGame"]["Points"] = 42
    stale = canonical_game_from_pbp(
        stale_payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
        source_observation_id="obs-stale-third",
        retrieved_at=datetime(2024, 11, 18, tzinfo=timezone.utc),
    )
    stale_values = _observation_values(
        stale_payload,
        observation_id=stale.source_observation_id,
        game_id=stale.game_id,
        accepted_at=datetime(2024, 11, 17, tzinfo=timezone.utc),
        retrieved_at=stale.retrieved_at,
    )
    result = repository.replace_games_atomic(
        (stale,), accepted_observations={stale.source_observation_id: stale_values}
    )[0]
    assert not result.inserted and not result.replaced
    assert result.checksum == second.checksum
    stored = repository.get_game(first.game_id)
    assert stored is not None
    assert stored.source_observation_id == second.source_observation_id
    with engine.connect() as connection:
        assert {
            row[0]
            for row in connection.execute(select(CollectionObservation.observation_id))
        } == {first.source_observation_id, second.source_observation_id}
        assert len(connection.execute(select(CompositionJob)).all()) == 6


def test_bound_correction_failure_rolls_back_observation_raw_typed_and_jobs(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'correction-rollback.sqlite3'}")
    run_migrations(engine)
    _install_ledger_manifest(engine)
    repository = CanonicalGameLedgerRepository(engine)
    payload = _clean_observation()
    first = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
        source_observation_id="obs-rollback-first",
        retrieved_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
    )
    first_values = _observation_values(
        payload,
        observation_id=first.source_observation_id,
        game_id=first.game_id,
        accepted_at=first.retrieved_at,
        retrieved_at=first.retrieved_at,
    )
    assert repository.replace_games_atomic(
        (first,), accepted_observations={first.source_observation_id: first_values}
    )[0].inserted

    corrected_payload = json.loads(json.dumps(payload))
    corrected_payload["stats"]["Home"]["FullGame"][1]["Points"] = 26
    corrected_payload["team_results"]["Home"]["FullGame"]["Points"] = 41
    corrected = canonical_game_from_pbp(
        corrected_payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
        source_observation_id="obs-rollback-correction",
        retrieved_at=datetime(2024, 11, 17, tzinfo=timezone.utc),
    )
    corrected_values = _observation_values(
        corrected_payload,
        observation_id=corrected.source_observation_id,
        game_id=corrected.game_id,
        accepted_at=corrected.retrieved_at,
        retrieved_at=corrected.retrieved_at,
    )

    def fail_after_ledger_write(connection, game):
        assert connection.execute(select(CanonicalGameLedgerGame)).first() is not None
        raise RuntimeError("injected correction sink failure")

    repository.correction_sink = fail_after_ledger_write
    with pytest.raises(RuntimeError, match="injected correction sink failure"):
        repository.replace_games_atomic(
            (corrected,),
            accepted_observations={corrected.source_observation_id: corrected_values},
        )

    stored = repository.get_game(first.game_id)
    assert stored is not None
    assert stored.checksum == first.checksum
    assert stored.raw_checksum == first.raw_checksum
    with engine.connect() as connection:
        assert {
            row[0]
            for row in connection.execute(select(CollectionObservation.observation_id))
        } == {first.source_observation_id}
        assert connection.execute(select(CompositionJob)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all()


def test_observation_with_mismatched_retrieval_time_is_rejected_without_any_write(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'binding-time.sqlite3'}")
    run_migrations(engine)
    _install_ledger_manifest(engine)
    repository = CanonicalGameLedgerRepository(engine)
    payload = _clean_observation()
    observation_id = "obs-time-mismatch-0001"
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
        source_observation_id=observation_id,
        retrieved_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
    )
    values = _observation_values(
        payload, observation_id=observation_id, game_id=game.game_id,
        retrieved_at=datetime(2024, 11, 16, 1, tzinfo=timezone.utc),
    )

    try:
        repository.replace_games_atomic((game,), accepted_observations={observation_id: values})
    except LedgerValidationError as error:
        assert "retrieval time" in str(error)
    else:
        raise AssertionError("observation with a mismatched retrieval time unexpectedly accepted")
    assert repository.get_game(game.game_id) is None
    with engine.connect() as connection:
        assert connection.execute(select(CollectionObservation)).all() == []
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_ledger_observations_survive_gc_including_superseded_corrections(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger-retention.sqlite3'}")
    run_migrations(engine)
    _install_ledger_manifest(
        engine, collect_before=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    repository = CanonicalGameLedgerRepository(engine)
    payload = _clean_observation()
    first_id = "obs-retain-first"
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
        source_observation_id=first_id,
        retrieved_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
    )
    assert repository.replace_games_atomic(
        (game,),
        accepted_observations={
            first_id: _observation_values(
                payload, observation_id=first_id, game_id=game.game_id,
            ),
        },
    )[0].inserted

    corrected_payload = json.loads(json.dumps(payload))
    corrected_payload["stats"]["Home"]["FullGame"][1]["Points"] = 26
    corrected_payload["team_results"]["Home"]["FullGame"]["Points"] = 41
    second_id = "obs-retain-second"
    corrected = canonical_game_from_pbp(
        corrected_payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
        source_observation_id=second_id,
        retrieved_at=datetime(2024, 11, 17, tzinfo=timezone.utc),
    )
    assert repository.replace_games_atomic(
        (corrected,),
        accepted_observations={
            second_id: _observation_values(
                corrected_payload, observation_id=second_id, game_id=game.game_id,
                retrieved_at=datetime(2024, 11, 17, tzinfo=timezone.utc),
                accepted_at=datetime(2024, 11, 17, tzinfo=timezone.utc),
            ),
        },
    )[0].replaced

    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(CollectionObservation.__table__.insert().values(
            observation_id="obs-unrelated", client_observation_id="unrelated-client",
            collector_id="collector", manifest_id="ledger-manifest",
            environment="server", provider="pbp", observation_type="league_obs",
            scope=json.dumps({"window": "season"}), season="2024-25", cutoff=now,
            schema_version=1, checksum="u" * 64, payload="{}", payload_bytes=2,
            retrieved_at=now, accepted_at=now - timedelta(days=60),
        ))
    operations = CollectionOperationsService(engine, clock=lambda: now)

    assert operations.gc_observations(now=now, retention_days=30) == 1
    with engine.connect() as connection:
        surviving = set(
            connection.execute(select(CollectionObservation.observation_id)).scalars()
        )
        references = connection.execute(select(LedgerObservationEvidence)).all()
    assert surviving == {first_id, second_id}
    assert {row.observation_id for row in references} == {first_id, second_id}
    assert {row.game_id for row in references} == {"0022400001"}


def test_schema_drift_details_preserve_the_affected_game_id(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'drift-game-id.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(
        engine,
        schema_drift_sink=record_schema_drift,
    )
    game = canonical_game_from_pbp(
        _raw_observation_with_unknown_fields(),
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )

    assert repository.replace_game(game).inserted
    with engine.connect() as connection:
        row = connection.execute(select(ReconciliationItem).where(
            ReconciliationItem.kind == "schema_drift"
        )).mappings().one()
    assert json.loads(row["details"])["game_id"] == game.game_id


def test_first_seen_game_with_documented_boxscore_vocabulary_emits_no_alert(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'broad-vocab.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(
        engine,
        schema_drift_sink=record_schema_drift,
    )
    payload = _clean_observation()
    for side in ("Home", "Away"):
        for row in payload["stats"][side]["FullGame"]:
            row["AtRimFGM"] = 1
            row["OffReboundOpportunities"] = 4
            row["BadPassTurnovers"] = 1
            row["ShootingFouls"] = 1
            row["SecondChancePoints"] = 2
            row["PenaltyPoints"] = 1
            row["Pace"] = 102.5
            row["ShotQualityAvg"] = 0.55
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )

    assert repository.replace_game(game).inserted
    with engine.connect() as connection:
        assert connection.execute(select(ReconciliationItem)).all() == []


def test_governed_baseline_covers_the_documented_boxscore_vocabulary():
    assert "Pace" in LEDGER_GOVERNED_FULLGAME_FIELDS
    assert "SecondChancePoints" in LEDGER_GOVERNED_FULLGAME_FIELDS
    assert "OffReboundOpportunities" in LEDGER_GOVERNED_FULLGAME_FIELDS
    assert "ShootingFouls" in LEDGER_GOVERNED_FULLGAME_FIELDS
    assert "BadPassTurnovers" in LEDGER_GOVERNED_FULLGAME_FIELDS
    assert "AtRimFGA" in LEDGER_GOVERNED_FULLGAME_FIELDS
    assert "PenaltyPoints" in LEDGER_GOVERNED_FULLGAME_FIELDS
    payload = _clean_observation()
    observed = set()
    for side in ("Home", "Away"):
        for row in payload["stats"][side]["FullGame"]:
            observed |= set(row)
    assert observed <= LEDGER_GOVERNED_FULLGAME_FIELDS


def test_pre_migration_games_without_raw_evidence_are_detected(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pre032.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    repository.replace_game(game)
    assert repository.game_ids_without_raw_evidence("2024-25") == frozenset()

    # Simulate a game accepted before migration 032: typed facts only, a NULL
    # raw_checksum, and no archived raw rows.
    with engine.begin() as connection:
        connection.execute(CanonicalGameLedgerGame.__table__.insert().values(
            game_id="0022400002", season="2024-25", season_type="Regular Season",
            game_date=date(2024, 11, 14),
            home_team_id=1610612747, home_team_tricode="LAL",
            away_team_id=1610612759, away_team_tricode="SAS",
            status="final", source_observation_id="legacy:0022400002",
            checksum="legacy-typed-checksum",
            raw_checksum=None,
            retrieved_at=datetime(2024, 11, 15, tzinfo=timezone.utc),
            updated_at=datetime(2024, 11, 15, tzinfo=timezone.utc),
        ))
    assert repository.game_ids_without_raw_evidence("2024-25") == frozenset({"0022400002"})
    assert repository.game_ids_without_raw_evidence(
        "2024-25", through=date(2024, 11, 14),
    ) == frozenset({"0022400002"})
    assert repository.game_ids_without_raw_evidence(
        "2024-25", through=date(2024, 11, 13),
    ) == frozenset()


def test_sparse_player_row_with_missing_zero_counts_is_a_governed_zero(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sparse_player.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    payload = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "pbp_stats" / "game_stats.valid.json")
        .read_text(encoding="utf-8")
    )
    reaves_row = next(
        row for row in payload["stats"]["Home"]["FullGame"]
        if row.get("EntityId") == "203507"
    )
    # The provider omits observed-zero additive counters, so dropping counters
    # that are independently proven to be zero must not reject the game: Reaves
    # finished with no blocks and no offensive rebounds, and the complete
    # team_results diagnostic reconciliation (blocks and offensive rebounds
    # included) confirms both stay consistent as governed zeroes.
    reaves_row.pop("Blocks", None)
    reaves_row.pop("OffRebounds", None)
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    reaves = next(fact for fact in game.player_facts if fact.player_id == 203507)
    assert reaves.blocks == 0
    assert reaves.offensive_rebounds == 0
    assert reaves.points == 15
    assert repository.replace_game(game).inserted
    stored = repository.get_game(game.game_id)
    assert stored is not None
    assert stored.player_facts == game.player_facts
    with engine.connect() as connection:
        assert len(connection.execute(select(LedgerGameRowEvidence)).all()) == 5


def test_missing_provably_nonzero_points_rejects_atomically():
    payload = _raw_observation_with_unknown_fields()
    leon_row = next(
        row for row in payload["stats"]["Home"]["FullGame"]
        if row.get("EntityId") == "2544"
    )
    # Dropping Points is only a governed zero when retained evidence proves it
    # can be zero.  LeBron's retained makes and free-throw points prove his
    # total is 25, so the omission is corrupted evidence: missing nonzero
    # counts fail through independent arithmetic and reject atomically.
    leon_row.pop("Points", None)
    try:
        canonical_game_from_pbp(
            payload,
            event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
            participant_ids_by_team={
                1610612747: (2544, 203507),
                1610612759: (201935,),
            },
        )
    except LedgerValidationError as error:
        assert "missing nonzero points" in str(error)
    else:
        raise AssertionError("player row missing provably nonzero points unexpectedly published")


def test_missing_nonzero_player_blocks_with_diagnostics_rejects():
    payload = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "pbp_stats" / "game_stats.valid.json")
        .read_text(encoding="utf-8")
    )
    leon_row = next(
        row for row in payload["stats"]["Home"]["FullGame"]
        if row.get("EntityId") == "2544"
    )
    # Blocks has no in-row arithmetic identity, so a missing value is a governed
    # zero only when the complete team_results diagnostic reconciliation stays
    # consistent.  LeBron finished with a block and the complete Home diagnostic
    # (Blocks 1) proves it, so the omission is corrupted evidence and must reject
    # atomically rather than persisting a fabricated zero.
    leon_row.pop("Blocks", None)
    try:
        canonical_game_from_pbp(
            payload,
            event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
            participant_ids_by_team={
                1610612747: (2544, 203507),
                1610612759: (201935,),
            },
        )
    except LedgerValidationError as error:
        assert "does not reconcile" in str(error)
    else:
        raise AssertionError("player row missing diagnostic-proven nonzero Blocks unexpectedly published")


def test_missing_team_results_envelope_rejects_atomically():
    payload = _raw_observation_with_unknown_fields()
    payload.pop("team_results", None)
    try:
        canonical_game_from_pbp(
            payload,
            event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
            participant_ids_by_team={
                1610612747: (2544, 203507),
                1610612759: (201935,),
            },
        )
    except LedgerValidationError as error:
        assert "team_results diagnostic envelope" in str(error)
    else:
        raise AssertionError("accepted evidence without the team_results envelope unexpectedly passed")


def test_missing_one_side_team_results_envelope_rejects_atomically():
    payload = _raw_observation_with_unknown_fields()
    payload["team_results"].pop("Away", None)
    try:
        canonical_game_from_pbp(
            payload,
            event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
            participant_ids_by_team={
                1610612747: (2544, 203507),
                1610612759: (201935,),
            },
        )
    except LedgerValidationError as error:
        assert "team_results diagnostic envelope for Away" in str(error)
    else:
        raise AssertionError("accepted evidence missing an Away diagnostic envelope unexpectedly passed")


def test_missing_team_results_full_game_envelope_rejects_atomically():
    payload = _raw_observation_with_unknown_fields()
    payload["team_results"]["Home"] = {"Name": "LAL"}
    try:
        canonical_game_from_pbp(
            payload,
            event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
            participant_ids_by_team={
                1610612747: (2544, 203507),
                1610612759: (201935,),
            },
        )
    except LedgerValidationError as error:
        assert "team_results diagnostic envelope for Home" in str(error)
    else:
        raise AssertionError("accepted evidence without a Home FullGame diagnostic envelope unexpectedly passed")


@pytest.mark.parametrize(
    ("field", "mutation"),
    [
        ("Blocks", "remove"),
        ("Blocks", "null"),
        ("Blocks", "malformed"),
        ("Blocks", "negative"),
        ("Points", "remove"),
        ("Assists", "remove"),
    ],
)
def test_missing_null_or_malformed_team_results_diagnostic_field_rejects(field, mutation):
    payload = _raw_observation_with_unknown_fields()
    diagnostic = payload["team_results"]["Home"]["FullGame"]
    if mutation == "remove":
        diagnostic.pop(field, None)
    elif mutation == "null":
        diagnostic[field] = None
    elif mutation == "malformed":
        diagnostic[field] = "not-a-count"
    else:
        diagnostic[field] = -1
    try:
        canonical_game_from_pbp(
            payload,
            event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
            participant_ids_by_team={
                1610612747: (2544, 203507),
                1610612759: (201935,),
            },
        )
    except LedgerValidationError as error:
        message = str(error)
        assert (
            "team_results" in message
            or "does not reconcile" in message
            or "must be a non-negative integer" in message
        )
    else:
        raise AssertionError("accepted evidence with incomplete or malformed team_results diagnostics unexpectedly passed")


def test_archived_player_row_missing_provably_nonzero_points_rejects_at_repository_boundary(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'raw_missing_points.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    incomplete = _mutate_raw_field(
        game,
        lambda row: row.row_type == "player" and row.entity_id == 101,
        "Points",
    )

    try:
        repository.replace_game(incomplete)
    except LedgerValidationError as error:
        assert "missing nonzero points" in str(error)
    else:
        raise AssertionError("archived raw row missing provably nonzero points unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_missing_player_identity_evidence_rejects_the_complete_game_atomically(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'missing_identity.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    payload = _raw_observation_with_unknown_fields()
    leon_row = next(
        row for row in payload["stats"]["Home"]["FullGame"]
        if row.get("EntityId") == "2544"
    )
    leon_row.pop("EntityId", None)
    try:
        repository.replace_game(canonical_game_from_pbp(
            payload,
            event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
            participant_ids_by_team={
                1610612747: (2544, 203507),
                1610612759: (201935,),
            },
        ))
    except ProviderResponseError as error:
        assert "schema" in str(error)
    else:
        raise AssertionError("player row missing identity evidence unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_missing_player_name_evidence_rejects_the_complete_game_atomically(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'missing_name.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    payload = _raw_observation_with_unknown_fields()
    leon_row = next(
        row for row in payload["stats"]["Home"]["FullGame"]
        if row.get("EntityId") == "2544"
    )
    leon_row.pop("Name", None)
    try:
        repository.replace_game(canonical_game_from_pbp(
            payload,
            event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
            participant_ids_by_team={
                1610612747: (2544, 203507),
                1610612759: (201935,),
            },
        ))
    except ProviderResponseError as error:
        assert "schema" in str(error)
    else:
        raise AssertionError("player row missing name evidence unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_explicit_zero_required_count_remains_valid(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'explicit_zero.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    payload = _raw_observation_with_unknown_fields()
    # Reaves finished with no blocks and no offensive rebounds; spelling those
    # governed-zero counters explicitly stays consistent with the complete
    # team_results diagnostic (Home Blocks 1 and OffRebounds 3 reconcile), so
    # explicit zeroes remain valid evidence rather than a contradiction.
    payload["stats"]["Home"]["FullGame"][2]["Blocks"] = 0
    payload["stats"]["Home"]["FullGame"][2]["OffRebounds"] = 0
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    reaves = next(fact for fact in game.player_facts if fact.player_id == 203507)
    assert reaves.blocks == 0
    assert reaves.offensive_rebounds == 0
    assert repository.replace_game(game).inserted


_REMOVE_RAW_FIELD = object()


def _mutate_raw_field(game, predicate, field_name, value=_REMOVE_RAW_FIELD):
    """Remove or rewrite one provider field on matching archived rows.

    Omitting ``value`` drops the field from the row; passing a value (including
    ``None``) sets it.  Each touched row is rechecksummed and the game's
    raw evidence is rebuilt coherently.
    """
    rows = []
    for row in game.raw_rows:
        if predicate(row):
            payload = {
                key: item for key, item in row.payload.items() if key != field_name
            }
            if value is not _REMOVE_RAW_FIELD:
                payload[field_name] = value
            row = replace(
                row,
                payload=payload,
                checksum=canonical_row_checksum(payload),
                observed_fields=tuple(sorted(payload)),
            )
        rows.append(row)
    return replace(game, raw_rows=tuple(rows)).with_checksum()


def test_typed_player_mismatch_rejects_at_repository_boundary(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'player_mismatch.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    mismatch = replace(
        game,
        player_facts=(replace(game.player_facts[0], points=9), *game.player_facts[1:]),
    ).with_checksum()

    try:
        repository.replace_game(mismatch)
    except LedgerValidationError as error:
        assert "raw player evidence" in str(error)
    else:
        raise AssertionError("mixed raw/typed player evidence unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_typed_team_mismatch_rejects_at_repository_boundary(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'team_mismatch.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    mismatch = replace(
        game,
        team_facts=(replace(game.team_facts[0], points=17), game.team_facts[1]),
    ).with_checksum()

    try:
        repository.replace_game(mismatch)
    except LedgerValidationError as error:
        assert "raw team-summary evidence" in str(error)
    else:
        raise AssertionError("mixed raw/typed team evidence unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_mismatched_correction_preserves_prior_raw_and_typed_version(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mixed_correction.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    repository.replace_game(game)
    mismatch = replace(
        game,
        player_facts=(replace(game.player_facts[0], points=9), *game.player_facts[1:]),
    ).with_checksum()

    try:
        repository.replace_game(mismatch)
    except LedgerValidationError:
        pass
    else:
        raise AssertionError("mixed raw/typed correction unexpectedly replaced the game")
    stored = repository.get_game(game.game_id)
    assert stored is not None
    assert stored.player_facts[0].points == game.player_facts[0].points
    assert stored.checksum == game.checksum
    assert stored.raw_checksum == game.raw_checksum


def test_raw_player_row_count_contradiction_rejects_at_repository_boundary(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'raw_missing_player_count.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    # Assists has no independent arithmetic identity, so a sparse omission on
    # the archived row is a governed zero; a raw row that loses the count while
    # the typed fact retains a value therefore contradicts the retained
    # evidence and must reject atomically.
    incomplete = _mutate_raw_field(
        game,
        lambda row: row.row_type == "player" and row.entity_id == 101,
        "Assists",
    )

    try:
        repository.replace_game(incomplete)
    except LedgerValidationError as error:
        assert "raw player evidence" in str(error)
    else:
        raise AssertionError("raw player evidence contradicting the typed facts unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_raw_team_row_malformed_count_rejects_at_repository_boundary(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'raw_missing_team_count.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    # The team-summary row is sparse, so an omitted residual count is an
    # observed zero; a malformed value on the archived row is still malformed
    # evidence and rejects the complete game atomically.
    incomplete = _mutate_raw_field(
        game,
        lambda row: row.row_type == "team" and row.side == "Home",
        "Fouls",
        "not-a-count",
    )

    try:
        repository.replace_game(incomplete)
    except LedgerValidationError as error:
        assert "must be a non-negative integer" in str(error)
    else:
        raise AssertionError("raw team-summary evidence with a malformed count unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_raw_player_row_missing_minutes_rejects_at_repository_boundary(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'raw_missing_minutes.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    incomplete = _mutate_raw_field(
        game,
        lambda row: row.row_type == "player" and row.entity_id == 101,
        "Minutes",
    )

    try:
        repository.replace_game(incomplete)
    except LedgerValidationError as error:
        assert "minutes" in str(error)
    else:
        raise AssertionError("raw player evidence missing minutes unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_team_possessions_are_authority_and_not_player_additive(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'team_possessions.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    game = replace(
        game,
        team_facts=(
            replace(game.team_facts[0], possessions=97.5),
            replace(game.team_facts[1], possessions=50.0),
        ),
        player_facts=tuple(
            replace(player, possessions=30.0 if player.team_id == game.home_team_id else 24.0)
            for player in game.player_facts
        ),
    )
    game = replace(game, raw_rows=raw_rows_from_facts(game)).with_checksum()

    # Team possessions (97.5 Home / 50.0 Away) are a team-summary value that
    # is not part of the player-additive count vocabulary at all, so they may
    # legitimately differ from summed player possessions (60.0 / 48.0).
    result = repository.replace_game(game)
    assert result.inserted
    stored = repository.get_game(game.game_id)
    home = next(fact for fact in stored.team_facts if fact.team_id == game.home_team_id)
    assert home.possessions == 97.5
    assert sum(
        player.possessions or 0
        for player in stored.player_facts
        if player.team_id == game.home_team_id
    ) == 60.0


def test_team_summary_missing_minutes_rejects_at_repository_boundary(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'team_missing_minutes.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    incomplete = _mutate_raw_field(
        game,
        lambda row: row.row_type == "team" and row.side == "Home",
        "Minutes",
    )

    try:
        repository.replace_game(incomplete)
    except LedgerValidationError as error:
        assert "team-summary row is missing minutes evidence" in str(error)
    else:
        raise AssertionError("team-summary evidence missing minutes unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_team_summary_null_minutes_rejects_at_repository_boundary(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'team_null_minutes.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    incomplete = _mutate_raw_field(
        game,
        lambda row: row.row_type == "team" and row.side == "Home",
        "Minutes",
        None,
    )

    try:
        repository.replace_game(incomplete)
    except LedgerValidationError as error:
        assert "team-summary row is missing minutes evidence" in str(error)
    else:
        raise AssertionError("team-summary evidence with null minutes unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_team_summary_malformed_minutes_rejects_at_repository_boundary(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'team_malformed_minutes.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    incomplete = _mutate_raw_field(
        game,
        lambda row: row.row_type == "team" and row.side == "Home",
        "Minutes",
        "not-a-time",
    )

    try:
        repository.replace_game(incomplete)
    except LedgerValidationError as error:
        assert "minutes" in str(error)
    else:
        raise AssertionError("team-summary evidence with malformed minutes unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_team_summary_zero_minutes_remains_valid(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'team_zero_minutes.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    game = _mutate_raw_field(
        game,
        lambda row: row.row_type == "team" and row.side == "Home",
        "Minutes",
        "00:00",
    )

    assert repository.replace_game(game).inserted


def test_accepted_game_requires_raw_evidence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty_raw.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    payload = _raw_observation_with_unknown_fields()
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    stripped = replace(game, raw_rows=(), raw_checksum=None).with_checksum()
    try:
        repository.replace_game(stripped)
    except LedgerValidationError as error:
        assert "raw evidence" in str(error)
    else:
        raise AssertionError("empty raw evidence unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
