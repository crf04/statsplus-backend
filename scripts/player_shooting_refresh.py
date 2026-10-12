"""Advance one database-only residential player-shooting refresh tick."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from app.services.player_shooting_refresh import PlayerShootingRefresh, active_manifest_season
from app.utils.db import _normalize_database_url, is_demo_database_url


def _composer(engine):
    # Providers are constructed only when durable authorized jobs exist;
    # this path calls compose_queued, never refresh or provider collection.
    from app.config.settings import load_settings
    from app.dependencies import build_dependencies
    from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader, LedgerRuntime
    dependencies = build_dependencies(load_settings())
    return LedgerRuntime(
        backfill=dependencies.ledger_backfill_service,
        repository=dependencies.canonical_game_ledger_repository,
        materialization=dependencies.ledger_materialization_service,
        governance=ActiveManifestLedgerGovernanceReader(engine),
        matchup_materialization=dependencies.ledger_matchup_materialization_service,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "season", nargs="?",
        help="season to tick; defaults to the active collection manifest's season",
    )
    args = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url or is_demo_database_url(database_url):
        parser.error("DATABASE_URL must identify a writable application database")
    engine = create_engine(_normalize_database_url(database_url))
    try:
        season = args.season or active_manifest_season(engine)
        if season is None:
            result = {"state": "no_active_manifest", "composed_jobs": 0}
        else:
            result = PlayerShootingRefresh(
                engine, composer_factory=lambda: _composer(engine),
                clock=lambda: datetime.now(timezone.utc),
            ).tick(season)
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
