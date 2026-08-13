"""Command line entry point for the one-shot Residential Collector."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .client import RailwayClient, RequestsTransport
from .cache import InstructionCache
from .config import CollectorConfig, CollectorConfigurationError, load_collector_config
from .diagnostics import build_safe_logger
from .outbox import OutboxRepository
from .provider import NBAStatsProviderAdapter
from .release import release_metadata
from .rehearsal import NBA_TEAM_IDS, ResidentialCompatibilityProbes, SanitizedFixtureProvider
from .runner import EnvironmentCredentialProvider, ResidentialCollector, WindowsCredentialProvider


def _release_root() -> Path:
    configured = str(os.environ.get("COLLECTOR_RELEASE_ROOT", "")).strip()
    if configured:
        return Path(configured).resolve()
    repository_root = Path(__file__).resolve().parents[2]
    return repository_root if (repository_root / "pyproject.toml").exists() else Path(__file__).resolve().parent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="statsplus-residential-collector",
        description="Run one bounded, pull-only StatsPlus residential collection invocation.",
    )
    parser.add_argument("command", nargs="?", choices=("run", "status", "release", "validate-config", "rehearsal", "credential-check"), default="run")
    parser.add_argument("--credential-env", help=argparse.SUPPRESS)
    parser.add_argument("--season", help="NBA season for the compatibility rehearsal")
    parser.add_argument("--cutoff", help="ISO cutoff governing rehearsal date_to")
    parser.add_argument("--live", action="store_true", help="use live NBA endpoints instead of sanitized fixtures")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    return parser


def run_once(
    config: CollectorConfig,
    *,
    provider: Any | None = None,
    transport: Any | None = None,
    secret: str | None = None,
    credential_provider: Any | None = None,
) -> int:
    outbox = OutboxRepository(
        config.outbox_path,
        max_bytes=config.outbox_max_bytes,
        max_item_bytes=config.outbox_max_item_bytes,
    )
    try:
        client = RailwayClient(
            config.railway_url,
            identity_id=config.identity_id,
            environment=config.environment,
            transport=transport or RequestsTransport(),
            timeout=config.http_timeout_seconds,
            allow_insecure_localhost=config.allow_insecure_localhost,
            release_version=config.release_version,
        )
        if credential_provider is None:
            if secret:
                credential_provider = None
            elif config.environment in {"testing", "test", "development", "historical_rehearsal"}:
                credential_provider = EnvironmentCredentialProvider()
            else:
                credential_provider = WindowsCredentialProvider()
        collector = ResidentialCollector(
            client=client, outbox=outbox,
            provider=provider if provider is not None else NBAStatsProviderAdapter(),
            identity_id=config.identity_id, environment=config.environment,
            credential_provider=credential_provider, secret=secret,
            release_version=config.release_version,
            token_ttl_seconds=config.token_ttl_seconds,
            poll_limit=config.poll_limit,
            logger=build_safe_logger(config.log_path),
            instruction_cache=InstructionCache(config.outbox_path.with_suffix(".instructions.json")),
            release_checksum=release_metadata(_release_root(), version=config.release_version).checksum,
        )
        result = collector.run()
        print(json.dumps({
            "status": result.disposition.value,
            "exit_code": result.exit_code,
            "uploaded": result.uploaded,
            "spooled": result.spooled,
            "pending": outbox.count(),
            "failures": list(result.failures),
        }, sort_keys=True))
        return result.exit_code
    finally:
        outbox.close()


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config = load_collector_config()
    except (CollectorConfigurationError, ValueError) as error:
        parser.error(str(error))
    if args.command == "validate-config":
        print(json.dumps({"status": "valid", "environment": config.environment, "identity_id": config.identity_id}, sort_keys=True))
        return 0
    if args.command == "credential-check":
        WindowsCredentialProvider().get_secret(config.identity_id)
        print(json.dumps({"status": "available", "identity_id": config.identity_id}, sort_keys=True))
        return 0
    if args.command == "status":
        outbox = OutboxRepository(config.outbox_path, max_bytes=config.outbox_max_bytes, max_item_bytes=config.outbox_max_item_bytes)
        try:
            metadata = release_metadata(_release_root(), version=config.release_version)
            print(json.dumps({"status": "ok", "environment": config.environment, "identity_id": config.identity_id,
                              "release_version": config.release_version, "release_checksum": metadata.checksum,
                              "outbox_count": outbox.count(),
                              "outbox_bytes": outbox.bytes_pending()}, sort_keys=True))
        finally:
            outbox.close()
        return 0
    if args.command == "release":
        metadata = release_metadata(_release_root(), version=config.release_version)
        print(metadata.to_json())
        return 0
    if args.command == "rehearsal":
        if not args.season or not args.cutoff:
            parser.error("rehearsal requires --season and --cutoff")
        provider = NBAStatsProviderAdapter() if args.live else SanitizedFixtureProvider()
        probes = ResidentialCompatibilityProbes(provider)
        results = tuple(
            result
            for team_id in NBA_TEAM_IDS
            for result in probes.run(season=args.season, cutoff=args.cutoff, opponent_team_id=team_id)
        )
        failures = sorted({result.scope for result in results if not result.passed})
        evidence = {"status": "passed" if not failures else "failed", "mode": "live" if args.live else "offline",
                    "teams": len(NBA_TEAM_IDS), "probes": len(results), "failed_scopes": failures}
        print(json.dumps(evidence, sort_keys=True))
        return 0 if not failures else 20
    credential_provider = EnvironmentCredentialProvider(variable=args.credential_env) if args.credential_env else None
    if credential_provider is not None and config.environment not in {"testing", "test", "development", "historical_rehearsal"}:
        parser.error("--credential-env is limited to non-production rehearsal environments")
    return run_once(config, credential_provider=credential_provider)


__all__ = ["main", "run_once"]
