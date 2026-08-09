"""Offline contract for the operator athlete mapping CLI."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, insert

from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog
from scripts import athlete_mappings


def _seed_database(tmp_path):
    path = tmp_path / "cli.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(AthleteCatalog.__table__).values(
                season="2024-25",
                player_id=15,
                display_name="Nikola Jokić",
                roster_status="active",
                is_active=True,
                is_active_for_season=True,
                team_id=1610612743,
                team_name="Denver Nuggets",
                team_abbreviation="DEN",
                published_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
            )
        )
    return f"sqlite:///{path}"


def test_cli_dry_run_and_audited_actions(tmp_path, capsys):
    database_url = _seed_database(tmp_path)

    assert athlete_mappings.main(
        [
            "dry-run",
            "--database-url",
            database_url,
            "--provider",
            "prizepicks",
            "--provider-athlete-id",
            "pp-15",
            "--season",
            "2024-25",
            "--name",
            "Nikola Jokic",
        ]
    ) == 0
    assert '"state": "auto"' in capsys.readouterr().out

    assert athlete_mappings.main(
        [
            "approve",
            "--database-url",
            database_url,
            "--provider",
            "prizepicks",
            "--provider-athlete-id",
            "pp-15",
            "--season",
            "2024-25",
            "--canonical-player-id",
            "15",
            "--operator",
            "ops@example.com",
            "--reason",
            "verified source",
        ]
    ) == 0
    assert '"manual_approved"' in capsys.readouterr().out

    assert athlete_mappings.main(
        [
            "history",
            "--database-url",
            database_url,
            "--provider",
            "prizepicks",
            "--provider-athlete-id",
            "pp-15",
        ]
    ) == 0
    assert '"operator_id": "ops@example.com"' in capsys.readouterr().out
