#!/usr/bin/env python3
"""Inspect and operate provider athlete mappings without contacting providers."""

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
from app.providers.dfs import AthleteEvidence, TeamEvidence
from app.services.athlete_catalog_service import AthleteCatalogService
from app.services.athlete_mapping_repository import AthleteMappingRepository
from app.services.athlete_resolver import AthleteResolver
from app.utils.db import _normalize_database_url, is_demo_database_url


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and manage durable provider athlete mappings."
    )
    parser.add_argument("--database-url", help="writable SQLAlchemy database URL")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_identity(command: argparse.ArgumentParser, *, season: bool = False) -> None:
        command.add_argument("--provider", required=True)
        command.add_argument("--provider-athlete-id", required=True)
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

    approve = subparsers.add_parser("approve", help="approve a canonical identity")
    add_database_option(approve)
    add_identity(approve, season=True)
    approve.add_argument("--canonical-player-id", required=True, type=int)
    _add_operator_options(approve)

    override = subparsers.add_parser("override", help="override a canonical identity")
    add_database_option(override)
    add_identity(override, season=True)
    override.add_argument("--canonical-player-id", required=True, type=int)
    _add_operator_options(override)

    reject = subparsers.add_parser("reject", help="suppress a provider identity")
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
    history.add_argument("--provider-athlete-id")
    history.add_argument("--limit", type=int)
    return parser


def _add_evidence_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", required=True)
    parser.add_argument("--provider-team-id")
    parser.add_argument("--team-id", type=int, help="canonical team ID evidence")
    parser.add_argument("--team-name")
    parser.add_argument("--team-abbreviation")


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
            "the tracked nba_play_types.db is read-only and cannot store athlete mappings"
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
    # CLI commands never mutate schema implicitly.  Operators run the
    # migration command explicitly, and this seam remains read-only for list,
    # history, and dry-run.
    catalog = AthleteCatalogService(engine, settings=settings)
    repository = AthleteMappingRepository(engine)
    resolver = AthleteResolver(catalog, mapping_repository=repository)
    return engine, catalog, repository, resolver


def _evidence(args: argparse.Namespace) -> AthleteEvidence:
    team = None
    if any(
        value is not None
        for value in (
            args.provider_team_id,
            args.team_id,
            args.team_name,
            args.team_abbreviation,
        )
    ):
        team = TeamEvidence(
            provider_id=args.provider_team_id,
            canonical_id=args.team_id,
            name=args.team_name,
            abbreviation=args.team_abbreviation,
        )
    return AthleteEvidence(
        provider_id=args.provider_athlete_id,
        name=args.name,
        team=team,
    )


def _print_json(value: Any) -> None:
    value = _json_value(value)
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


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
    engine, catalog, repository, resolver = _build_services(database_url)
    try:
        if args.command == "list":
            return {
                "mappings": repository.list_mappings(
                    provider=args.provider, active_only=not args.all
                ),
                "rejections": repository.list_rejections(
                    provider=args.provider, active_only=not args.all
                ),
            }
        if args.command == "history":
            return repository.history(
                provider=args.provider,
                provider_athlete_id=args.provider_athlete_id,
                limit=args.limit,
            )
        if args.command == "dry-run":
            return resolver.resolve(
                args.provider,
                _evidence(args),
                args.season,
            )
        if args.command == "approve":
            return repository.approve(
                args.provider,
                args.provider_athlete_id,
                args.canonical_player_id,
                season=args.season,
                operator_id=args.operator_id,
                reason=args.reason,
            )
        if args.command == "override":
            return repository.override(
                args.provider,
                args.provider_athlete_id,
                args.canonical_player_id,
                season=args.season,
                operator_id=args.operator_id,
                reason=args.reason,
            )
        if args.command == "reject":
            return repository.reject(
                args.provider,
                args.provider_athlete_id,
                season=args.season,
                operator_id=args.operator_id,
                reason=args.reason,
            )
        if args.command == "clear":
            return {
                "cleared": repository.clear_rejection(
                    args.provider,
                    args.provider_athlete_id,
                    operator_id=args.operator_id,
                    reason=args.reason,
                )
            }
        raise ValueError("unsupported athlete mapping command")
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
            f"Athlete mapping command failed for {_redacted_database_url(database_url)}: "
            "the operation did not complete.",
            file=sys.stderr,
        )
        return 1
    _print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
