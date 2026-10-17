"""Advance one database-only residential player-shooting refresh tick."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine

from app.services.player_shooting_refresh import PlayerShootingRefresh, active_manifest_season
from app.utils.db import _normalize_database_url, is_demo_database_url

logger = logging.getLogger(__name__)


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


def _precompute_target_backtests():
    """Fill the saved-Target Backtest result cache after the tick (#363).

    The tick is the scheduled caller that advances publication pointers, and
    this step only fills keys that are missing, so running it on every
    invocation covers this tick's compositions, an operator's or admin
    route's since the last one, and entries the TTL expired.  It never
    raises: its failure leaves the tick's output and exit status alone.
    """

    try:
        from app.config.settings import load_settings
        from app.dependencies import build_dependencies
        from app.services.target_backtest import log_precompute_skipped
        from app.utils.json_logging import configure_json_logging

        settings = load_settings()
        configure_json_logging(settings.log_level)
        # Checked before building dependencies, which ping Redis.
        if not settings.cache.enabled:
            log_precompute_skipped("ENABLE_CACHE=false")
            return
        if not settings.cache.target_backtest_enabled:
            log_precompute_skipped("TARGET_BACKTEST_CACHE_ENABLED=false")
            return
        build_dependencies(settings).target_backtest_service.precompute(now=datetime.now(UTC))
    except Exception:
        logger.exception("target_backtest_precompute failed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "season",
        nargs="?",
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
                engine,
                composer_factory=lambda: _composer(engine),
                clock=lambda: datetime.now(UTC),
            ).tick(season)
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    finally:
        engine.dispose()
        _precompute_target_backtests()


if __name__ == "__main__":
    raise SystemExit(main())
