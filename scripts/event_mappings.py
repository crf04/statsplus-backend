#!/usr/bin/env python3
"""Inspect and operate provider event mappings without contacting providers."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
import json
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.config.settings import load_settings
from app.providers.dfs import EventEvidence, TeamEvidence
from app.services.event_catalog_service import EventCatalogService
from app.services.event_mapping_repository import EventMappingRepository
from app.services.event_resolver import EventResolver
from app.utils.db import _normalize_database_url, is_demo_database_url


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and manage durable provider event mappings."
    )
    parser.add_argument("--database-url", help="writable SQLAlchemy database URL")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_identity(command: argparse.ArgumentParser, *, season: bool = False) -> None:
        command.add_argument("--provider", required=True)
        command.add_argument("--provider-event-id", required=True)
        if season:
            command.add_argument("--season", required=True)

    def add_database_option(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--database-url",
            dest="database_url",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )

    list_parser = subparsers.add_parser("list", help="list current mapping state")
    add_database_option(list_parser)
    list_parser.add_argument("--provider")
    list_parser.add_argument("--all", action="store_true", help="include inactive rows")

    dry_run = subparsers.add_parser("dry-run", help="resolve evidence without writing")
    add_database_option(dry_run)
    add_identity(dry_run, season=True)
    _add_evidence_options(dry_run)

    approve = subparsers.add_parser("approve", help="approve a canonical NBA game")
    add_database_option(approve)
    add_identity(approve, season=True)
    approve.add_argument("--canonical-event-id", required=True, help="NBA game ID")
    _add_operator_options(approve)
    _add_evidence_options(approve)

    override = subparsers.add_parser("override", help="override a canonical NBA game")
    add_database_option(override)
    add_identity(override, season=True)
    override.add_argument("--canonical-event-id", required=True, help="NBA game ID")
    _add_operator_options(override)
    _add_evidence_options(override)

    reject = subparsers.add_parser("reject", help="suppress a provider event identity")
    add_database_option(reject)
    add_identity(reject)
    reject.add_argument("--season")
    _add_operator_options(reject)

    clear = subparsers.add_parser("clear", help="clear an active rejection")
    add_database_option(clear)
    add_identity(clear)
    _add_operator_options(clear)

    history = subparsers.add_parser("history", help="show append-only decisions")
    add_database_option(history)
    history.add_argument("--provider")
    history.add_argument("--provider-event-id")
    history.add_argument("--limit", type=int)
    return parser


def _add_evidence_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--label", help="provider matchup label evidence")
    parser.add_argument("--starts-at", help="provider start time (ISO 8601)")
    parser.add_argument("--status-label", help="provider event status evidence")
    for side in ("home", "away"):
        parser.add_argument(f"--{side}-provider-team-id")
        parser.add_argument(
            f"--{side}-team-id", type=int, help=f"canonical {side} team ID evidence"
        )
        parser.add_argument(f"--{side}-team-name")
        parser.add_argument(f"--{side}-team-abbreviation")


def _add_operator_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--operator", required=True, dest="operator_id")
    parser.add_argument("--reason", required=True)


def _database_url_for_command(
    parser: argparse.ArgumentParser, requested_url: str | None
) -> str:
    database_url = requested_url if requested_url is not None else os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("a writable target is required: pass --database-url or set DATABASE_URL")
    if is_demo_database_url(database_url):
        parser.error(
            "the tracked nba_play_types.db is read-only and cannot store event mappings"
        )
    return database_url


def _redacted_database_url(database_url: str) -> str:
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except ArgumentError:
        return "<invalid database URL>"


def _build_services(database_url: str):
    settings = load_settings(overrides={"DATABASE_URL": database_url})
    engine = create_engine(_normalize_database_url(database_url))
    # CLI commands never mutate schema implicitly.  Operators run the migration
    # command explicitly, and this seam remains read-only for list, history, and
    # dry-run.
    catalog = EventCatalogService(
        engine, nba_stats_provider=_OfflineScheduleProvider(), settings=settings
    )
    repository = EventMappingRepository(engine)
    resolver = EventResolver(
        catalog, mapping_repository=repository, settings=settings
    )
    return engine, catalog, repository, resolver


class _OfflineScheduleProvider:
    """Refuse the provider seam the catalog service requires but never uses.

    ``EventCatalogService`` owns both refreshing and reading the catalog, and
    this CLI only reads.  Supplying an explicitly refusing provider keeps the
    command offline by construction rather than by convention.
    """

    def fetch_whole_season_schedule(self, *, season: str):
        raise RuntimeError("the event mapping CLI never contacts a provider")


def _team_evidence(args: argparse.Namespace, side: str) -> TeamEvidence | None:
    values = {
        "provider_id": getattr(args, f"{side}_provider_team_id"),
        "canonical_id": getattr(args, f"{side}_team_id"),
        "name": getattr(args, f"{side}_team_name"),
        "abbreviation": getattr(args, f"{side}_team_abbreviation"),
    }
    if all(value is None for value in values.values()):
        return None
    return TeamEvidence(**values)


def _evidence(args: argparse.Namespace) -> EventEvidence:
    return EventEvidence(
        provider_id=args.provider_event_id,
        label=args.label,
        starts_at=args.starts_at,
        status_label=args.status_label,
        home_team=_team_evidence(args, "home"),
        away_team=_team_evidence(args, "away"),
    )


def _optional_evidence(args: argparse.Namespace) -> EventEvidence | None:
    """Build provider evidence only when an operator supplied any.

    Manual decisions retain the previously observed evidence when the operator
    has nothing new to record.
    """

    supplied = (
        args.label,
        args.starts_at,
        args.status_label,
        _team_evidence(args, "home"),
        _team_evidence(args, "away"),
    )
    if all(value is None for value in supplied):
        return None
    return _evidence(args)


def _print_json(value: Any) -> None:
    print(json.dumps(_json_value(value), indent=2, sort_keys=True, default=str))


def _json_value(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return _json_value(value.to_dict())
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _run(database_url: str, args: argparse.Namespace) -> Any:
    engine, _catalog, repository, resolver = _build_services(database_url)
    try:
        if args.command == "list":
            return {
                "mappings": repository.list_mappings(
                    provider=args.provider, active_only=not args.all
                ),
                "rejections": repository.list_rejections(
                    provider=args.provider, active_only=not args.all
                ),
                "unresolved": repository.list_unresolved(provider=args.provider),
                # Conflicts are inactive and are not unresolved observations, so
                # the actionable review queue is reported separately rather than
                # dropped from the default listing.
                "conflicts": repository.list_conflicts(provider=args.provider),
            }
        if args.command == "history":
            return repository.history(
                provider=args.provider,
                provider_event_id=args.provider_event_id,
                limit=args.limit,
            )
        if args.command == "dry-run":
            return resolver.resolve(args.provider, _evidence(args), args.season)
        if args.command == "approve":
            return repository.approve(
                args.provider,
                args.provider_event_id,
                args.canonical_event_id,
                season=args.season,
                operator_id=args.operator_id,
                reason=args.reason,
                provider_evidence=_optional_evidence(args),
            )
        if args.command == "override":
            return repository.override(
                args.provider,
                args.provider_event_id,
                args.canonical_event_id,
                season=args.season,
                operator_id=args.operator_id,
                reason=args.reason,
                provider_evidence=_optional_evidence(args),
            )
        if args.command == "reject":
            return repository.reject(
                args.provider,
                args.provider_event_id,
                season=args.season,
                operator_id=args.operator_id,
                reason=args.reason,
            )
        if args.command == "clear":
            return {
                "cleared": repository.clear_rejection(
                    args.provider,
                    args.provider_event_id,
                    operator_id=args.operator_id,
                    reason=args.reason,
                )
            }
        raise ValueError("unsupported event mapping command")
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    database_url = _database_url_for_command(parser, args.database_url)
    try:
        result = _run(database_url, args)
    except Exception:
        print(
            f"Event mapping command failed for {_redacted_database_url(database_url)}: "
            "the operation did not complete.",
            file=sys.stderr,
        )
        return 1
    _print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
