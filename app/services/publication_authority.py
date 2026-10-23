"""Immutable manifest and Event Catalog authority for publication versions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import defer

from app.domain.publication_integrity import publication_payload_matches_checksum
from app.domain.utc import assume_utc
from app.models.collection_control import CatalogPublication, CollectionManifest
from app.services.team_matchup_publications import (
    PublicationGovernanceUnavailable,
)


@dataclass(frozen=True, slots=True)
class PublicationAuthority:
    manifest_id: str
    event_catalog_publication_id: str
    event_catalog_checksum: str


def _manifest_authority(
    session, manifest: CollectionManifest, verified_payloads: set[tuple[str, str]] | None = None
) -> PublicationAuthority:
    try:
        scopes = set(json.loads(manifest.scopes))
        accepted_versions = {int(value) for value in json.loads(manifest.accepted_versions)}
    except (TypeError, ValueError, json.JSONDecodeError):
        raise PublicationGovernanceUnavailable() from None
    if (
        not manifest.event_catalog_publication_id
        or not manifest.event_catalog_checksum
        or "canonical_game_ledger" not in scopes
        or 1 not in accepted_versions
    ):
        raise PublicationGovernanceUnavailable()
    catalog = session.get(
        CatalogPublication,
        manifest.event_catalog_publication_id,
        options=(defer(CatalogPublication.payload),),
    )
    if (
        catalog is None
        or not catalog.complete
        or catalog.catalog_type != "event"
        or catalog.season != manifest.season
        or assume_utc(catalog.cutoff) != assume_utc(manifest.cutoff)
        or catalog.checksum != manifest.event_catalog_checksum
    ):
        raise PublicationGovernanceUnavailable()
    # The payload is loaded only to check it against its checksum, once per
    # caller-held set of payloads that already matched.
    payload_key = (catalog.publication_id, catalog.checksum)
    if verified_payloads is None or payload_key not in verified_payloads:
        if not publication_payload_matches_checksum(catalog.payload, catalog.checksum):
            raise PublicationGovernanceUnavailable()
        if verified_payloads is not None:
            verified_payloads.add(payload_key)
    return PublicationAuthority(
        manifest_id=manifest.manifest_id,
        event_catalog_publication_id=catalog.publication_id,
        event_catalog_checksum=catalog.checksum,
    )


def resolve_publication_authority(
    session,
    *,
    season: str,
    cutoff: datetime,
    manifest_id: str | None,
) -> PublicationAuthority:
    """Resolve one exact authority, refusing ambiguous season/cutoff lookup."""

    if manifest_id:
        manifest = session.get(CollectionManifest, manifest_id)
        manifests = [manifest] if manifest is not None else []
    else:
        manifests = list(
            session.scalars(
                select(CollectionManifest).where(
                    CollectionManifest.season == season,
                    CollectionManifest.cutoff == assume_utc(cutoff),
                )
            )
        )
    if len(manifests) != 1:
        raise PublicationGovernanceUnavailable()
    manifest = manifests[0]
    if manifest.season != season or assume_utc(manifest.cutoff) != assume_utc(cutoff):
        raise PublicationGovernanceUnavailable()
    return _manifest_authority(session, manifest)


def verify_publication_authority(
    session, publication, *, verified_payloads: set[tuple[str, str]] | None = None
) -> PublicationAuthority:
    """Verify a version still names its exact immutable authorizing rows.

    ``verified_payloads`` holds ``(publication_id, checksum)`` of Event
    Catalogs whose payload already matched; their payload is not fetched.
    """

    if (
        not publication.manifest_id
        or not publication.event_catalog_publication_id
        or not publication.event_catalog_checksum
    ):
        raise PublicationGovernanceUnavailable()
    manifest = session.get(CollectionManifest, publication.manifest_id)
    if manifest is None:
        raise PublicationGovernanceUnavailable()
    authority = _manifest_authority(session, manifest, verified_payloads)
    if (
        publication.season != manifest.season
        or assume_utc(publication.cutoff) != assume_utc(manifest.cutoff)
        or publication.event_catalog_publication_id != authority.event_catalog_publication_id
        or publication.event_catalog_checksum != authority.event_catalog_checksum
    ):
        raise PublicationGovernanceUnavailable()
    return authority


__all__ = [
    "PublicationAuthority",
    "resolve_publication_authority",
    "verify_publication_authority",
]
