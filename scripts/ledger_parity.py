"""Approve or reject one durable ledger parity artifact with audit evidence."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402

from app.services.ledger_parity import LedgerParityArtifactRepository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_id")
    parser.add_argument("decision", choices=("approved", "rejected"))
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    artifact = LedgerParityArtifactRepository(create_engine(args.database_url)).adjudicate(
        args.artifact_id,
        decision=args.decision,
        actor=args.actor,
        reason=args.reason,
    )
    print(f"{artifact.artifact_id}: {artifact.decision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
