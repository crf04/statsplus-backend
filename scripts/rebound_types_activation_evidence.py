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
import math
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.domain.rebound_types import REBOUND_TYPE_WIRE_NAMES  # noqa: E402
from app.models.collection_control import (  # noqa: E402
    PublicationPointer,
    PublicationVersion,
)
from app.services.database_first_activation import (  # noqa: E402
    PublicationPayloadError,
    decode_team_window,
)

WINDOWS = ("season", "l15")
LEAGUE_SIZE = 30
PER48_TOLERANCE = 1e-6


def _rows(payload: Any) -> list[Mapping[str, Any]]:
    rows = payload.get("rows") if isinstance(payload, Mapping) else payload
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def check_identity(
    rebound_rows: Sequence[Mapping[str, Any]],
    traditional_rows: Sequence[Mapping[str, Any]],
    *,
    window: str,
) -> tuple[int, list[dict[str, Any]]]:
    """Return ``(teams_checked, failures)`` for one window's two payloads."""

    failures: list[dict[str, Any]] = []
    rebound_by_team = {row.get("team_id"): row for row in rebound_rows}
    traditional_by_team = {row.get("team_id"): row for row in traditional_rows}
    if len(rebound_by_team) != LEAGUE_SIZE or len(traditional_by_team) != LEAGUE_SIZE:
        failures.append({
            "window": window,
            "failure": "league_not_30_teams",
            "rebound_types_teams": len(rebound_by_team),
            "traditional_teams": len(traditional_by_team),
        })
    if set(rebound_by_team) != set(traditional_by_team):
        failures.append({
            "window": window,
            "failure": "team_sets_differ",
            "only_rebound_types": sorted(set(rebound_by_team) - set(traditional_by_team)),
            "only_traditional": sorted(set(traditional_by_team) - set(rebound_by_team)),
        })
    checked = 0
    for team_id in sorted(set(rebound_by_team) & set(traditional_by_team)):
        rebound = rebound_by_team[team_id]
        traditional = traditional_by_team[team_id]
        checked += 1
        if sorted(rebound.get("game_ids") or ()) != sorted(traditional.get("game_ids") or ()):
            failures.append({
                "window": window,
                "team_id": team_id,
                "failure": "game_set_mismatch",
                "rebound_types_games": len(rebound.get("game_ids") or ()),
                "traditional_games": len(traditional.get("game_ids") or ()),
            })
            continue
        try:
            slice_sum = sum(rebound["counts"][name] for name in REBOUND_TYPE_WIRE_NAMES)
            opp_reb = traditional["counts"]["rebounds"]
            per48_sum = sum(rebound["per48"][name] for name in REBOUND_TYPE_WIRE_NAMES)
            traditional_per48 = traditional["per48"]["rebounds"]
        except (KeyError, TypeError):
            failures.append({
                "window": window,
                "team_id": team_id,
                "failure": "row_malformed",
            })
            continue
        if slice_sum != opp_reb:
            failures.append({
                "window": window,
                "team_id": team_id,
                "failure": "slices_do_not_add_up_to_opp_reb",
                "slice_sum": slice_sum,
                "opp_reb": opp_reb,
            })
        if not math.isclose(per48_sum, traditional_per48, rel_tol=PER48_TOLERANCE, abs_tol=PER48_TOLERANCE):
            failures.append({
                "window": window,
                "team_id": team_id,
                "failure": "per48_does_not_add_up_to_opp_reb",
                "slice_sum_per48": per48_sum,
                "opp_reb_per48": traditional_per48,
            })
    return checked, failures


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


def build_report(
    session: Session, season: str, publication_ids: Mapping[str, str | None]
) -> dict[str, Any]:
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
            window_failures.append({
                "window": window,
                "failure": "candidate_not_decodable",
                "reason": str(error),
            })
        checked, identity_failures = check_identity(
            _rows(rebound_payload), _rows(json.loads(traditional.payload)), window=window
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
