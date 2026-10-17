"""A small stored-rotation world for the rotation route and service tests.

Builds ``GameRotation`` values directly from minutes so tests state the data a
reader would check by hand, and stores them through the real repository on a
migrated SQLite database.
"""

from datetime import UTC, datetime

from sqlalchemy import create_engine

from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
from app.migrations import run_migrations
from app.services.rotation_derivation import GameRotation, PlayerRotation, Stint
from app.services.rotation_repository import RotationRepository
from app.services.rotation_service import RotationService

SEASON = "2025-26"
COLLECTED_AT = datetime(2026, 4, 13, 10, 0, tzinfo=UTC)
TEAM_IDS = NBA_TEAM_TRICODE_TO_ID


def cs(minutes):
    return round(minutes * 6000)


def clock(minutes_seconds):
    """``"2:36"`` as the feed's clock-remaining centiseconds."""

    minutes, seconds = minutes_seconds.split(":")
    return (int(minutes) * 60 + int(seconds)) * 100


def line(
    player_id,
    name,
    team,
    *,
    minutes=0.0,
    started=False,
    closed=False,
    periods=(0, 0, 0, 0, 0),
    stints=(),
):
    """One player line; ``stints`` are ``(in_period, "12:00", out_period, "2:36", minutes)``."""

    return PlayerRotation(
        player_id=player_id,
        name=name,
        team_tricode=team,
        started=started,
        minutes_cs=cs(minutes),
        closed=closed,
        period_cs=dict(zip(("Q1", "Q2", "Q3", "Q4", "OT"), (cs(m) for m in periods), strict=False)),
        stints=tuple(Stint(ip, clock(ic), op, clock(oc), cs(m)) for ip, ic, op, oc, m in stints),
    )


def game(game_id, date, home, away, players, periods=4):
    return GameRotation(
        game_id=game_id,
        game_date=date,
        home_tricode=home,
        away_tricode=away,
        periods=periods,
        players=tuple(players),
    )


class Catalog:
    """The Athlete Catalog read the service uses: season rows by display name."""

    def __init__(self, *athletes):
        self.athletes = athletes

    def get_catalog(self, season, *, active_only=False):
        return [
            {
                "season": season,
                "player_id": player_id,
                "display_name": name,
                "roster_status": "active",
                "is_active": True,
                "is_active_for_season": True,
                "team_id": TEAM_IDS.get(team),
                "team_name": team,
                "team_abbreviation": team,
            }
            for player_id, name, team in self.athletes
        ]


def migrated_repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rotations.sqlite3'}")
    run_migrations(engine)
    return RotationRepository(engine)


def service_for(repository, catalog, default_season=SEASON):
    return RotationService(repository, catalog, lambda: default_season)


def store(repository, *rotations, season=SEASON):
    for rotation in rotations:
        repository.replace_game(season, rotation, COLLECTED_AT)
