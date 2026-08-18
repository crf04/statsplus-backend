"""Serialized unique authority resolution for matchup parity operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.publication_integrity import publication_payload_matches_checksum
from app.domain.utc import assume_utc
from app.models.collection_control import (
    ActiveSeason,
    CatalogPublication,
    CollectionManifest,
)


@dataclass(frozen=True, slots=True)
class UniqueMatchupAuthority:
    manifest_id: str
    catalog_id: str
    catalog_checksum: str


def lock_matchup_authority_serialization(session: Session, season: str) -> ActiveSeason:
    row = session.scalar(
        select(ActiveSeason)
        .where(ActiveSeason.season == season)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.status != "active" or row.phase != "Regular Season":
        raise ValueError("active_regular_season_required")
    return row


def resolve_unique_matchup_authority(
    session: Session, *, season: str, cutoff: datetime, lock: bool = True,
) -> UniqueMatchupAuthority:
    target_cutoff = assume_utc(cutoff)
    active = (
        lock_matchup_authority_serialization(session, season)
        if lock
        else session.get(ActiveSeason, season)
    )
    if (
        active is None
        or active.status != "active"
        or active.phase != "Regular Season"
        or (
            active.cutoff is not None
            and assume_utc(active.cutoff) != target_cutoff
        )
    ):
        raise ValueError("active_regular_season_required")

    authorities: list[UniqueMatchupAuthority] = []
    manifests = session.scalars(select(CollectionManifest).where(
        CollectionManifest.season == season,
        CollectionManifest.status == "active",
    ))
    for manifest in manifests:
        try:
            scopes = set(json.loads(manifest.scopes))
            versions = {int(value) for value in json.loads(manifest.accepted_versions)}
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            assume_utc(manifest.cutoff) != target_cutoff
            or "canonical_game_ledger" not in scopes
            or 1 not in versions
        ):
            continue
        catalog = session.get(CatalogPublication, manifest.event_catalog_publication_id)
        if (
            catalog is None
            or catalog.catalog_type != "event"
            or not catalog.complete
            or catalog.season != season
            or assume_utc(catalog.cutoff) != target_cutoff
            or catalog.checksum != manifest.event_catalog_checksum
            or not publication_payload_matches_checksum(catalog.payload, catalog.checksum)
        ):
            continue
        authorities.append(UniqueMatchupAuthority(
            manifest_id=manifest.manifest_id,
            catalog_id=catalog.publication_id,
            catalog_checksum=catalog.checksum,
        ))
    if len(authorities) != 1:
        raise ValueError("manifest_authority_ambiguous")
    return authorities[0]


__all__ = (
    "UniqueMatchupAuthority",
    "lock_matchup_authority_serialization",
    "resolve_unique_matchup_authority",
)
