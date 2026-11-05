"""Recollect 2025-26 Synergy play types with their turnovers and free-throw trips.

One-off operator command for crf04/statsplus#416.  The 2025-26 Synergy
observations were collected before the collector kept ``TOV_POSS_PCT`` and
``FT_POSS_PCT``, so that season's TOV, FTA and FTM Matchup Scores have no Play
types component.  This command fetches the 22 Synergy responses (11 player, 11
team defense) through the residential collector's own executor and
normalizers, accepts them under the manifest that authorized each stream's
active 2025-26 Publication, and drains the composition jobs that acceptance
queues, exactly as ``scripts/ledger_refresh.py 2025-26 --compose-only`` does.

It never activates a season, issues or supersedes a manifest, or touches
another season's rows, so the daily jobs keep following 2026-27.  It refuses
before any provider call unless 2025-26 is the Published Season, both Synergy
streams serve a 2025-26 Publication, and that Publication's Event Catalog shows
the Regular Season finished (Synergy's season totals can then no longer
change), and unless no other 2025-26 composition job is waiting.

Idempotent.  Observation ids are derived from the payload, so a rerun with the
same provider answers accepts nothing new and recomposes nothing; a rerun after
a crash drains whatever the first run queued.  ``--dry-run`` fetches,
validates and reports without writing.  Prints one JSON summary line; exits 0
on success, 2 when refused or when a composition did not publish.

NBA requests go through ``COLLECTOR_NBA_PROXY_URL`` when it is set.

    python scripts/synergy_play_types_backfill.py --database-url "$DATABASE_URL" --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collector.contracts import ObservationEnvelope, ProviderContractError, canonical_json
from app.collector.provider import ProviderTransientError, ResidentialScopeExecutor, ScopeWork
from app.domain.team_matchup_taxonomy import PLAY_TYPES
from app.models.collection_control import (
    CollectionObservation,
    CompositionJob,
    PublicationPointer,
    PublicationStream,
    PublicationVersion,
)
from app.services.collection_control import (
    PublicationService,
    _collector_scope_descriptors,
    _published_season,
    _validate_observation_payload,
)
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.team_matchup_publications import PublicationGovernanceUnavailable

SEASON = "2025-26"
COLLECTOR_ID = "backfill-statsplus-416"
INSTRUCTION_ID = "statsplus#416:2025-26-synergy"
#: Each Synergy stream and the observation type it composes from.
STREAMS = {
    "synergy_play_types": "synergy_play_types",
    "synergy_play_types_opponent_season": "synergy_opponent",
}
EXIT_OK = 0
EXIT_REFUSED = 2


class Refused(Exception):
    """A precondition failed; nothing was written."""


@dataclass(frozen=True, slots=True)
class Target:
    stream_key: str
    observation_type: str
    manifest_id: str
    cutoff: datetime
    environment: str
    games_by_team: dict[int, int]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _targets(engine) -> list[Target]:
    governance = ActiveManifestLedgerGovernanceReader(engine)
    targets = []
    with Session(engine) as session:
        if _published_season(session) != SEASON:
            raise Refused("published_season_not_2025_26")
        queued = sorted(
            session.scalars(
                select(CompositionJob.stream_key).where(
                    CompositionJob.season == SEASON,
                    CompositionJob.status == "queued",
                    CompositionJob.stream_key.not_in(STREAMS),
                )
            )
        )
        if queued:
            raise Refused(f"other_2025_26_jobs_queued:{','.join(queued)}")
        for stream_key, observation_type in STREAMS.items():
            stream = session.get(PublicationStream, stream_key)
            active = session.scalar(
                select(PublicationVersion)
                .join(
                    PublicationPointer,
                    PublicationPointer.active_publication_id == PublicationVersion.publication_id,
                )
                .where(PublicationPointer.stream_key == stream_key)
            )
            if stream is None or not stream.enabled:
                raise Refused(f"stream_disabled:{stream_key}")
            if active is None or active.season != SEASON or not active.manifest_id:
                raise Refused(f"stream_not_serving_2025_26:{stream_key}")
            cutoff = _aware(active.cutoff)
            try:
                governed = governance.read_for_composition(SEASON, cutoff, active.manifest_id)
            except PublicationGovernanceUnavailable as error:
                raise Refused(f"governance_unavailable:{stream_key}") from error
            if not governed.season_complete:
                raise Refused("season_not_complete")
            environment = session.scalar(
                select(CollectionObservation.environment)
                .where(
                    CollectionObservation.manifest_id == active.manifest_id,
                    CollectionObservation.observation_type == observation_type,
                )
                .limit(1)
            )
            if environment is None:
                raise Refused(f"no_prior_observation:{stream_key}")
            targets.append(
                Target(
                    stream_key=stream_key,
                    observation_type=observation_type,
                    manifest_id=active.manifest_id,
                    cutoff=cutoff,
                    environment=environment,
                    games_by_team={
                        team_id: len(game_ids)
                        for team_id, game_ids in governed.expected_season_game_ids.items()
                    },
                )
            )
    return targets


def _fetch(target: Target, provider: Any, now: datetime) -> list[ObservationEnvelope]:
    """The collector's own requests, executor and normalizers for one stream."""

    executor = ResidentialScopeExecutor(provider, clock=lambda: now)
    envelopes = []
    for descriptor in _collector_scope_descriptors({target.observation_type}, target.cutoff):
        if descriptor["scope"] != target.observation_type:
            continue
        work = ScopeWork(
            scope=descriptor["scope"],
            observation_type=target.observation_type,
            season=SEASON,
            cutoff=target.cutoff.isoformat(),
            instruction_id=INSTRUCTION_ID,
            manifest_id=target.manifest_id,
            parameters=descriptor["parameters"],
        )
        for observation in executor.execute_scope(
            work, collector_id=COLLECTOR_ID, environment=target.environment, retrieved_at=now
        ):
            if not observation.complete:
                raise Refused(f"incomplete_observation:{target.stream_key}")
            envelope = ObservationEnvelope.from_observation(
                observation,
                manifest_id=target.manifest_id,
                environment=target.environment,
                collector_id=COLLECTOR_ID,
                instruction_id=INSTRUCTION_ID,
                retrieved_at=now,
            )
            try:
                _validate_observation_payload(
                    dict(envelope.payload), observation_type=target.observation_type
                )
            except ValueError as error:
                raise Refused(f"observation_invalid:{target.stream_key}") from error
            envelopes.append(envelope)
    return envelopes


def _report(target: Target, envelopes: Sequence[ObservationEnvelope]) -> dict[str, Any]:
    """Rows per play type and who they cover; refuses evidence that cannot compose."""

    records = [
        (envelope.scope["play_type"], record)
        for envelope in envelopes
        for record in envelope.payload["records"]
    ]
    if sorted(envelope.scope["play_type"] for envelope in envelopes) != sorted(PLAY_TYPES):
        raise Refused(f"play_types_incomplete:{target.stream_key}")
    report: dict[str, Any] = {
        "manifest_id": target.manifest_id,
        "cutoff": target.cutoff.isoformat(),
        "observations": len(envelopes),
        "rows_by_play_type": dict(sorted(Counter(play_type for play_type, _record in records).items())),
    }
    if target.observation_type == "synergy_opponent":
        if any("TOV" not in record or "FTT" not in record for _play_type, record in records):
            raise Refused("team_rates_missing")
        if any(
            int(record["games_played"]) != target.games_by_team.get(int(record["team_id"]))
            for _play_type, record in records
        ):
            raise Refused("games_played_mismatch")
        report["teams"] = len({int(record["team_id"]) for _play_type, record in records})
    else:
        with_counts = [record for _play_type, record in records if "turnovers" in record]
        if not with_counts:
            raise Refused("player_rates_missing")
        report["players"] = len({int(record["player_id"]) for _play_type, record in records})
        report["rows_with_counts"] = len(with_counts)
    return report


def _new(session: Session, envelopes: Sequence[ObservationEnvelope]) -> list[ObservationEnvelope]:
    accepted = set(
        session.scalars(
            select(CollectionObservation.client_observation_id).where(
                CollectionObservation.collector_id == COLLECTOR_ID,
                CollectionObservation.client_observation_id.in_(
                    [envelope.client_observation_id for envelope in envelopes]
                ),
            )
        )
    )
    return [envelope for envelope in envelopes if envelope.client_observation_id not in accepted]


def _accept(
    session: Session, publications: PublicationService, target: Target, envelopes, now: datetime
) -> None:
    """Store the observations and queue their stream, as ingestion would."""

    for envelope in envelopes:
        payload = envelope.payload_bytes().decode()
        session.add(
            CollectionObservation(
                observation_id=str(uuid.uuid4()),
                client_observation_id=envelope.client_observation_id,
                collector_id=COLLECTOR_ID,
                environment=target.environment,
                provider=envelope.provider,
                observation_type=envelope.observation_type,
                manifest_id=target.manifest_id,
                scope=canonical_json(envelope.scope).decode(),
                season=SEASON,
                cutoff=target.cutoff,
                schema_version=envelope.schema_version,
                checksum=envelope.checksum,
                payload=payload,
                payload_bytes=len(payload.encode()),
                retrieved_at=now,
                accepted_at=now,
            )
        )
    session.flush()
    publications.enqueue_for_observation(
        target.observation_type,
        season=SEASON,
        cutoff=target.cutoff,
        manifest_id=target.manifest_id,
        window="season",
        session=session,
    )


def _published(engine, target: Target) -> dict[str, Any]:
    with Session(engine) as session:
        job = session.scalar(
            select(CompositionJob).where(
                CompositionJob.stream_key == target.stream_key,
                CompositionJob.season == SEASON,
                CompositionJob.cutoff == target.cutoff,
            )
        )
        active = session.scalar(
            select(PublicationVersion)
            .join(
                PublicationPointer,
                PublicationPointer.active_publication_id == PublicationVersion.publication_id,
            )
            .where(PublicationPointer.stream_key == target.stream_key)
        )
        rows = json.loads(active.payload).get("rows", ()) if active is not None else ()
        if target.observation_type == "synergy_opponent":
            carries_counts = bool(rows) and all(
                f"{play_type}_{stat}" in row["per48"]
                for row in rows
                for play_type in PLAY_TYPES
                for stat in ("TOV", "FTT")
            )
        else:
            carries_counts = any("turnovers" in row for row in rows)
        return {
            "job_status": job.status if job is not None else None,
            "last_error": job.last_error if job is not None else None,
            "publication_id": active.publication_id if active is not None else None,
            "version": active.version if active is not None else None,
            "carries_counts": carries_counts
            and active.season == SEASON
            and active.manifest_id == target.manifest_id
            and _aware(active.cutoff) == target.cutoff,
        }


def run_backfill(
    engine,
    *,
    provider: Any,
    compose_queued: Callable[[str], int],
    now: datetime,
    dry_run: bool,
) -> tuple[int, dict[str, Any]]:
    summary: dict[str, Any] = {"season": SEASON, "dry_run": dry_run}
    try:
        targets = _targets(engine)
        fetched = {target.stream_key: _fetch(target, provider, now) for target in targets}
        summary["streams"] = {
            target.stream_key: _report(target, fetched[target.stream_key]) for target in targets
        }
    except Refused as refusal:
        return EXIT_REFUSED, {**summary, "refused": str(refusal)}
    except (ProviderContractError, ProviderTransientError) as error:
        return EXIT_REFUSED, {
            **summary,
            "refused": f"provider:{getattr(error, 'reason', type(error).__name__)}",
        }
    publications = PublicationService(
        engine, l15_expectation_resolver=ActiveManifestLedgerGovernanceReader(engine)
    )
    with Session(engine) as session, session.begin():
        for target in targets:
            new = _new(session, fetched[target.stream_key])
            summary["streams"][target.stream_key]["new_observations"] = len(new)
            if new and not dry_run:
                _accept(session, publications, target, new, now)
    if dry_run:
        return EXIT_OK, summary
    summary["composed_jobs"] = compose_queued(SEASON)
    for target in targets:
        summary["streams"][target.stream_key]["published"] = _published(engine, target)
    failed = any(not stream["published"]["carries_counts"] for stream in summary["streams"].values())
    return (EXIT_REFUSED if failed else EXIT_OK), summary


def main(argv: Sequence[str] | None = None, *, provider: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--dry-run", action="store_true", help="fetch, validate and report without writing")
    args = parser.parse_args(argv)
    if not args.database_url.strip():
        parser.error("--database-url or DATABASE_URL is required")

    from app.collector.provider import NBAStatsProviderAdapter
    from app.config.settings import RuntimeSettings
    from app.dependencies import build_dependencies
    from app.services.ledger_runtime import LedgerRuntime

    settings = RuntimeSettings(
        environment="development",
        database={"url": args.database_url},
        auth={"firebase_admin_disabled": True},
    )
    dependencies = build_dependencies(settings)
    runtime = LedgerRuntime(
        backfill=dependencies.ledger_backfill_service,
        repository=dependencies.canonical_game_ledger_repository,
        materialization=dependencies.ledger_materialization_service,
        governance=ActiveManifestLedgerGovernanceReader(dependencies.engine),
        matchup_materialization=dependencies.ledger_matchup_materialization_service,
    )
    try:
        status, summary = run_backfill(
            dependencies.engine,
            provider=provider
            or NBAStatsProviderAdapter(proxy=os.environ.get("COLLECTOR_NBA_PROXY_URL") or None),
            compose_queued=runtime.compose_queued,
            now=datetime.now(UTC),
            dry_run=args.dry_run,
        )
    finally:
        dependencies.engine.dispose()
    print(json.dumps(summary, sort_keys=True, default=str), flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
