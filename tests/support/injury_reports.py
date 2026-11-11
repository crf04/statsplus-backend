"""Stored official injury reports for the ``without`` team-window tests (crf04/statsplus#450)."""

from datetime import UTC, datetime

from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.services.injury_snapshot_repository import InjurySnapshotRepository, InjurySnapshotScope


def injury_reports(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'injury_reports.sqlite3'}")
    run_migrations(engine)
    return InjurySnapshotRepository(engine)


def store_reports(repository, season, games, *listed, status="Out"):
    """One stored report per game of ``{game_id: game day}``, each listing every ``(player_id, tricode)``."""

    repository.store_backfilled_report(
        source="nba_official",
        raw_payload=[],
        source_entries=[],
        retrieved_at=datetime(2026, 11, 10, tzinfo=UTC),
        games=[
            (
                InjurySnapshotScope(season, game_id),
                [
                    {
                        "canonical_player_id": player_id,
                        "tricode": team,
                        "canonical_status": status,
                        "source_player_name": f"Player {player_id}",
                        "game_date": str(day),
                    }
                    for player_id, team in listed
                ],
                0,
            )
            for game_id, day in games.items()
        ],
    )
