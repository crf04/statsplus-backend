#!/usr/bin/env python3
"""Record baseline/database-first p95 latency and retained query plans."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine

from app.config.settings import RuntimeSettings
from app.dependencies import build_dependencies
from app.migrations import run_migrations
from app.services.database_first_activation import DatabaseOnlyProviderGuard
from app.services.database_first_benchmark import benchmark_matchup_services


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
    game_id = str(fixture.get("game_id") or args.game_id)
    if not season or not game_id:
        raise SystemExit("benchmark fixture and --game-id require concrete season/game identity")
    seeded = fixture.get("seeded_fixture")
    required_sections = {
        "event_catalog",
        "player_pool",
        "player_game_logs",
        "player_diets",
        "team_matchups",
        "publications",
    }
    if (
        not isinstance(seeded, dict)
        or not required_sections <= set(seeded)
        or any(
            not isinstance(seeded[section], (dict, list, tuple))
            or not seeded[section]
            for section in required_sections
        )
    ):
        raise SystemExit(
            "benchmark fixture must contain non-empty event_catalog, player_pool, "
            "player_game_logs, player_diets, team_matchups, and publications sections"
        )

    settings = RuntimeSettings(
        environment="testing",
        database={"url": args.database_url},
        auth={"firebase_admin_disabled": True},
        cache={"enabled": False},
        features={"injury_report_enabled": False},
        nba={"current_season": season},
    )
    dependencies = build_dependencies(settings)
    service = dependencies.matchup_service
    # Injury Reports retain their existing service contract, but the benchmark
    # measures the statistical Matchups path and must not let a live injury
    # provider dominate its route timing.
    service.injuries = None
    if dependencies.event_catalog_service is not None:
        # The complete route must use the durable catalog read.  Any attempt
        # to refresh schedule/provider data turns the benchmark into a failed
        # evidence run instead of silently measuring a fallback.
        dependencies.event_catalog_service.provider = DatabaseOnlyProviderGuard(
            "benchmark-nba"
        )

    def baseline_read() -> dict:
        """Run the complete MatchupService against legacy fact repositories."""

        targets = [
            (service, "publication_reader"),
            (service.player_logs, "_publication_reader"),
            (getattr(service.player_diets, "repository", None), "_publication_reader"),
            (service.team_matchups, "_publication_reader"),
        ]
        previous = [(target, attribute, getattr(target, attribute, None)) for target, attribute in targets if target is not None]
        try:
            for target, attribute, _ in previous:
                setattr(target, attribute, None)
            return service.get_matchup(game_id=game_id)
        finally:
            for target, attribute, value in previous:
                setattr(target, attribute, value)

    def database_first_read() -> dict:
        """Run the complete activated database-first MatchupService route."""

        return service.get_matchup(game_id=game_id)

    report = benchmark_matchup_services(
        engine,
        baseline_route=baseline_read,
        database_first_route=database_first_read,
        season=season,
        game_id=game_id,
        iterations=args.iterations,
    )
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
