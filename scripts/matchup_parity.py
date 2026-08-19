#!/usr/bin/env python3
"""CLI adapter for the bounded matchup parity application module."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402

from app.services import matchup_parity_operation as _operation  # noqa: E402


# Compatibility exports for focused callers while business policy remains in
# the application module and this file owns only argv/engine adaptation.
ALL_REQUIRED_STREAMS = _operation.ALL_REQUIRED_STREAMS
InvalidEvidenceError = _operation.InvalidEvidenceError
_aware_utc = _operation._aware_utc
Per36DiagnosticCaptureRepository = _operation.Per36DiagnosticCaptureRepository
_invalid_summary = _operation._invalid_summary
_manifest_preflight = _operation._manifest_preflight
_overall_status = _operation._overall_status
_print_protected_game_ids = _operation._print_protected_game_ids
_publish_summary = _operation._publish_summary
_required_streams = _operation._required_streams
_sanitize_matchup_report = _operation._sanitize_matchup_report
_stage_summary = _operation._stage_summary


def _capture_per36(args, engine):
    original_repository = _operation.Per36DiagnosticCaptureRepository
    original_preflight = _operation._manifest_preflight
    try:
        _operation.Per36DiagnosticCaptureRepository = Per36DiagnosticCaptureRepository
        _operation._manifest_preflight = _manifest_preflight
        return _operation._capture_per36(args, engine)
    finally:
        _operation.Per36DiagnosticCaptureRepository = original_repository
        _operation._manifest_preflight = original_preflight


def _commit_and_publish_summary(transaction, session, args, staged):
    original_publish = _operation._publish_summary
    try:
        _operation._publish_summary = _publish_summary
        return _operation._commit_and_publish_summary(
            transaction, session, args, staged,
        )
    finally:
        _operation._publish_summary = original_publish


def _compare(args, engine):
    return _operation.MatchupParityOperation(engine).compare(args)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    compare = subparsers.add_parser("compare")
    compare.add_argument("--database-url", required=True)
    compare.add_argument("--season", required=True)
    compare.add_argument("--manifest-id", required=True)
    compare.add_argument("--actor", required=True)
    compare.add_argument("--output", required=True)
    compare.add_argument("--target", choices=("isolated", "candidate"), required=True)
    compare.add_argument("--per36-capture-id", required=True)

    adjudicate = subparsers.add_parser("adjudicate")
    adjudicate.add_argument("artifact_id")
    adjudicate.add_argument("decision", choices=("approved", "rejected"))
    adjudicate.add_argument("--database-url", required=True)
    adjudicate.add_argument("--actor", required=True)
    adjudicate.add_argument("--reason", required=True)

    capture = subparsers.add_parser("capture-per36")
    capture.add_argument("--database-url", required=True)
    capture.add_argument("--season", required=True)
    capture.add_argument("--manifest-id", required=True)
    capture.add_argument("--publication-id", required=True)
    capture.add_argument("--actor", required=True)
    capture.add_argument("--input", required=True)
    capture.add_argument("--output", required=True)

    collect = subparsers.add_parser("collect-per36")
    collect.add_argument("--database-url", required=True)
    collect.add_argument("--season", required=True)
    collect.add_argument("--manifest-id", required=True)
    collect.add_argument("--output", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--database-url", required=True)
    prepare.add_argument("--season", required=True)
    prepare.add_argument("--manifest-id", required=True)
    prepare.add_argument("--output", required=True)

    args = parser.parse_args()
    engine = create_engine(args.database_url) if args.command else None
    operation = _operation.MatchupParityOperation(engine) if engine is not None else None
    if args.command == "compare":
        return _compare(args, engine)
    if args.command == "adjudicate":
        return operation.adjudicate(args)
    if args.command == "capture-per36":
        return operation.capture_per36(args)
    if args.command == "collect-per36":
        return operation.collect_per36(args)
    if args.command == "prepare":
        return operation.prepare(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
