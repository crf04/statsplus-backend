"""Immutable manifest and Event Catalog authority for publication versions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json

from sqlalchemy import select

from app.domain.publication_integrity import publication_payload_matches_checksum
from app.domain.utc import assume_utc
from app.models.collection_control import CatalogPublication, CollectionManifest


@dataclass(frozen=True, slots=True)
class PublicationAuthority:
    manifest_id: str
    event_catalog_publication_id: str
    event_catalog_checksum: str


def _manifest_authority(session, manifest: CollectionManifest) -> PublicationAuthority:
    try:
        scopes = set(json.loads(manifest.scopes))
        accepted_versions = {
            int(value) for value in json.loads(manifest.accepted_versions)
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("publication authority is unavailable") from None
    if (
        not manifest.event_catalog_publication_id
        or not manifest.event_catalog_checksum
        or "canonical_game_ledger" not in scopes
        or 1 not in accepted_versions
    ):
        raise ValueError("publication authority is unavailable")
    catalog = session.get(
        CatalogPublication,
        manifest.event_catalog_publication_id,
    )
    if (
        catalog is None
        or not catalog.complete
        or catalog.catalog_type != "event"
        or catalog.season != manifest.season
        or assume_utc(catalog.cutoff) != assume_utc(manifest.cutoff)
        or catalog.checksum != manifest.event_catalog_checksum
        or not publication_payload_matches_checksum(
            catalog.payload, catalog.checksum
        )
    ):
        raise ValueError("publication authority is unavailable")
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
        manifests = list(session.scalars(select(CollectionManifest).where(
            CollectionManifest.season == season,
            CollectionManifest.cutoff == assume_utc(cutoff),
        )))
    if len(manifests) != 1:
        raise ValueError("publication authority is unavailable")
    manifest = manifests[0]
    if (
        manifest.season != season
        or assume_utc(manifest.cutoff) != assume_utc(cutoff)
    ):
        raise ValueError("publication authority is unavailable")
    return _manifest_authority(session, manifest)


def verify_publication_authority(session, publication) -> PublicationAuthority:
    """Verify a version still names its exact immutable authorizing rows."""

    if (
        not publication.manifest_id
        or not publication.event_catalog_publication_id
        or not publication.event_catalog_checksum
    ):
        raise ValueError("publication authority is unavailable")
    manifest = session.get(CollectionManifest, publication.manifest_id)
    if manifest is None:
        raise ValueError("publication authority is unavailable")
    authority = _manifest_authority(session, manifest)
    if (
        publication.season != manifest.season
        or assume_utc(publication.cutoff) != assume_utc(manifest.cutoff)
        or publication.event_catalog_publication_id
        != authority.event_catalog_publication_id
        or publication.event_catalog_checksum != authority.event_catalog_checksum
    ):
        raise ValueError("publication authority is unavailable")
    return authority


__all__ = [
    "PublicationAuthority",
    "resolve_publication_authority",
    "verify_publication_authority",
]
