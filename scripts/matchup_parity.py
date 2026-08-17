"""Run or adjudicate the bounded legacy-vs-ledger matchup dual-run.

The legacy provider-aggregate writer and the ledger materializer both populate
the disposable ``team_matchup_facts`` read model.  Before fencing the legacy
writer and activating the ledger-owned traditional/assist streams, an operator
runs this command at one governed season and cutoff to prove the two
materializers selected the same 30-team roster and exact Season/L15 game sets
and produced the same contracted counts.

The command reads the stored snapshots directly: legacy facts are those with
provider ``nba_stats`` or ``pbp_stats``; ledger facts carry provider ``ledger``
and their exact game-id lineage.  The legacy materializer does not persist its
per-team game selection, so it is re-derived from the stored Event Catalog with
the same resolver the legacy writer uses.

Example:

    ./scripts/matchup_parity.py 2024-25 season --database-url "$DATABASE_URL" \\
        --as-of 2024-11-15 --record --stream-key traditional_opponent_season \\
        --publication-id "<id>" --payload-checksum "<sha256>"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402

from app.domain.nba_events import NBAGameStatus  # noqa: E402
from app.domain.utc import parse_utc_iso  # noqa: E402
from app.migrations import run_migrations  # noqa: E402
from app.services.event_catalog_repository import EventCatalogRepository  # noqa: E402
from app.services.ledger_parity import LedgerParityArtifactRepository  # noqa: E402
from app.services.matchup_parity import (  # noqa: E402
    compare_matchup_materializations,
)
from app.services.team_matchup_refresh import (  # noqa: E402
    EASTERN,
    TeamWindowBoundaryResolver,
    is_governed_event,
)
from app.services.team_matchup_repository import (  # noqa: E402
    TeamMatchupSnapshotScope,
    TeamMatchupRepository,
)

LEDGER_PROVIDERS = frozenset({"nba_stats", "pbp_stats"})


def _completed_governed_events(events, as_of):
    result = []
    for event in events:
        if not is_governed_event(event) or event.get("is_postponed"):
            continue
        status_text = str(event.get("status_text") or "")
        if (
            event.get("status_code") != NBAGameStatus.FINAL
            and not status_text.casefold().startswith("final")
        ):
            continue
        scheduled = parse_utc_iso(str(event["scheduled_at"])).astimezone(EASTERN).date()
        if scheduled <= as_of:
            result.append(event)
    return result


def _legacy_game_ids_by_team(events, window, as_of):
    if window == "l15":
        boundaries = TeamWindowBoundaryResolver().last_n(
            events, as_of=as_of, window_games=15
        )
        return {
            boundary.team_id: frozenset(boundary.game_ids)
            for boundary in boundaries.values()
        }
    by_team: dict[int, set[str]] = {}
    for event in _completed_governed_events(events, as_of):
        for team_id in (event["home_team_id"], event["away_team_id"]):
            by_team.setdefault(int(team_id), set()).add(str(event["nba_game_id"]))
    return {team_id: frozenset(game_ids) for team_id, game_ids in by_team.items()}


def _split(snapshot):
    legacy_facts = tuple(fact for fact in snapshot.facts if fact.provider in LEDGER_PROVIDERS)
    ledger_facts = tuple(fact for fact in snapshot.facts if fact.provider == "ledger")
    legacy_obs = tuple(
        observation for observation in snapshot.observations
        if observation.surface in {"traditional", "assist_locations"}
    )
    ledger_obs = tuple(
        observation for observation in snapshot.observations
        if observation.surface in {"traditional", "assist_locations"}
    )
    return legacy_facts, legacy_obs, ledger_facts, ledger_obs


def _compare(args, engine) -> int:
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    scope = TeamMatchupSnapshotScope(
        args.season, date.fromisoformat(args.as_of),
        None if args.window == "season" else 15,
    )
    snapshot = repository.get_snapshot(scope)
    legacy_facts, legacy_obs, ledger_facts, ledger_obs = _split(snapshot)
    events = EventCatalogRepository(engine).list_events(args.season)
    legacy_game_ids = _legacy_game_ids_by_team(events, args.window, date.fromisoformat(args.as_of))
    report = compare_matchup_materializations(
        legacy_facts, legacy_obs,
        ledger_facts, ledger_obs,
        season=args.season,
        window=args.window,
        as_of=date.fromisoformat(args.as_of),
        legacy_as_of=date.fromisoformat(args.as_of),
        expected_team_ids=legacy_game_ids.keys(),
        legacy_game_ids_by_team=legacy_game_ids,
    )
    print(json.dumps(report.to_dict(), sort_keys=True, indent=2))
    if args.record:
        if not args.stream_key or not args.publication_id or not args.payload_checksum:
            raise SystemExit("--record requires --stream-key, --publication-id, and --payload-checksum")
        from datetime import datetime

        artifact = LedgerParityArtifactRepository(engine).record_matchup_parity(
            args.stream_key,
            cutoff=datetime.fromisoformat(f"{args.as_of}T00:00:00+00:00"),
            report=report,
            publication_id=args.publication_id,
            payload_checksum=args.payload_checksum,
        )
        print(json.dumps({"artifact_id": artifact.artifact_id, "status": artifact.status}))
    return 0 if report.exact else 2


def _adjudicate(args, engine) -> int:
    repository = LedgerParityArtifactRepository(engine)
    artifact = repository.adjudicate(
        args.artifact_id,
        decision=args.decision,
        actor=args.actor,
        reason=args.reason,
    )
    print(f"{artifact.artifact_id}: {artifact.decision}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    compare = subparsers.add_parser("compare")
    compare.add_argument("season")
    compare.add_argument("window", choices=("season", "l15"))
    compare.add_argument("--database-url", required=True)
    compare.add_argument("--as-of", required=True)
    compare.add_argument("--record", action="store_true")
    compare.add_argument("--stream-key")
    compare.add_argument("--publication-id")
    compare.add_argument("--payload-checksum")

    adjudicate = subparsers.add_parser("adjudicate")
    adjudicate.add_argument("artifact_id")
    adjudicate.add_argument("decision", choices=("approved", "rejected"))
    adjudicate.add_argument("--database-url", required=True)
    adjudicate.add_argument("--actor", required=True)
    adjudicate.add_argument("--reason", required=True)

    args = parser.parse_args()
    if args.command == "adjudicate":
        return _adjudicate(args, create_engine(args.database_url))
    if args.command == "compare":
        return _compare(args, create_engine(args.database_url))
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
