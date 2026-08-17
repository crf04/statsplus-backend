"""Run or adjudicate the bounded legacy-vs-ledger matchup dual-run.

The legacy provider-aggregate writer and the ledger materializer both produce
the disposable ``team_matchup_facts`` read model for the same governed season
and cutoff, but they write the same surface rows, so their outputs never
coexist in one stored snapshot.  This command compares the legacy
materializer's persisted output against the immutable ledger candidate
publications at the exact governed authority, and records durable parity
artifacts bound to the exact stream, window, cutoff, and publication.  It never
reads or advances a ``PublicationPointer``.

The ledger side is never accepted as JSON: it is derived from the candidate
``PublicationVersion`` rows named in ``publications.json``, re-verified against
their payload checksum and their manifest and immutable Event Catalog
authority.  The legacy side is read from the actual persisted
``team_matchup_facts`` rows the legacy materializer wrote.

    ./scripts/matchup_parity.py compare 2025-26 season \
        --database-url "$DATABASE_URL" \
        --cutoff 2025-11-01T00:00:00+00:00 \
        --publications-json publications.json

``publications.json`` maps each ledger-owned stream to its candidate
publication ID:

    {
      "traditional_opponent_season": "<publication id>",
      "assist_locations_season": "<publication id>"
    }
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
    MatchupParityRunner,
    StoredLegacyMatchupSource,
)
from app.services.team_matchup_repository import (  # noqa: E402
    TeamMatchupRepository,
)


def _aware_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SystemExit("--cutoff must be an aware timestamp")
    return parsed


def _load_publications(path: str) -> dict[str, str]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        str(stream_key): str(publication_id)
        for stream_key, publication_id in document.items()
    }


def _compare(args, engine) -> int:
    run_migrations(engine)
    cutoff = _aware_utc(args.cutoff)
    runner = MatchupParityRunner(
        engine,
        governance=ActiveManifestLedgerGovernanceReader(engine),
        legacy_source=StoredLegacyMatchupSource(TeamMatchupRepository(engine)),
    )
    reports = runner.run(
        args.season,
        args.window,
        cutoff=cutoff,
        publications=_load_publications(args.publications_json),
    )
    print(json.dumps(
        [report.to_dict() for report in reports], sort_keys=True, indent=2,
    ))
    return 0 if reports and all(report.exact for report in reports) else 2


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
    compare.add_argument("--cutoff", required=True, help="exact aware immutable manifest cutoff")
    compare.add_argument("--publications-json", required=True)

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
