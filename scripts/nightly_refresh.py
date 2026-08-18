#!/usr/bin/env python3
"""Run durable current-season refreshes as one deployment-owned process unit."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402

from app.config.settings import load_settings  # noqa: E402
from app.domain.freshness import time_window_timedelta  # noqa: E402
from app.migrations import run_migrations  # noqa: E402
from app.providers.nba_stats import NBAStatsAdapter  # noqa: E402
from app.providers.pbp_game_logs import PBPGameLogAdapter  # noqa: E402
from app.providers.pbp_stats import PBPStatsAdapter  # noqa: E402
from app.services.athlete_catalog_service import AthleteCatalogService  # noqa: E402
from app.services.data_service import DataService  # noqa: E402
from app.services.database_first_activation import LegacyWriteFence  # noqa: E402
from app.services.event_catalog_service import EventCatalogService  # noqa: E402
from app.services.player_game_log_ingest import PlayerGameLogIngestService  # noqa: E402
from app.services.player_game_log_repository import PlayerGameLogRepository  # noqa: E402
from app.services.player_diet import PlayerDietService  # noqa: E402
from app.services.statistic_catalog import StatisticCatalog  # noqa: E402
from app.services.stats_freshness_repository import (  # noqa: E402
    StatsFreshnessRepository,
)
from app.services.team_matchup_refresh import TeamMatchupRefreshService  # noqa: E402
from app.services.team_matchup_repository import TeamMatchupRepository  # noqa: E402
from app.utils.db import _normalize_database_url, is_demo_database_url  # noqa: E402


def run_nightly_refresh(
    *,
    refresh_stats: Callable[[], Any],
    refresh_schedule: Callable[[], Any],
    refresh_athlete_catalog: Callable[[], Any],
    refresh_player_game_logs: Callable[[], Any],
    refresh_player_diets: Callable[[], Any],
    refresh_team_matchups: Callable[[], Any],
) -> int:
    """Run the complete unit, retrying from its first step exactly once."""

    return _run_refresh_steps(
        (
            ("stats", refresh_stats),
            ("schedule", refresh_schedule),
            ("athlete catalog", refresh_athlete_catalog),
            ("player game logs", refresh_player_game_logs),
            ("player diets", refresh_player_diets),
            ("team matchups", refresh_team_matchups),
        )
    )


def run_hosted_refresh(*, refresh_player_game_logs: Callable[[], Any]) -> int:
    """Run only the PBP-backed refresh that is safe from hosted egress."""

    return _run_refresh_steps((("player game logs", refresh_player_game_logs),))


def _run_refresh_steps(
    steps: tuple[tuple[str, Callable[[], Any]], ...],
) -> int:
    """Run one ordered refresh unit, retrying from its first step once."""

    for attempt in range(1, 3):
        succeeded = True
        for step, refresh in steps:
            failed_step = step
            try:
                if refresh() is False:
                    succeeded = False
                    break
            except Exception:
                succeeded = False
                break
        if succeeded:
            return 0
        disposition = "retrying" if attempt == 1 else "no retries remain"
        print(
            f"Nightly Refresh attempt {attempt} failed during "
            f"{failed_step} refresh; {disposition}.",
            file=sys.stderr,
        )
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the StatsPlus Nightly Refresh.")
    parser.add_argument(
        "--database-url", help="SQLAlchemy database URL (or set DATABASE_URL)"
    )
    parser.add_argument(
        "--hosted-only",
        action="store_true",
        help=(
            "refresh only PBP-backed player game logs; never contact NBA Stats "
            "from hosted infrastructure"
        ),
    )
    return parser


def _run(database_url: str, *, hosted_only: bool = False) -> int:
    """Assemble and execute the command against one writable database."""
    settings = load_settings(overrides={"DATABASE_URL": database_url})
    engine = create_engine(_normalize_database_url(database_url))
    try:
        run_migrations(engine)
        # Hosted Railway egress cannot reliably reach stats.nba.com.  The
        # hosted-only command therefore injects an inert catalog provider and
        # uses the catalog services strictly through their database read APIs.
        # Any accidental refresh call fails at the call site instead of making
        # an upstream request.
        provider = object() if hosted_only else NBAStatsAdapter(settings=settings)
        event_service = EventCatalogService(
            engine, settings=settings, nba_stats_provider=provider
        )
        athlete_service = AthleteCatalogService(
            engine, settings=settings, nba_stats_provider=provider
        )
        player_game_log_repository = PlayerGameLogRepository(
            engine,
            statistic_catalog=StatisticCatalog.load_default(),
            stats_surface_season=settings.nba.current_season,
            stats_surface_max_age=time_window_timedelta(
                settings.catalog.player_game_log_max_age_hours,
                unit_seconds=3600,
                field="PLAYER_GAME_LOG_MAX_AGE_HOURS",
            ),
        )
        player_game_log_ingest_service = PlayerGameLogIngestService(
            pbp_provider=PBPGameLogAdapter(settings=settings),
            repository=player_game_log_repository,
            athlete_catalog=athlete_service,
            event_catalog=event_service,
            minimum_active_players_per_team_game=(
                settings.catalog.player_game_log_min_active_players_per_team_game
            ),
            reconciliation_days=(
                settings.catalog.player_game_log_reconciliation_days
            ),
        )
        if hosted_only:
            return run_hosted_refresh(
                refresh_player_game_logs=lambda: player_game_log_ingest_service.refresh(
                    settings.nba.current_season
                )
            )

        pbp_provider = PBPStatsAdapter(settings=settings)
        stats_freshness = StatsFreshnessRepository(engine)
        write_fence = LegacyWriteFence(engine)
        data_service = DataService(
            engine,
            settings=settings,
            pbp_provider=pbp_provider,
            nba_stats_provider=provider,
            stats_freshness=stats_freshness,
            write_fence=write_fence,
        )
        player_diet_service = PlayerDietService(
            engine,
            athlete_catalog=athlete_service,
            nba_stats_provider=provider,
            pbp_stats_provider=pbp_provider,
        )
        team_matchup_service = TeamMatchupRefreshService(
            repository=TeamMatchupRepository(engine, write_fence=write_fence),
            event_catalog=event_service,
            nba_stats_provider=provider,
            pbp_stats_provider=pbp_provider,
        )
        return run_nightly_refresh(
            refresh_stats=data_service.update_all_data,
            refresh_schedule=lambda: event_service.refresh(
                settings.nba.current_season
            ),
            refresh_athlete_catalog=lambda: athlete_service.refresh_season(
                settings.nba.current_season
            ).status
            == "succeeded",
            refresh_player_game_logs=lambda: player_game_log_ingest_service.refresh(
                settings.nba.current_season
            ),
            refresh_player_diets=lambda: player_diet_service.refresh(
                settings.nba.current_season
            ),
            refresh_team_matchups=lambda: team_matchup_service.refresh(
                settings.nba.current_season
            ),
        )
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        parser.error(
            "a refresh target is required: pass --database-url or set DATABASE_URL"
        )
    if is_demo_database_url(database_url):
        parser.error("the tracked nba_play_types.db is a read-only demo database")

    result = (
        _run(database_url, hosted_only=True)
        if args.hosted_only
        else _run(database_url)
    )
    print(json.dumps({"status": "succeeded" if result == 0 else "failed"}))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
