"""Offline contract for the operator event mapping CLI."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, insert

from app.migrations import run_migrations
from app.models.event_catalog import EventCatalogEntry, EventCatalogRefresh
from scripts import event_mappings

SEASON = "2025-26"
LAL = 1610612747
SAS = 1610612759
TIP_OFF = datetime(2025, 10, 23, tzinfo=timezone.utc)
NOW = datetime(2025, 10, 22, 12, tzinfo=timezone.utc)


def _event_values(game_id: str, *, offset_hours: float = 0.0) -> dict[str, object]:
    return {
        "nba_game_id": game_id,
        "season": SEASON,
        "scheduled_at": TIP_OFF + timedelta(hours=offset_hours),
        "home_team_id": LAL,
        "home_team_name": "Los Angeles Lakers",
        "home_team_tricode": "LAL",
        "away_team_id": SAS,
        "away_team_name": "San Antonio Spurs",
        "away_team_tricode": "SAS",
        "status_text": "Scheduled",
        "status_code": 1,
        "postponed_status": None,
        "postponement_evidence": None,
        "classification": "Regular Season",
        "first_seen_at": NOW,
        "last_seen_at": NOW,
    }


def _seed_database(tmp_path, *, extra_games: int = 0) -> str:
    path = tmp_path / "events-cli.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    run_migrations(engine)
    rows = [_event_values("0022500001")] + [
        _event_values(f"002250001{index}", offset_hours=2 + index)
        for index in range(extra_games)
    ]
    with engine.begin() as connection:
        connection.execute(insert(EventCatalogEntry.__table__), rows)
        connection.execute(
            insert(EventCatalogRefresh.__table__).values(
                season=SEASON,
                last_attempt_at=datetime.now(timezone.utc),
                last_success_at=datetime.now(timezone.utc),
                event_count=len(rows),
            )
        )
    engine.dispose()
    return f"sqlite:///{path}"


def _run(capsys, *argv: str):
    assert event_mappings.main(list(argv)) == 0
    return json.loads(capsys.readouterr().out)


def _identity(database_url: str, command: str) -> list[str]:
    return [
        command,
        "--database-url",
        database_url,
        "--provider",
        "underdog",
        "--provider-event-id",
        "ud-1",
    ]


def _matchup_evidence() -> list[str]:
    return [
        "--label",
        "Lakers vs Spurs",
        "--starts-at",
        TIP_OFF.isoformat(),
        "--home-team-abbreviation",
        "LAL",
        "--away-team-abbreviation",
        "SAS",
    ]


def test_cli_dry_run_resolves_without_writing(tmp_path, capsys):
    database_url = _seed_database(tmp_path)

    dry_run = _run(
        capsys,
        *_identity(database_url, "dry-run"),
        "--season",
        SEASON,
        *_matchup_evidence(),
    )
    listing = _run(capsys, "list", "--database-url", database_url)

    assert dry_run["state"] == "auto"
    assert dry_run["canonical_event"]["nba_game_id"] == "0022500001"
    assert dry_run["provider_evidence"]["label"] == "Lakers vs Spurs"
    assert listing == {
        "mappings": [],
        "rejections": [],
        "unresolved": [],
        "conflicts": [],
    }


def test_cli_dry_run_reports_ambiguous_candidates(tmp_path, capsys):
    database_url = _seed_database(tmp_path, extra_games=1)

    dry_run = _run(
        capsys,
        *_identity(database_url, "dry-run"),
        "--season",
        SEASON,
        *_matchup_evidence(),
    )

    assert dry_run["state"] == "ambiguous"
    assert [candidate["nba_game_id"] for candidate in dry_run["candidates"]] == [
        "0022500001",
        "0022500010",
    ]


def test_cli_approves_overrides_and_records_history(tmp_path, capsys):
    database_url = _seed_database(tmp_path, extra_games=1)
    operator = ["--operator", "ops", "--reason", "reviewed the schedule"]

    approved = _run(
        capsys,
        *_identity(database_url, "approve"),
        "--season",
        SEASON,
        "--canonical-event-id",
        "0022500001",
        *operator,
        *_matchup_evidence(),
    )
    overridden = _run(
        capsys,
        *_identity(database_url, "override"),
        "--season",
        SEASON,
        "--canonical-event-id",
        "0022500010",
        *operator,
    )
    listing = _run(capsys, "list", "--database-url", database_url)
    history = _run(
        capsys,
        "history",
        "--database-url",
        database_url,
        "--provider",
        "underdog",
        "--provider-event-id",
        "ud-1",
    )

    assert approved["state"] == "manual_approved"
    assert approved["mapping"]["canonical_event_id"] == "0022500001"
    assert approved["mapping"]["provider_event_label"] == "Lakers vs Spurs"
    assert overridden["state"] == "manual_override"
    assert overridden["mapping"]["canonical_event_id"] == "0022500010"
    # An override with no supplied evidence retains what was approved.
    assert overridden["mapping"]["provider_home_team_abbreviation"] == "LAL"
    assert [mapping["mapping_state"] for mapping in listing["mappings"]] == [
        "manual_override"
    ]
    assert [decision["decision_state"] for decision in history] == [
        "manual_approved",
        "manual_override",
    ]
    assert history[0]["operator_id"] == "ops"
    assert history[0]["reason"] == "reviewed the schedule"


def test_cli_rejects_and_clears_an_identity(tmp_path, capsys):
    database_url = _seed_database(tmp_path)
    operator = ["--operator", "ops", "--reason", "not an NBA fixture"]

    rejected = _run(capsys, *_identity(database_url, "reject"), *operator)
    listing = _run(capsys, "list", "--database-url", database_url)
    cleared = _run(capsys, *_identity(database_url, "clear"), *operator)
    repeated = _run(capsys, *_identity(database_url, "clear"), *operator)

    assert rejected["state"] == "rejected"
    assert [rejection["provider_event_id"] for rejection in listing["rejections"]] == [
        "ud-1"
    ]
    assert cleared == {"cleared": True}
    assert repeated == {"cleared": False}


def test_cli_reports_the_unresolved_and_conflict_queues(tmp_path, capsys):
    database_url = _seed_database(tmp_path, extra_games=1)
    engine = create_engine(database_url)
    try:
        from app.providers.dfs import EventEvidence, TeamEvidence
        from app.services.event_mapping_repository import EventMappingRepository
        from app.services.event_resolver import EventResolver

        class _Catalog:
            def get_events(self, season):
                from app.services.event_catalog_repository import EventCatalogRepository

                return EventCatalogRepository(engine).list_events(season)

            def get_freshness(self, season):
                return {"season": season, "fresh": True, "last_success_at": NOW.isoformat()}

        repository = EventMappingRepository(engine)
        resolver = EventResolver(_Catalog(), mapping_repository=repository)
        evidence = EventEvidence(
            provider_id="ud-1",
            starts_at=TIP_OFF,
            home_team=TeamEvidence(abbreviation="LAL"),
            away_team=TeamEvidence(abbreviation="SAS"),
        )
        repository.record_resolution(resolver.resolve("underdog", evidence, SEASON))
    finally:
        engine.dispose()

    listing = _run(capsys, "list", "--database-url", database_url)

    assert [decision["decision_state"] for decision in listing["unresolved"]] == [
        "ambiguous"
    ]
    assert listing["conflicts"] == []


def test_cli_requires_a_writable_target(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as error:
        event_mappings.main(["list"])

    assert error.value.code == 2
    assert "--database-url" in capsys.readouterr().err


def test_cli_rejects_the_demo_database(capsys):
    with pytest.raises(SystemExit) as error:
        event_mappings.main(["list", "--database-url", "sqlite:///nba_play_types.db"])

    assert error.value.code == 2
    assert "read-only" in capsys.readouterr().err


def test_cli_reports_a_failure_without_leaking_the_password(capsys):
    database_url = "postgresql://ops:super-secret@example.invalid/stats"

    assert event_mappings.main(["list", "--database-url", database_url]) == 1

    error_output = capsys.readouterr().err
    assert "super-secret" not in error_output
    assert "postgresql://ops:***@example.invalid/stats" in error_output


def test_cli_never_contacts_a_provider(tmp_path):
    database_url = _seed_database(tmp_path)
    _engine, catalog, _repository, _resolver = event_mappings._build_services(
        database_url
    )
    try:
        with pytest.raises(RuntimeError, match="never contacts a provider"):
            catalog.refresh(SEASON)
    finally:
        _engine.dispose()
