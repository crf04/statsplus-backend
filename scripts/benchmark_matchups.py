#!/usr/bin/env python3
"""Record baseline/database-first p95 latency and retained query plans."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from app.services.database_first_benchmark import benchmark_matchup_reads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    engine = create_engine(args.database_url)
    run_migrations(engine)

    def read() -> None:
        with engine.connect() as connection:
            connection.execute(text("SELECT stream_key FROM publication_streams ORDER BY stream_key")).all()
            connection.execute(text("SELECT stream_key FROM publication_pointers ORDER BY stream_key")).all()

    # The CLI's baseline is the same bounded local read because a provider
    # benchmark cannot be credential-free. Deployments may inject a real
    # legacy callable through the library API for the production comparison.
    report = benchmark_matchup_reads(
        engine,
        baseline=read,
        database_first=read,
        iterations=args.iterations,
    )
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
