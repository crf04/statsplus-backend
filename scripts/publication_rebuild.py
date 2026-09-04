"""Drive and observe durable publication-format rebuilds.

Starting a rebuild through the admin route records the approved intent; this
executable is what makes it progress.  It is the same shape as
``scripts/ledger_refresh.py --compose-only``: a thin adapter that parses argv,
builds the real service, and calls one method.  Running it again after a
crash resumes whatever phase the durable row recorded, because the operation
lives in the database rather than in the worker.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import RuntimeSettings  # noqa: E402
from app.services.collection_control import ControlPlaneError  # noqa: E402
from app.services.traditional_opponent_publications import (  # noqa: E402
    TRADITIONAL_OPPONENT_FAMILY,
)
from app.services.traditional_opponent_rebuild import (  # noqa: E402
    TraditionalOpponentRebuildService,
)
from app.utils.db import get_engine  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 2
EXIT_UNAVAILABLE = 3

#: Every publication family that owns a durable rebuild service.
REBUILD_SERVICES = {
    TRADITIONAL_OPPONENT_FAMILY: TraditionalOpponentRebuildService,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument(
        "--family",
        default=TRADITIONAL_OPPONENT_FAMILY,
        choices=sorted(REBUILD_SERVICES),
    )
    parser.add_argument(
        "--owner",
        default="publication-rebuild-worker",
        help="lease owner recorded for this pass",
    )
    parser.add_argument(
        "--status",
        metavar="REBUILD_ID",
        help="report one rebuild's bounded status instead of running work",
    )
    parser.add_argument(
        "--limit", type=int, help="drive at most this many rebuilds"
    )
    return parser


def _service(args):
    settings = RuntimeSettings(
        environment="development",
        database={"url": args.database_url},
        auth={"firebase_admin_disabled": True},
    )
    return REBUILD_SERVICES[args.family](get_engine(settings))


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    service = _service(args)
    if args.status:
        try:
            print(json.dumps(service.status(args.status), sort_keys=True))
        except ControlPlaneError as error:
            print(json.dumps({"error_code": error.reason}, sort_keys=True))
            return EXIT_UNAVAILABLE
        return EXIT_OK
    finished = service.run_pending(owner=args.owner, limit=args.limit)
    print(json.dumps({
        "family": args.family,
        "driven": len(finished),
        "rebuilds": [
            {
                "rebuild_id": rebuild.rebuild_id,
                "state": rebuild.state,
                "error_code": rebuild.error_code,
            }
            for rebuild in finished
        ],
    }, sort_keys=True))
    return EXIT_FAILED if any(
        rebuild.state == "failed" for rebuild in finished
    ) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
