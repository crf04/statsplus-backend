"""Run a bounded Canonical Game Ledger refresh and optional composition."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import RuntimeSettings  # noqa: E402
from app.dependencies import build_dependencies  # noqa: E402
from app.migrations import run_migrations  # noqa: E402
from app.services.ledger_runtime import LedgerRuntime  # noqa: E402
from app.utils.db import get_engine  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("season")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--max-games", type=int)
    parser.add_argument("--historical-repair", action="store_true")
    parser.add_argument("--compose", action="store_true")
    args = parser.parse_args()
    settings = RuntimeSettings(
        environment="development",
        database={"url": args.database_url},
        auth={"firebase_admin_disabled": True},
    )
    run_migrations(get_engine(settings))
    dependencies = build_dependencies(settings)
    runtime = LedgerRuntime(
        backfill=dependencies.ledger_backfill_service,
        repository=dependencies.canonical_game_ledger_repository,
        materialization=dependencies.ledger_materialization_service,
    )
    result = runtime.refresh(
        args.season,
        max_games=args.max_games,
        historical_repair=args.historical_repair,
    )
    composed = runtime.compose_queued(args.season) if args.compose and result.complete else 0
    print(json.dumps({**asdict(result), "composed_jobs": composed}, default=str, sort_keys=True))
    return 0 if result.complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
