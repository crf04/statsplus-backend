"""Load one season of player totals from NBA ``LeagueDashPlayerStats``.

Stores the regular-season Base totals of every player in ``player_season_stats``
(crf04/statsplus#277).  The season's rows are replaced in one transaction, so a
rerun leaves the same rows and a failed fetch writes nothing::

    python scripts/load_player_season_stats.py --season 2023-24 --database-url "$DATABASE_URL"

Prints a JSON summary and exits 0; any failure exits 1 with the prior rows intact.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--season", required=True, help="YYYY-YY season")
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args(argv)

    from app.config.settings import RuntimeSettings
    from app.errors import redact_public_value
    from app.migrations import run_migrations
    from app.providers.nba_stats import NBAStatsAdapter, validate_canonical_season
    from app.services.player_season_stats import PlayerSeasonStatsRepository, load_season
    from app.utils.db import get_engine

    try:
        season = validate_canonical_season(args.season)
    except ValueError as error:
        parser.error(str(error))

    settings = RuntimeSettings(
        environment="development",
        database={"url": args.database_url},
        auth={"firebase_admin_disabled": True},
    )
    engine = get_engine(settings)
    try:
        run_migrations(engine)
        rows = load_season(
            NBAStatsAdapter(settings=settings).fetch_player_totals_stats,
            PlayerSeasonStatsRepository(engine),
            season,
        )
    except Exception as error:
        print(
            json.dumps({"season": season, "error": redact_public_value(f"{type(error).__name__}: {error}")}),
            flush=True,
        )
        return 1
    finally:
        engine.dispose()
    print(json.dumps({"season": season, "players": rows}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
