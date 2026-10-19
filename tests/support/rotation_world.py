"""A small stored-rotation world for the rotation route and service tests.

Builds ``GameRotation`` values directly from minutes so tests state the data a
reader would check by hand, and stores them through the real repository on a
migrated SQLite database.
"""

from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import create_engine

from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
from app.migrations import run_migrations
from app.services.game_log_frame import derive_game_log_frame
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


class StoredLogs:
    """The game-service seam ``without`` reads stats through: batched logs under a snapshot.

    ``add`` takes one game-log row per call; ``batches`` records each batched
    read as ``(player_ids, season, snapshot)``.
    """

    snapshot = object()

    def __init__(self):
        self.rows = {}
        self.batches = []

    def add(self, player_id, team, game_id, date, *, minutes, pts=0, reb=0, ast=0, fg3m=0, fga=0):
        self.rows.setdefault(player_id, []).append(
            {
                "PLAYER_ID": player_id,
                "PLAYER_NAME": f"Player {player_id}",
                "GAME_ID": game_id,
                "GAME_DATE": date,
                "MATCHUP": f"{team} @ XXX",
                "TEAM_ID": TEAM_IDS[team],
                "TEAM_ABBREVIATION": team,
                "MIN": minutes,
                "PTS": pts,
                "REB": reb,
                "AST": ast,
                "FGM": 0,
                "FGA": fga,
                "FG3M": fg3m,
                "FG3A": 0,
                "FTM": 0,
                "FTA": 0,
                "OREB": 0,
                "DREB": 0,
                "TOV": 0,
                "STL": 0,
                "BLK": 0,
                "PF": 0,
            }
        )

    def capture_publication_snapshot(self, season):
        return self.snapshot

    def get_players_logs_by_id(self, player_ids, season, *, publication_snapshot=None):
        ids = sorted(player_ids)
        self.batches.append((ids, season, publication_snapshot))
        return {pid: self.get_player_logs(pid, season) for pid in ids}

    # The stored game-log source seam a real ``GameService`` reads through.
    def get_players_logs(self, player_ids, season, *, publication_snapshot=None):
        return self.get_players_logs_by_id(player_ids, season, publication_snapshot=publication_snapshot)

    def get_player_logs(self, player_id, season, *, publication_snapshot=None):
        rows = self.rows.get(player_id)
        if not rows:
            return pd.DataFrame(columns=["GAME_ID"])
        return derive_game_log_frame(pd.DataFrame(rows))


def service_for(repository, catalog, default_season=SEASON, logs=None):
    return RotationService(
        repository, catalog, lambda: default_season, logs if logs is not None else StoredLogs()
    )


def store(repository, *rotations, season=SEASON):
    for rotation in rotations:
        repository.replace_game(season, rotation, COLLECTED_AT)
