"""Collect 2025-26 NBA.com Defensive Rating for the Season and Last 15 streams.

One-off operator command for crf04/statsplus#447.  DRTG is NBA.com's own
``DEF_RATING`` (``LeagueDashTeamStats``, ``MeasureType=Advanced``).  The daily
manifests follow 2026-27, so no manifest will ever ask the collector for the
finished 2025-26 season.  This command makes the two league-wide reads the
residential collector makes (``LastNGames`` 0 and 15) through its own executor
and normalizer, accepts them under the manifest that authorized the active
2025-26 ``exact_shot_zones_opponent_season`` Publication, and drains the
composition jobs that acceptance queues, exactly as
``scripts/synergy_play_types_backfill.py`` does for Synergy.

It never activates a season, issues or supersedes a manifest, or touches
another season's rows.  It refuses before any provider call unless 2025-26 is
the Published Season, both Defensive Rating streams are enabled, the anchor
Publication's Event Catalog shows the Regular Season finished (so neither
window can still move), and no other 2025-26 composition job is waiting.  A
response NBA refuses, or one that does not rate all thirty teams, writes
nothing.

Idempotent.  Observation ids are derived from the payload, so a rerun with the
same provider answers accepts nothing new and recomposes nothing; a rerun after
a crash drains whatever the first run queued.  ``--dry-run`` fetches,
validates and reports without writing.  Prints one JSON summary line; exits 0
on success, 2 when refused or when a composition did not publish.

NBA requests go through the residential proxy in ``COLLECTOR_NBA_PROXY_URL``;
without it the command refuses unless ``--no-proxy`` is passed.

    python scripts/defensive_rating_backfill.py --database-url "$DATABASE_URL" --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
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
from app.models.collection_control import (
    CollectionObservation,
    CompositionJob,
    PublicationPointer,
    PublicationStream,
    PublicationVersion,
)
from app.services.collection_control import (
    DEFENSIVE_RATING_OBSERVATION,
    PublicationService,
    _collector_scope_descriptors,
    _published_season,
    _validate_observation_payload,
)
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.team_matchup_publications import PublicationGovernanceUnavailable

SEASON = "2025-26"
COLLECTOR_ID = "backfill-statsplus-447"
INSTRUCTION_ID = "statsplus#447:2025-26-defensive-rating"
#: The window each Defensive Rating stream composes.
STREAMS = {"defensive_rating_season": "season", "defensive_rating_l15": "l15"}
#: The NBA-owned team-defense stream whose active 2025-26 Publication names
#: the manifest (and so the cutoff and Event Catalog) this backfill rides on.
ANCHOR_STREAM = "exact_shot_zones_opponent_season"
EXIT_OK = 0
EXIT_REFUSED = 2


class Refused(Exception):
    """A precondition failed; nothing was written."""


@dataclass(frozen=True, slots=True)
class Authority:
    manifest_id: str
    cutoff: datetime
    environment: str
    l15_date_from_by_team: dict[int, str]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _active(session: Session, stream_key: str) -> PublicationVersion | None:
    return session.scalar(
        select(PublicationVersion)
        .join(
            PublicationPointer,
            PublicationPointer.active_publication_id == PublicationVersion.publication_id,
        )
        .where(PublicationPointer.stream_key == stream_key)
    )


def _authority(engine) -> Authority:
    governance = ActiveManifestLedgerGovernanceReader(engine)
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
        for stream_key in STREAMS:
            stream = session.get(PublicationStream, stream_key)
            if stream is None or not stream.enabled:
                raise Refused(f"stream_disabled:{stream_key}")
        anchor = _active(session, ANCHOR_STREAM)
        if anchor is None or anchor.season != SEASON or not anchor.manifest_id:
            raise Refused(f"anchor_not_serving_2025_26:{ANCHOR_STREAM}")
        cutoff = _aware(anchor.cutoff)
        try:
            governed = governance.read_for_composition(SEASON, cutoff, anchor.manifest_id)
        except PublicationGovernanceUnavailable as error:
            raise Refused("governance_unavailable") from error
        if not governed.season_complete:
            raise Refused("season_not_complete")
        if not governed.l15_ready:
            raise Refused("l15_not_ready")
        environment = session.scalar(
            select(CollectionObservation.environment)
            .where(CollectionObservation.manifest_id == anchor.manifest_id)
            .limit(1)
        )
        if environment is None:
            raise Refused("no_prior_observation")
        return Authority(
            manifest_id=anchor.manifest_id,
            cutoff=cutoff,
            environment=environment,
            l15_date_from_by_team=dict(governed.expected_l15_date_from_by_team),
        )


def _fetch(authority: Authority, provider: Any, now: datetime) -> dict[str, ObservationEnvelope]:
    """The collector's own descriptors, executor and normalizer, one per window."""

    executor = ResidentialScopeExecutor(provider, clock=lambda: now)
    envelopes: dict[str, ObservationEnvelope] = {}
    for descriptor in _collector_scope_descriptors(
        {DEFENSIVE_RATING_OBSERVATION},
        authority.cutoff,
        l15_date_from_by_team=authority.l15_date_from_by_team,
    ):
        work = ScopeWork(
            scope=descriptor["scope"],
            observation_type=DEFENSIVE_RATING_OBSERVATION,
            season=SEASON,
            cutoff=authority.cutoff.isoformat(),
            instruction_id=INSTRUCTION_ID,
            manifest_id=authority.manifest_id,
            parameters=descriptor["parameters"],
        )
        window = descriptor["parameters"]["window"]
        for observation in executor.execute_scope(
            work, collector_id=COLLECTOR_ID, environment=authority.environment, retrieved_at=now
        ):
            if not observation.complete:
                raise Refused(f"incomplete_observation:{window}")
            envelope = ObservationEnvelope.from_observation(
                observation,
                manifest_id=authority.manifest_id,
                environment=authority.environment,
                collector_id=COLLECTOR_ID,
                instruction_id=INSTRUCTION_ID,
                retrieved_at=now,
            )
            try:
                _validate_observation_payload(
                    dict(envelope.payload), observation_type=DEFENSIVE_RATING_OBSERVATION
                )
            except ValueError as error:
                raise Refused(f"observation_invalid:{window}") from error
            envelopes[window] = envelope
    if set(envelopes) != set(STREAMS.values()):
        raise Refused("windows_incomplete")
    return envelopes


def _report(envelope: ObservationEnvelope) -> dict[str, Any]:
    records = envelope.payload["records"]
    return {"teams": len(records), "games_played": sorted({int(r["games_played"]) for r in records})}


def _is_new(session: Session, authority: Authority, window: str, envelope: ObservationEnvelope) -> bool:
    """Whether NBA's answer differs from the window's latest accepted one.

    Composition reads the latest accepted observation, so an answer that
    returns to an earlier value (A, then B, then A) is new evidence again,
    not a duplicate of the first.
    """

    latest = session.scalars(
        select(CollectionObservation)
        .where(
            CollectionObservation.manifest_id == authority.manifest_id,
            CollectionObservation.observation_type == DEFENSIVE_RATING_OBSERVATION,
            CollectionObservation.season == SEASON,
        )
        .order_by(CollectionObservation.accepted_at.desc(), CollectionObservation.observation_id.desc())
    ).all()
    for observation in latest:
        if json.loads(observation.scope).get("window") == window:
            return observation.checksum != envelope.checksum
    return True


def _accept(
    session: Session,
    publications: PublicationService,
    authority: Authority,
    window: str,
    envelope: ObservationEnvelope,
    now: datetime,
) -> None:
    """Store the observation and queue its stream, as ingestion would."""

    payload = envelope.payload_bytes().decode()
    session.add(
        CollectionObservation(
            observation_id=str(uuid.uuid4()),
            # The payload-derived id plus the run, so an answer seen before can
            # be accepted again when NBA returns to it.
            client_observation_id=f"{envelope.client_observation_id[:96]}:{now.strftime('%Y%m%dT%H%M%S%f')}",
            collector_id=COLLECTOR_ID,
            environment=authority.environment,
            provider=envelope.provider,
            observation_type=envelope.observation_type,
            manifest_id=authority.manifest_id,
            scope=canonical_json(envelope.scope).decode(),
            season=SEASON,
            cutoff=authority.cutoff,
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
        DEFENSIVE_RATING_OBSERVATION,
        season=SEASON,
        cutoff=authority.cutoff,
        manifest_id=authority.manifest_id,
        window=window,
        session=session,
    )


def _retry_failed(session: Session, publications: PublicationService, stream_key: str, cutoff) -> int:
    """Requeue the stream's failed job, so a rerun recovers a failed composition."""

    job = session.scalar(
        select(CompositionJob).where(
            CompositionJob.stream_key == stream_key,
            CompositionJob.season == SEASON,
            CompositionJob.cutoff == cutoff,
            CompositionJob.status == "failed",
        )
    )
    if job is None:
        return 0
    publications.retry(job.job_id, reason="statsplus#447 2025-26 backfill", session=session)
    return 1


def _published(
    engine, stream_key: str, authority: Authority, envelope: ObservationEnvelope
) -> dict[str, Any]:
    with Session(engine) as session:
        job = session.scalar(
            select(CompositionJob).where(
                CompositionJob.stream_key == stream_key,
                CompositionJob.season == SEASON,
                CompositionJob.cutoff == authority.cutoff,
            )
        )
        active = _active(session, stream_key)
        rows = json.loads(active.payload).get("rows", ()) if active is not None else ()
        fetched = {int(r["team_id"]): float(r["DEF_RATING"]) for r in envelope.payload["records"]}
        served = {int(row["team_id"]): float(row["per48"]["DEF_RATING"]) for row in rows}
        return {
            "job_status": job.status if job is not None else None,
            "last_error": job.last_error if job is not None else None,
            "publication_id": active.publication_id if active is not None else None,
            "teams": len(rows),
            "serving": active is not None
            and len(rows) == 30
            and served == fetched
            and active.season == SEASON
            and active.manifest_id == authority.manifest_id
            and _aware(active.cutoff) == authority.cutoff,
        }


def run_backfill(
    engine,
    *,
    provider: Any,
    compose_queued: Callable[[], Callable[[str], int]],
    now: datetime,
    dry_run: bool,
) -> tuple[int, dict[str, Any]]:
    summary: dict[str, Any] = {"season": SEASON, "dry_run": dry_run}
    try:
        authority = _authority(engine)
        fetched = _fetch(authority, provider, now)
    except Refused as refusal:
        return EXIT_REFUSED, {**summary, "refused": str(refusal)}
    except (ProviderContractError, ProviderTransientError) as error:
        return EXIT_REFUSED, {
            **summary,
            "refused": f"provider:{getattr(error, 'reason', type(error).__name__)}",
        }
    summary.update(manifest_id=authority.manifest_id, cutoff=authority.cutoff.isoformat())
    summary["streams"] = {stream_key: _report(fetched[window]) for stream_key, window in STREAMS.items()}
    publications = PublicationService(
        engine, l15_expectation_resolver=ActiveManifestLedgerGovernanceReader(engine)
    )
    with Session(engine) as session, session.begin():
        for stream_key, window in STREAMS.items():
            new = _is_new(session, authority, window, fetched[window])
            summary["streams"][stream_key]["new_observations"] = int(new)
            if dry_run:
                continue
            if new:
                _accept(session, publications, authority, window, fetched[window], now)
            summary["streams"][stream_key]["retried_jobs"] = _retry_failed(
                session, publications, stream_key, authority.cutoff
            )
    if dry_run:
        return EXIT_OK, summary
    summary["composed_jobs"] = compose_queued()(SEASON)
    for stream_key in STREAMS:
        summary["streams"][stream_key]["published"] = _published(
            engine, stream_key, authority, fetched[STREAMS[stream_key]]
        )
    failed = any(not stream["published"]["serving"] for stream in summary["streams"].values())
    return (EXIT_REFUSED if failed else EXIT_OK), summary


def _composer(engine) -> Callable[[str], int]:
    """The ledger worker's queued composition, without the app's startup work.

    Only the two Defensive Rating jobs can be queued for 2025-26 here
    (``_authority`` refuses otherwise), and they need publications only.
    """

    from app.services.canonical_game_ledger import (
        CanonicalGameLedgerRepository,
        record_schema_drift,
    )
    from app.services.ledger_materialization import LedgerCorrectionQueue
    from app.services.ledger_runtime import LedgerRuntime

    governance = ActiveManifestLedgerGovernanceReader(engine)
    publications = PublicationService(engine, l15_expectation_resolver=governance)
    repository = CanonicalGameLedgerRepository(
        engine,
        correction_sink=LedgerCorrectionQueue(require_governance=True),
        schema_drift_sink=record_schema_drift,
    )
    return LedgerRuntime(
        backfill=None,
        repository=repository,
        materialization=None,
        governance=governance,
        publication_service=publications,
    ).compose_queued


def main(argv: Sequence[str] | None = None, *, provider: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--dry-run", action="store_true", help="fetch, validate and report without writing")
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="call stats.nba.com directly when COLLECTOR_NBA_PROXY_URL is not set",
    )
    args = parser.parse_args(argv)
    if not args.database_url.strip():
        parser.error("--database-url or DATABASE_URL is required")
    proxy = os.environ.get("COLLECTOR_NBA_PROXY_URL") or None
    if provider is None and proxy is None and not args.no_proxy:
        parser.error("COLLECTOR_NBA_PROXY_URL is not set; pass --no-proxy to call stats.nba.com directly")

    from app.collector.provider import NBAStatsProviderAdapter
    from app.config.settings import RuntimeSettings
    from app.utils.db import get_engine

    # The runtime's own engine (Railway's postgres:// URL normalized, the
    # configured pool) without building the app, whose startup would dispatch
    # queued refresh jobs.
    engine = get_engine(
        RuntimeSettings(
            environment="development",
            database={"url": args.database_url},
            auth={"firebase_admin_disabled": True},
        )
    )
    try:
        status, summary = run_backfill(
            engine,
            provider=provider or NBAStatsProviderAdapter(proxy=proxy),
            compose_queued=lambda: _composer(engine),
            now=datetime.now(UTC),
            dry_run=args.dry_run,
        )
    finally:
        engine.dispose()
    print(json.dumps(summary, sort_keys=True, default=str), flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
