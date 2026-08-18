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
    season: str
    cutoff: datetime
    manifest_id: str
    manifest_checksum: str
    catalog_id: str
    catalog_checksum: str
    team_ids: frozenset[int]
    season_game_ids_by_team: tuple[tuple[int, frozenset[str]], ...]
    l15_game_ids_by_team: tuple[tuple[int, frozenset[str]], ...]
    provider_start_date: str
    provider_end_date: str

    @property
    def expected_game_ids(self) -> frozenset[str]:
        return frozenset(
            game_id
            for _, game_ids in self.season_game_ids_by_team
            for game_id in game_ids
        )

    def require_completed_regular_season(self) -> None:
        """Reject partial/midseason authority for the 2025-26 cutover."""

        counts = {
            team_id: len(game_ids)
            for team_id, game_ids in self.season_game_ids_by_team
        }
        l15 = dict(self.l15_game_ids_by_team)
        if (
            self.season != "2025-26"
            or len(self.team_ids) != 30
            or set(counts) != set(self.team_ids)
            or set(counts.values()) != {82}
            or len(self.expected_game_ids) != 1230
            or set(l15) != set(self.team_ids)
            or any(
                len(game_ids) != 15
                or not game_ids.issubset(dict(self.season_game_ids_by_team)[team_id])
                for team_id, game_ids in l15.items()
            )
        ):
            raise ValueError("completed_2025_26_regular_season_required")


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
        try:
            document = json.loads(catalog.payload)
            events = document["events"]
            completed = tuple(
                event for event in events
                if (
                    event.get("phase") == "Regular Season"
                    and (
                        event.get("completed") is True
                        or event.get("status_code") == 3
                        or str(event.get("status", "")).strip().lower()
                        in {"final", "final/ot", "final/2ot", "final/3ot"}
                    )
                    and not event.get("postponed_status")
                )
            )
            chronological = tuple(sorted(
                completed, key=lambda event: (
                    str(event["scheduled_at"]), str(event["nba_game_id"]),
                )
            ))
            team_ids = frozenset(
                int(team_id) for event in completed
                for team_id in (event["home_team_id"], event["away_team_id"])
            )
            by_team = tuple(sorted(
                (
                    team_id,
                    frozenset(
                        str(event["nba_game_id"])
                        for event in completed
                        if team_id in {
                            int(event["home_team_id"]), int(event["away_team_id"]),
                        }
                    ),
                )
                for team_id in team_ids
            ))
            l15_by_team = tuple(sorted(
                (
                    team_id,
                    frozenset(
                        tuple(
                            str(event["nba_game_id"])
                            for event in reversed(chronological)
                            if team_id in {
                                int(event["home_team_id"]), int(event["away_team_id"]),
                            }
                        )[:15]
                    ),
                )
                for team_id in team_ids
            ))
            dates = tuple(
                datetime.fromisoformat(str(event["scheduled_at"])).date().isoformat()
                for event in completed
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        authorities.append(UniqueMatchupAuthority(
            season=season,
            cutoff=target_cutoff,
            manifest_id=manifest.manifest_id,
            manifest_checksum=manifest.checksum,
            catalog_id=catalog.publication_id,
            catalog_checksum=catalog.checksum,
            team_ids=team_ids,
            season_game_ids_by_team=by_team,
            l15_game_ids_by_team=l15_by_team,
            provider_start_date=min(dates),
            provider_end_date=max(dates),
        ))
    if len(authorities) != 1:
        raise ValueError("manifest_authority_ambiguous")
    return authorities[0]


__all__ = (
    "UniqueMatchupAuthority",
    "lock_matchup_authority_serialization",
    "resolve_unique_matchup_authority",
)
