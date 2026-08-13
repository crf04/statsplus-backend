"""Offline contract tests for the Canonical Game Ledger packet."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, select

from app.migrations import run_migrations
from app.models.canonical_game_ledger import (
    CanonicalGameLedgerGame,
    CanonicalGameLedgerPlayerFact,
    CanonicalGameLedgerTeamFact,
    LedgerPublication,
)
from app.models.collection_control import CompositionJob
from app.services.canonical_game_ledger import (
    CanonicalGameLedgerRepository,
    LedgerValidationError,
    LedgerPublicationRecord,
    LedgerSchemaUnavailable,
    canonical_game_from_pbp,
)
from app.services.ledger_materialization import LedgerCorrectionQueue


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
        {"EntityId": "101", "Name": "Home One", "Minutes": "20:00", "FG2M": 2, "FG2A": 4, "FG3M": 1, "FG3A": 2, "FtPoints": 1, "FTA": 2, "OffRebounds": 1, "DefRebounds": 2, "Assists": 3, "Turnovers": 1, "Steals": 1, "Blocks": 0, "Fouls": 1, "Points": 8},
        {"EntityId": "102", "Name": "Home Two", "Minutes": "20:00", "FG2M": 2, "FG2A": 4, "FG3M": 1, "FG3A": 2, "FtPoints": 1, "FTA": 2, "OffRebounds": 1, "DefRebounds": 2, "Assists": 3, "Turnovers": 1, "Steals": 1, "Blocks": 0, "Fouls": 1, "Points": 8}
      ]}, "Away": {"FullGame": [
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
    ).with_checksum()
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
    ).with_checksum()
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
    ).with_checksum()

    repository.replace_game(corrected)

    with engine.connect() as connection:
        jobs = connection.execute(select(CompositionJob)).scalars().all()
    assert len(jobs) == 3


def test_full_game_preserves_optional_assist_locations_and_fences_envelope_identity():
    payload = json.loads(
        json.dumps(
            {
                "stats": {
                    "Home": {
                        "FullGame": [
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
