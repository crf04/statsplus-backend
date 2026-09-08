"""A bounded daily authorization tick for the residential player-shot writer."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.collection_control import (
    ActiveSeason, AuditEvent, BootstrapRequest, CatalogPublication,
    CollectionManifest, CompositionJob, PublicationPointer, PublicationRepairGroup,
    PublicationStream, PublicationVersion,
)
from app.services.collection_control import CollectionControlService, ControlPlaneError
from app.services.matchup_authority import lock_matchup_authority_serialization

#: The residential player-shooting streams this daily tick authorizes.  Each
#: is scheduled on its own enabled flag: one being disabled -- or having no
#: evidence yet -- must not stop the other being collected, and one manifest
#: covers whichever are enabled rather than one manifest per stream.
STREAMS = ("grouped_shot_types", "exact_shot_zones")
CENTRAL = ZoneInfo("America/Chicago")
ACTOR = "player_shooting_refresh"
REASON = "Daily residential player shooting refresh"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def daily_window(now: datetime) -> tuple[datetime, datetime]:
    if now.tzinfo is None:
        raise ValueError("refresh clock must include a timezone")
    day = now.astimezone(CENTRAL).date()
    return tuple(datetime.combine(day, hour, CENTRAL).astimezone(timezone.utc)
                 for hour in (time(3, 45), time(11)))


class PlayerShootingRefresh:
    def __init__(self, engine, *, composer_factory, clock):
        self.engine = engine
        self.composer_factory = composer_factory
        self.clock = clock

    def tick(self, season: str) -> dict[str, object]:
        now = _utc(self.clock())
        cutoff, deadline = daily_window(now)
        # Do not initialize the application/provider graph on idle cron ticks.
        with self.engine.connect() as connection:
            active = connection.execute(select(ActiveSeason.season).where(
                ActiveSeason.season == season, ActiveSeason.status == "active",
                ActiveSeason.phase == "Regular Season",
            )).first()
            enabled = {
                row[0] for row in connection.execute(
                    select(PublicationStream.stream_key).where(
                        PublicationStream.stream_key.in_(STREAMS),
                        PublicationStream.enabled.is_(True),
                    )
                )
            }
            queued = connection.execute(select(CompositionJob.job_id).where(
                CompositionJob.season == season, CompositionJob.status == "queued",
            ).limit(1)).first() if active and enabled else None
        if not active or not enabled:
            return {"state": "disabled" if active else "season_inactive", "composed_jobs": 0}
        composed = self.composer_factory().compose_queued(season) if queued else 0
        if not cutoff <= now < deadline:
            return {"state": "outside_window", "composed_jobs": composed}
        with self.engine.begin() as connection, Session(connection) as session, session.begin():
            # Every decision and the existing nested control-service writes
            # share this transaction and the lock also used by manual writers.
            active = lock_matchup_authority_serialization(session, season)
            streams = {
                row.stream_key
                for row in session.scalars(select(PublicationStream).where(
                    PublicationStream.stream_key.in_(STREAMS),
                ).with_for_update()).all()
                if row.enabled
            }
            if not streams:
                return {"state": "disabled", "composed_jobs": composed}
            state = self._authorize(
                connection, session, active, season, now, cutoff, deadline,
                streams=streams,
            )
        return {"state": state, "composed_jobs": composed, "cutoff": cutoff.isoformat()}

    def _authorize(self, connection, session, active, season, now, cutoff,
                   deadline, *, streams):
        manifests = session.scalars(select(CollectionManifest).where(
            CollectionManifest.season == season,
        ).order_by(CollectionManifest.cutoff.desc(), CollectionManifest.created_at.desc())).all()
        latest = manifests[0] if manifests else None
        newer_catalog = session.scalar(select(CatalogPublication.publication_id).where(
            CatalogPublication.season == season, CatalogPublication.cutoff > cutoff,
        ).limit(1))
        newer_request = session.scalar(select(BootstrapRequest.request_id).where(
            BootstrapRequest.season == season, BootstrapRequest.cutoff > cutoff,
            BootstrapRequest.status == "pending", BootstrapRequest.expires_at > now,
        ).limit(1))
        if ((active.cutoff is not None and _utc(active.cutoff) > cutoff)
                or (latest and _utc(latest.cutoff) > cutoff) or newer_catalog or newer_request):
            return "newer_authority"
        current = [row for row in manifests if row.status == "active"]
        pending_repair = session.scalar(select(PublicationRepairGroup.group_id).join(
            CollectionManifest, CollectionManifest.manifest_id == PublicationRepairGroup.manifest_id,
        ).where(CollectionManifest.season == season, CollectionManifest.status == "active",
                PublicationRepairGroup.promoted_at.is_(None)).limit(1))
        if pending_repair:
            return "repair_pending"
        for manifest in current:
            # Every enabled stream must already be in the open manifest.  A
            # manifest that covers only the sibling is not authority for a
            # stream enabled since it was issued.
            if (
                _utc(manifest.cutoff) == cutoff
                and _utc(manifest.collect_before) >= deadline
                and streams <= set(json.loads(manifest.scopes))
            ):
                return "manifest_ready"
            if _utc(manifest.collect_before) > now and not self._complete(session, manifest):
                return "collection_pending"
        scopes = set(streams)
        for manifest in current or ([latest] if latest else []):
            # Sibling scopes this tick does not manage are preserved as
            # before.  Membership of the player-shooting streams, though,
            # follows the currently enabled set: a stream disabled since the
            # prior manifest was issued must not be copied forward into new
            # collectible work, which would leave it discoverable and
            # executable by the collector after being turned off.
            scopes.update(set(json.loads(manifest.scopes)) - set(STREAMS))
        control = CollectionControlService(connection, clock=lambda: now)
        # create_manifest remains the authority for freshness, identity,
        # completeness, and immutable Event Catalog binding.
        try:
            manifest = control.create_manifest(season, cutoff=cutoff, scopes=scopes,
                                               collect_before=deadline)
        except ControlPlaneError as error:
            kind = {"event_catalog_required": "event", "athlete_catalog_required": "athlete"}.get(error.reason)
            if kind is None:
                raise
            pending = session.scalar(select(BootstrapRequest.request_id).where(
                BootstrapRequest.season == season, BootstrapRequest.catalog_type == kind,
                BootstrapRequest.cutoff == cutoff, BootstrapRequest.status == "pending",
                BootstrapRequest.expires_at > now,
            ).limit(1))
            if not pending:
                request = control.create_bootstrap_request(season, kind, cutoff=cutoff, session=session)
                request.expires_at = deadline
                self._audit(session, f"player_shooting.{kind}_requested", request.request_id, now)
            return f"awaiting_{kind}"
        self._audit(session, "player_shooting.manifest_created", manifest.manifest_id, now)
        return "manifest_ready"

    @staticmethod
    def _complete(session, manifest):
        scopes = set(json.loads(manifest.scopes))
        streams = session.scalars(select(PublicationStream).where(PublicationStream.enabled.is_(True))).all()
        for stream in streams:
            if stream.publication_strategy in {"never_schedule", "request_time"}:
                continue
            if stream.stream_key not in scopes and not set(json.loads(stream.required_observations)) & scopes:
                continue
            version = session.scalar(select(PublicationVersion).join(
                PublicationPointer, PublicationPointer.active_publication_id == PublicationVersion.publication_id,
            ).where(PublicationPointer.stream_key == stream.stream_key,
                    PublicationVersion.season == manifest.season))
            if version is None or _utc(version.cutoff) < _utc(manifest.cutoff):
                return False
        return True

    @staticmethod
    def _audit(session, action, resource, now):
        session.add(AuditEvent(event_id=str(uuid.uuid4()), actor=ACTOR, action=action,
                               resource=resource, reason=REASON, details="{}", created_at=now))
