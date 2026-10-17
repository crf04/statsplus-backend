"""Activation evidence for the ``rebound_types`` team streams (backend #446).

``rebound_types_season`` and ``rebound_types_l15`` have no legacy source, so
there is nothing to run a parity artifact against.  Their evidence is an
identity: in every window, each team's six miss-type slices add up to the
player-credited ``OPP_REB`` count the ACTIVE ``traditional_opponent_*``
publication serves for the same team and games.

This script reads one rebound candidate and the active traditional publication
per window, decodes the candidate through the strict read-side decoder, and
checks, for every team:

* both publications cover the same 30 teams and the same game IDs;
* the six counts sum to the traditional ``counts.rebounds``;
* the six per-48 values sum to the traditional ``per48.rebounds``
  (relative tolerance 1e-6: per-48 values are derived floats).

It is read-only.  Exit status 0 means every team and window passed; 1 means a
failure (listed, with the team and the two counts) or a missing publication.

    python scripts/rebound_types_activation_evidence.py --season 2025-26 \\
        --database-url "$DATABASE_URL"

Paste the JSON on the parent issue (crf04/statsplus#234) as the activation
evidence, then ``POST /admin/collection/streams/<stream>/activate`` with the
``candidate_publication_id`` it reports.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models.collection_control import (  # noqa: E402
    PublicationPointer,
    PublicationVersion,
)
from app.services.database_first_activation import (  # noqa: E402
    PublicationPayloadError,
    decode_team_window,
)
from app.services.rebound_types_identity import (  # noqa: E402
    check_identity,
    payload_rows,
)

WINDOWS = ("season", "l15")


def _candidate(session: Session, stream_key: str, season: str, publication_id: str | None):
    statement = select(PublicationVersion).where(
        PublicationVersion.stream_key == stream_key,
        PublicationVersion.season == season,
        PublicationVersion.status.in_(("candidate", "active")),
    )
    if publication_id is not None:
        statement = statement.where(PublicationVersion.publication_id == publication_id)
    return session.scalars(
        statement.order_by(PublicationVersion.cutoff.desc(), PublicationVersion.version.desc()).limit(1)
    ).first()


def _active_traditional(session: Session, stream_key: str, season: str):
    pointer = session.get(PublicationPointer, stream_key)
    if pointer is None or not pointer.active_publication_id:
        return None
    version = session.get(PublicationVersion, pointer.active_publication_id)
    return version if version is not None and version.season == season else None


def build_report(session: Session, season: str, publication_ids: Mapping[str, str | None]) -> dict[str, Any]:
    windows: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []
    teams_checked = 0
    for window in WINDOWS:
        stream_key = f"rebound_types_{window}"
        traditional_key = f"traditional_opponent_{window}"
        candidate = _candidate(session, stream_key, season, publication_ids.get(window))
        traditional = _active_traditional(session, traditional_key, season)
        if candidate is None or traditional is None:
            failure = {
                "window": window,
                "failure": "publication_missing",
                "rebound_types_candidate": candidate is not None,
                "active_traditional": traditional is not None,
            }
            windows[window] = {"passed": False, "failures": [failure]}
            failures.append(failure)
            continue
        window_failures: list[dict[str, Any]] = []
        rebound_payload = json.loads(candidate.payload)
        try:
            decode_team_window(rebound_payload, stream_key=stream_key)
        except PublicationPayloadError as error:
            window_failures.append(
                {
                    "window": window,
                    "failure": "candidate_not_decodable",
                    "reason": str(error),
                }
            )
        checked, identity_failures = check_identity(
            payload_rows(rebound_payload), payload_rows(json.loads(traditional.payload)), window=window
        )
        window_failures.extend(identity_failures)
        teams_checked += checked
        windows[window] = {
            "candidate_publication_id": candidate.publication_id,
            "candidate_status": candidate.status,
            "candidate_checksum": candidate.checksum,
            "rebound_types_cutoff": candidate.cutoff.isoformat(),
            "traditional_publication_id": traditional.publication_id,
            "traditional_cutoff": traditional.cutoff.isoformat(),
            "teams_checked": checked,
            "passed": not window_failures,
            "failures": window_failures,
        }
        failures.extend(window_failures)
    return {
        "season": season,
        "teams_checked": teams_checked,
        "passed": not failures and all(windows.get(w, {}).get("passed") for w in WINDOWS),
        "windows": windows,
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--season", required=True)
    parser.add_argument("--database-url", help="SQLAlchemy URL (or set DATABASE_URL)")
    parser.add_argument("--season-publication-id", help="rebound_types_season candidate")
    parser.add_argument("--l15-publication-id", help="rebound_types_l15 candidate")
    args = parser.parse_args(argv)
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("a database target is required: pass --database-url or set DATABASE_URL")
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            report = build_report(
                session,
                args.season,
                {"season": args.season_publication_id, "l15": args.l15_publication_id},
            )
    finally:
        engine.dispose()
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
