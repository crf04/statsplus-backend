"""Run the deterministic collection reconciliation/retention backstop."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from sqlalchemy import create_engine

from app.config.settings import current_nba_season
from app.services.collection_control import CollectionOperationsService, PublicationService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--season", default=current_nba_season())
    parser.add_argument("--cutoff")
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="only enqueue composition jobs at the cutoff; skip retention, pruning and alerting",
    )
    parser.add_argument(
        "--alerts-only",
        action="store_true",
        help=(
            "only re-evaluate cycles and open or resolve their lifecycle alerts; "
            "skip reconciliation, observation retention and history pruning"
        ),
    )
    parser.add_argument("--cycle-id", help="with --alerts-only, evaluate only this cycle")
    args = parser.parse_args(argv)
    if args.alerts_only and args.reconcile_only:
        parser.error("--alerts-only and --reconcile-only are mutually exclusive")
    if args.cycle_id and not args.alerts_only:
        parser.error("--cycle-id requires --alerts-only")
    engine = create_engine(args.database_url)
    if args.alerts_only:
        # No PublicationService: reconciliation and pruning are out of reach.
        print(CollectionOperationsService(engine).evaluate_cycle_alerts(cycle_id=args.cycle_id))
        return 0
    if not args.cutoff:
        parser.error("--cutoff is required unless --alerts-only")
    cutoff = datetime.fromisoformat(args.cutoff.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        parser.error("--cutoff must include a timezone")
    publication = PublicationService(engine)
    if args.reconcile_only:
        print({"jobs_enqueued": publication.reconcile_pending(season=args.season, cutoff=cutoff, limit=100)})
        return 0
    operations = CollectionOperationsService(engine, publication_service=publication)
    result = operations.run_maintenance(season=args.season, cutoff=cutoff, now=datetime.now(UTC))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
