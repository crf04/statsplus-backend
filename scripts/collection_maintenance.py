"""Run the deterministic collection reconciliation/retention backstop."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from sqlalchemy import create_engine

from app.config.settings import current_nba_season
from app.services.collection_control import CollectionOperationsService, PublicationService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--season", default=current_nba_season())
    parser.add_argument("--cutoff", required=True)
    args = parser.parse_args(argv)
    cutoff = datetime.fromisoformat(args.cutoff.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        parser.error("--cutoff must include a timezone")
    engine = create_engine(args.database_url)
    publication = PublicationService(engine)
    operations = CollectionOperationsService(engine, publication_service=publication)
    result = operations.run_maintenance(season=args.season, cutoff=cutoff, now=datetime.now(timezone.utc))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
