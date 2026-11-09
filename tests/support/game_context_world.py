"""A migrated SQLite database with Event Catalog games and final ledger games (crf04/statsplus#466)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import create_engine, insert, update

from app.migrations import run_migrations
from app.models.canonical_game_ledger import CanonicalGameLedgerGame, CanonicalGameLedgerTeamFact
from app.models.event_catalog import EventCatalogEntry

SEEN_AT = datetime(2026, 10, 1, tzinfo=UTC)
_BOX_COUNTS = (
    "field_goals_made",
    "field_goals_attempted",
    "two_pointers_made",
    "two_pointers_attempted",
    "three_pointers_made",
    "three_pointers_attempted",
    "free_throws_made",
    "free_throws_attempted",
    "offensive_rebounds",
    "defensive_rebounds",
    "rebounds",
    "assists",
    "turnovers",
    "steals",
    "blocks",
    "personal_fouls",
)


def migrated_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'game-context.sqlite3'}")
    run_migrations(engine)
    return engine


def schedule(
    engine,
    game_id: str,
    scheduled_at: str,
    away: tuple[int, str],
    home: tuple[int, str],
    *,
    season: str,
    classification: str = "Regular Season",
    postponed: bool = False,
    postponement_evidence: str | None = None,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(EventCatalogEntry.__table__).values(
                nba_game_id=game_id,
                season=season,
                away_team_id=away[0],
                away_team_name=away[1],
                away_team_tricode=away[1],
                home_team_id=home[0],
                home_team_name=home[1],
                home_team_tricode=home[1],
                scheduled_at=datetime.fromisoformat(scheduled_at),
                status_text="Postponed" if postponed else "Scheduled",
                postponed_status="PPD" if postponed else None,
                postponement_evidence=postponement_evidence,
                classification=classification,
                first_seen_at=SEEN_AT,
                last_seen_at=SEEN_AT,
            )
        )


def postpone(engine, game_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(EventCatalogEntry.__table__)
            .where(EventCatalogEntry.nba_game_id == game_id)
            .values(status_text="Postponed", postponed_status="PPD")
        )


def final(
    engine,
    game_id: str,
    game_date: str,
    away: tuple[int, str, int, float | None],
    home: tuple[int, str, int, float | None],
    *,
    season: str,
    minutes: float = 48.0,
) -> None:
    """One final ledger game; each side is ``(team_id, tricode, points, offensive_possessions)``."""

    with engine.begin() as connection:
        connection.execute(
            insert(CanonicalGameLedgerGame.__table__).values(
                game_id=game_id,
                season=season,
                season_type="Regular Season",
                game_date=date.fromisoformat(game_date),
                home_team_id=home[0],
                home_team_tricode=home[1],
                away_team_id=away[0],
                away_team_tricode=away[1],
                status="final",
                source_observation_id=f"obs-{game_id}",
                checksum="0" * 64,
                retrieved_at=SEEN_AT,
                updated_at=SEEN_AT,
            )
        )
        for side, opponent, is_home in ((away, home, False), (home, away, True)):
            connection.execute(
                insert(CanonicalGameLedgerTeamFact.__table__).values(
                    game_id=game_id,
                    team_id=side[0],
                    team_tricode=side[1],
                    opponent_team_id=opponent[0],
                    opponent_team_tricode=opponent[1],
                    is_home=is_home,
                    points=side[2],
                    team_minutes=minutes,
                    offensive_possessions=side[3],
                    **dict.fromkeys(_BOX_COUNTS, 0),
                )
            )
