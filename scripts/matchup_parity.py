"""Run or adjudicate the bounded legacy-vs-ledger matchup dual-run.

The legacy provider-aggregate writer and the ledger materializer both produce
the disposable ``team_matchup_facts`` read model for the same governed season
and cutoff, but they write the same surface rows, so their outputs never
coexist in one stored snapshot.  This command compares two independently
produced materializations -- one per side -- against the exact immutable
governed authority (the checksummed Event Catalog publication bound to the
active manifest) and records durable parity artifacts bound to the exact
stream, window, cutoff, and publication.  It never reads or advances a
``PublicationPointer``.

Each side is supplied as a JSON document:

    {
      "season": "2025-26",
      "window": "season",
      "cutoff": "2025-11-01T00:00:00+00:00",
      "facts": [
        {"team_id": 1, "base": "traditional", "stat_key": "OPP_REB",
         "raw_value": 10, "denominator_value": 48.0, "denominator_unit": "minutes"}
      ],
      "observations": [{"surface": "traditional", "status": "available"}],
      "game_ids_by_team": {"1": ["0022500001"]}
    }

Example:

    ./scripts/matchup_parity.py compare 2025-26 season \\
        --database-url "$DATABASE_URL" --cutoff 2025-11-01T00:00:00+00:00 \\
        --legacy-json legacy.json --ledger-json ledger.json \\
        --publications-json publications.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402

from app.migrations import run_migrations  # noqa: E402
from app.services.ledger_parity import LedgerParityArtifactRepository  # noqa: E402
from app.services.ledger_runtime import (  # noqa: E402
    ActiveManifestLedgerGovernanceReader,
)
from app.services.matchup_parity import (  # noqa: E402
    MatchupMaterialization,
    MatchupParityRunner,
)
from app.services.team_matchup_repository import (  # noqa: E402
    TeamMatchupFact,
    TeamMatchupObservation,
)


def _aware_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SystemExit("--cutoff must be an aware timestamp")
    return parsed


def _load_materialization(path: str) -> MatchupMaterialization:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        season = document["season"]
        window = document["window"]
        cutoff = _aware_utc(document["cutoff"])
        facts = tuple(
            TeamMatchupFact(
                team_id=int(row["team_id"]),
                base=str(row["base"]),
                slice_key=str(row.get("slice_key", row["stat_key"])),
                stat_key=str(row["stat_key"]),
                raw_value=row["raw_value"],
                denominator_value=row.get("denominator_value"),
                denominator_unit=row.get("denominator_unit"),
                provider=str(row.get("provider", "recorded")),
                game_ids=tuple(str(game_id) for game_id in row.get("game_ids", ())),
            )
            for row in document["facts"]
        )
        observations = tuple(
            TeamMatchupObservation(
                surface=str(row["surface"]),
                status=str(row["status"]),
                unavailable_reason=row.get("unavailable_reason"),
            )
            for row in document.get("observations", ())
        )
        game_ids_by_team = {
            int(team_id): frozenset(str(game_id) for game_id in game_ids)
            for team_id, game_ids in document.get("game_ids_by_team", {}).items()
        }
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit(f"materialization JSON is invalid: {error}") from error
    return MatchupMaterialization(
        season=season,
        window=window,
        cutoff=cutoff,
        facts=facts,
        observations=observations,
        game_ids_by_team=game_ids_by_team,
    )


def _load_publications(path: str | None) -> dict[str, tuple[str, str]] | None:
    if path is None:
        return None
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    result = {}
    for stream_key, publication in document.items():
        result[stream_key] = (
            publication["publication_id"],
            publication["payload_checksum"],
        )
    return result


def _compare(args, engine) -> int:
    run_migrations(engine)
    legacy = _load_materialization(args.legacy_json)
    ledger = _load_materialization(args.ledger_json)
    if args.cutoff is not None:
        exact_cutoff = _aware_utc(args.cutoff)
        legacy = MatchupMaterialization(
            legacy.season, legacy.window, exact_cutoff,
            legacy.facts, legacy.observations, legacy.game_ids_by_team,
        )
        ledger = MatchupMaterialization(
            ledger.season, ledger.window, exact_cutoff,
            ledger.facts, ledger.observations, ledger.game_ids_by_team,
        )
    runner = MatchupParityRunner(
        engine,
        governance=ActiveManifestLedgerGovernanceReader(engine),
    )
    reports = runner.run(
        legacy,
        ledger,
        publications=_load_publications(args.publications_json),
    )
    print(json.dumps(
        [report.to_dict() for report in reports], sort_keys=True, indent=2,
    ))
    return 0 if all(report.exact for report in reports) else 2


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
    compare.add_argument("--cutoff", help="exact aware immutable manifest cutoff")
    compare.add_argument("--legacy-json", required=True)
    compare.add_argument("--ledger-json", required=True)
    compare.add_argument("--publications-json")

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
