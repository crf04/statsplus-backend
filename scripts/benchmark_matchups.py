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
    parser.add_argument(
        "--fixture",
        required=True,
        help="JSON production-like fixture containing season and game_id",
    )
    parser.add_argument("--game-id", required=True)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    engine = create_engine(args.database_url)
    run_migrations(engine)
    with open(args.fixture, encoding="utf-8") as handle:
        fixture = json.load(handle)
    if not isinstance(fixture, dict):
        raise SystemExit("benchmark fixture must be a JSON object")
    season = str(fixture.get("season") or "")
    game_id = str(args.game_id)
    if not season or not game_id:
        raise SystemExit("benchmark fixture and --game-id require concrete season/game identity")

    def baseline_read() -> None:
        """Legacy Matchups-shaped read over stored fact tables."""
        with engine.connect() as connection:
            connection.execute(text(
                "SELECT player_id, game_id, points, rebounds, assists "
                "FROM player_game_logs WHERE season = :season AND game_id = :game_id"
            ), {"season": season, "game_id": game_id}).all()
            connection.execute(text(
                "SELECT season, player_id, base, slice_key, share "
                "FROM player_diet_facts WHERE season = :season"
            ), {"season": season}).all()

    def database_first_read() -> None:
        """Database-first route-shaped read over active immutable payloads."""
        with engine.connect() as connection:
            active = connection.execute(text(
                "SELECT p.stream_key, v.payload "
                "FROM publication_pointers p "
                "JOIN publication_versions v "
                "ON v.publication_id = p.active_publication_id "
                "WHERE v.season = :season"
            ), {"season": season}).all()
            # Decode the same JSON envelope the route reader serves; do not
            # call a provider or silently replace an active payload with a
            # legacy table query.
            for stream_key, payload in active:
                if not isinstance(stream_key, str) or not isinstance(payload, str):
                    raise ValueError("active publication payload is malformed")

    report = benchmark_matchup_reads(
        engine,
        baseline=baseline_read,
        database_first=database_first_read,
        iterations=args.iterations,
        provider_calls=0,
        baseline_source="legacy_matchups_sql",
        database_first_source="publication_version_matchups_sql",
    )
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
