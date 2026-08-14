"""Offline contract tests for the Canonical Game Ledger packet."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select

from app.migrations import run_migrations
from app.models.canonical_game_ledger import (
    CanonicalGameLedgerGame,
    CanonicalGameLedgerPlayerFact,
    CanonicalGameLedgerTeamFact,
    LedgerGameRowEvidence,
    LedgerPublication,
)
from app.models.collection_control import CompositionJob
from app.services.canonical_game_ledger import (
    CanonicalGameLedgerRepository,
    LedgerValidationError,
    LedgerPublicationRecord,
    LedgerSchemaUnavailable,
    canonical_game_from_pbp,
    canonical_row_checksum,
    raw_rows_from_facts,
)
from app.services.ledger_materialization import LedgerCorrectionQueue
from app.services.ledger_derivations import derive_assist_location_facts
from app.providers.pbp_game_logs import PBPGameLogAdapter


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
        {"EntityId": "0", "Name": "Team", "Minutes": "00:00", "Points": 16, "FGM": 6, "FGA": 12, "FG2M": 4, "FG2A": 8, "FG3M": 2, "FG3A": 4, "FtPoints": 2, "FTA": 4, "OffRebounds": 2, "DefRebounds": 1, "Rebounds": 3, "Assists": 6, "Turnovers": 2, "Steals": 2, "Blocks": 0, "Fouls": 2},
        {"EntityId": "101", "Name": "Home One", "Minutes": "20:00", "FG2M": 2, "FG2A": 4, "FG3M": 1, "FG3A": 2, "FtPoints": 1, "FTA": 2, "OffRebounds": 1, "DefRebounds": 2, "Assists": 3, "Turnovers": 1, "Steals": 1, "Blocks": 0, "Fouls": 1, "Points": 8},
        {"EntityId": "102", "Name": "Home Two", "Minutes": "20:00", "FG2M": 2, "FG2A": 4, "FG3M": 1, "FG3A": 2, "FtPoints": 1, "FTA": 2, "OffRebounds": 1, "DefRebounds": 2, "Assists": 3, "Turnovers": 1, "Steals": 1, "Blocks": 0, "Fouls": 1, "Points": 8}
      ]}, "Away": {"FullGame": [
        {"EntityId": "0", "Name": "Team", "Minutes": "00:00", "Points": 16, "FGM": 6, "FGA": 12, "FG2M": 4, "FG2A": 8, "FG3M": 2, "FG3A": 4, "FtPoints": 2, "FTA": 4, "OffRebounds": 3, "DefRebounds": 2, "Rebounds": 5, "Assists": 6, "Turnovers": 2, "Steals": 2, "Blocks": 0, "Fouls": 2},
        {"EntityId": "201", "Name": "Away One", "Minutes": "20:00", "FG2M": 2, "FG2A": 4, "FG3M": 1, "FG3A": 2, "FtPoints": 1, "FTA": 2, "OffRebounds": 1, "DefRebounds": 2, "Assists": 3, "Turnovers": 1, "Steals": 1, "Blocks": 0, "Fouls": 1, "Points": 8},
        {"EntityId": "202", "Name": "Away Two", "Minutes": "20:00", "FG2M": 2, "FG2A": 4, "FG3M": 1, "FG3A": 2, "FtPoints": 1, "FTA": 2, "OffRebounds": 1, "DefRebounds": 2, "Assists": 3, "Turnovers": 1, "Steals": 1, "Blocks": 0, "Fouls": 1, "Points": 8}
      ]}},
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
        assert "migration 024" in str(error)
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
                                "Points": 16,
                                "FGM": 6,
                                "FGA": 12,
                                "FG2M": 4,
                                "FG2A": 8,
                                "FG3M": 2,
                                "FG3A": 4,
                                "FtPoints": 2,
                                "FTA": 4,
                                "OffRebounds": 2,
                                "DefRebounds": 1,
                                "Rebounds": 3,
                                "Assists": 6,
                                "Turnovers": 2,
                                "Steals": 2,
                                "Blocks": 0,
                                "Fouls": 2,
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
                                "Points": 16,
                                "FGM": 6,
                                "FGA": 12,
                                "FG2M": 4,
                                "FG2A": 8,
                                "FG3M": 2,
                                "FG3A": 4,
                                "FtPoints": 2,
                                "FTA": 4,
                                "OffRebounds": 3,
                                "DefRebounds": 2,
                                "Rebounds": 5,
                                "Assists": 6,
                                "Turnovers": 2,
                                "Steals": 2,
                                "Blocks": 0,
                                "Fouls": 2,
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

    facts = derive_assist_location_facts((game,))
    assert len(facts) == len(game.player_facts)
    assert all(
        all(
            getattr(player, field) is not None
            for field in (
                "two_point_assists",
                "three_point_assists",
                "arc3_assists",
                "corner3_assists",
                "at_rim_assists",
                "short_mid_range_assists",
                "long_mid_range_assists",
            )
        )
        for player in game.player_facts
    )
    assert facts[0].two_point_assists == 2
    assert facts[0].corner3_assists == 0


def test_game_date_uses_nba_calendar_day_for_after_midnight_utc_tipoff():
    payload = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "pbp_stats" / "game_stats.valid.json")
        .read_text(encoding="utf-8")
    )
    payload.pop("team_results", None)
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


def _raw_observation_with_unknown_fields() -> dict[str, object]:
    payload = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "pbp_stats" / "game_stats.valid.json")
        .read_text(encoding="utf-8")
    )
    payload.pop("team_results", None)
    payload["stats"]["Home"]["FullGame"][0]["Possessions"] = 95.5
    payload["stats"]["Home"]["FullGame"][1]["UnknownAdditiveField"] = "future-proof"
    return payload


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
    # Every typed team fact comes from the team-summary row: the rebound
    # partition (2 OREB / 13 DREB) is the declared authority and includes team
    # rebounds no player is credited with (player sums are 1 OREB / 11 DREB),
    # while additive-equivalent facts (points, assists, ...) must equal player
    # sums.  Possessions is read from the team-summary row.
    assert home_team_fact.offensive_rebounds == 2
    assert home_team_fact.defensive_rebounds == 13
    assert home_team_fact.rebounds == 15
    assert home_team_fact.points == 40
    assert home_team_fact.assists == 13
    assert home_team_fact.possessions == 95.5


def test_team_summary_additive_mismatch_rejects_the_complete_game():
    payload = _raw_observation_with_unknown_fields()
    payload["stats"]["Home"]["FullGame"][0]["Points"] = 99
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
        raise AssertionError("contradictory team-summary evidence unexpectedly passed")


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
    corrected_payload["stats"]["Home"]["FullGame"][0]["Points"] = 41
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


def test_schema_drift_is_observed_in_field_set_metadata(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'schema_drift.sqlite3'}")
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
    assert "ProviderAddedField" in json.loads(row["observed_fields"])
    assert json.loads(row["payload"])["ProviderAddedField"] == 7


def test_missing_required_count_evidence_rejects_the_complete_game_atomically(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'missing_count.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    payload = _raw_observation_with_unknown_fields()
    leon_row = next(
        row for row in payload["stats"]["Home"]["FullGame"]
        if row.get("EntityId") == "2544"
    )
    leon_row.pop("Points", None)
    try:
        repository.replace_game(canonical_game_from_pbp(
            payload,
            event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
            participant_ids_by_team={
                1610612747: (2544, 203507),
                1610612759: (201935,),
            },
        ))
    except LedgerValidationError as error:
        assert "required" in str(error) and "Points" in str(error)
    else:
        raise AssertionError("missing required count evidence unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_explicit_zero_required_count_remains_valid(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'explicit_zero.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    payload = _raw_observation_with_unknown_fields()
    payload["stats"]["Home"]["FullGame"][1]["Fouls"] = 0
    payload["stats"]["Home"]["FullGame"][0]["Fouls"] = 1
    game = canonical_game_from_pbp(
        payload,
        event={**_event(), "scheduled_at": "2024-11-16T00:30:00+00:00"},
        participant_ids_by_team={
            1610612747: (2544, 203507),
            1610612759: (201935,),
        },
    )
    leon = next(fact for fact in game.player_facts if fact.player_id == 2544)
    assert leon.personal_fouls == 0
    assert repository.replace_game(game).inserted


def _without_raw_field(game, predicate, field_name):
    """Remove one provider field from matching archived rows and rechecksum."""
    rows = []
    for row in game.raw_rows:
        if predicate(row):
            payload = {key: value for key, value in row.payload.items() if key != field_name}
            row = replace(
                row,
                payload=payload,
                checksum=canonical_row_checksum(payload),
                observed_fields=tuple(sorted(payload)),
            )
        rows.append(row)
    return replace(game, raw_rows=tuple(rows)).with_checksum()


def _replace_raw_field(game, predicate, field_name, value):
    """Rewrite one provider field on matching archived rows and rechecksum."""
    rows = []
    for row in game.raw_rows:
        if predicate(row):
            payload = dict(row.payload)
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


def test_raw_player_row_missing_required_count_rejects_at_repository_boundary(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'raw_missing_player_count.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    incomplete = _without_raw_field(
        game,
        lambda row: row.row_type == "player" and row.entity_id == 101,
        "Points",
    )

    try:
        repository.replace_game(incomplete)
    except LedgerValidationError as error:
        assert "missing required Points count evidence" in str(error)
    else:
        raise AssertionError("raw player evidence missing a required count unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_raw_team_row_missing_required_count_rejects_at_repository_boundary(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'raw_missing_team_count.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    incomplete = _without_raw_field(
        game,
        lambda row: row.row_type == "team" and row.side == "Home",
        "Points",
    )

    try:
        repository.replace_game(incomplete)
    except LedgerValidationError as error:
        assert "team-summary row is missing required Points count evidence" in str(error)
    else:
        raise AssertionError("raw team-summary evidence missing a required count unexpectedly published")
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_raw_player_row_missing_minutes_rejects_at_repository_boundary(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'raw_missing_minutes.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    incomplete = _without_raw_field(
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

    # Team possessions (97.5 Home / 50.0 Away) are team-summary authority and
    # are deliberately NOT in ADDITIVE_EQUIVALENT_COUNT_FIELDS, so they may
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
    incomplete = _without_raw_field(
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
    incomplete = _replace_raw_field(
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
    incomplete = _replace_raw_field(
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
    game = _replace_raw_field(
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
