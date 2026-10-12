"""A control plane that has published 2025-26 and collected 2026-27.

Each season has its own Event Catalog, ``canonical_game_ledger`` manifest,
accepted ledger observations, and canonical games, so every publication rests
on the same kind of authority production uses.  ``player_game_logs`` and both
traditional-opponent windows are enabled and active on 2025-26; ``player_per36``
stays registered but disabled.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select

from app.migrations import run_migrations
from app.models.canonical_game_ledger import (
    CanonicalGameLedgerGame,
    LedgerObservationEvidence,
)
from app.models.collection_control import (
    CatalogPublication,
    CollectionManifest,
    CollectionObservation,
    PublicationPointer,
    PublicationVersion,
)
from app.services.collection_control import NBA_TEAM_IDS, PublicationService
from tests.services.test_matchup_selection_service import _log_row
from tests.support.traditional_opponent_family import v2_payload

UTC = timezone.utc
OLD = "2025-26"
NEW = "2026-27"
OLD_CUTOFF = datetime(2026, 4, 13, 4, tzinfo=UTC)
NEW_CUTOFF = datetime(2026, 10, 24, 4, tzinfo=UTC)
#: A second 2026-27 manifest, for candidates that rest on other authority.
OTHER_CUTOFF = datetime(2026, 10, 25, 4, tzinfo=UTC)
NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
TEAM_IDS = tuple(sorted(NBA_TEAM_IDS))

PLAYER_LOGS = "player_game_logs"
SEASON_STREAM = "traditional_opponent_season"
L15_STREAM = "traditional_opponent_l15"
PER36 = "player_per36"
ENABLED_STREAMS = (PLAYER_LOGS, SEASON_STREAM, L15_STREAM)

#: ``(home index, away index)`` into ``TEAM_IDS`` for each 2026-27 game.
NEW_GAMES = ((0, 1), (2, 0), (1, 3))


def _authority(connection, *, season, manifest_id, cutoff, games):
    """Insert one catalog, manifest, and its accepted ledger games."""

    catalog_payload = json.dumps({"manifest": manifest_id})
    catalog_checksum = hashlib.sha256(catalog_payload.encode()).hexdigest()
    catalog_id = f"catalog-{manifest_id}"
    connection.execute(CatalogPublication.__table__.insert().values(
        publication_id=catalog_id, season=season, catalog_type="event",
        cutoff=cutoff, version="v1", checksum=catalog_checksum,
        payload=catalog_payload, complete=True, published_at=cutoff,
    ))
    connection.execute(CollectionManifest.__table__.insert().values(
        manifest_id=manifest_id, season=season, cutoff=cutoff,
        collect_before=NOW + timedelta(days=1), accepted_versions="[1]",
        scopes='["canonical_game_ledger"]', checksum=manifest_id,
        event_catalog_publication_id=catalog_id,
        event_catalog_checksum=catalog_checksum,
        status="active", created_at=cutoff,
    ))
    for game_id, home, away in games:
        observation_id = f"pbp:{game_id}"
        connection.execute(CollectionObservation.__table__.insert().values(
            observation_id=observation_id,
            client_observation_id=observation_id,
            collector_id="test",
            manifest_id=manifest_id,
            environment="testing",
            provider="pbp+nba_live_data",
            observation_type="canonical_game_ledger",
            scope=json.dumps({
                "game_id": game_id, "surface": "canonical_game_ledger",
            }),
            season=season,
            cutoff=cutoff,
            schema_version=1,
            checksum=observation_id,
            payload="{}",
            payload_bytes=2,
            retrieved_at=cutoff,
            accepted_at=cutoff,
        ))
        connection.execute(CanonicalGameLedgerGame.__table__.insert().values(
            game_id=game_id, season=season, season_type="Regular Season",
            game_date=cutoff.date(),
            home_team_id=int(TEAM_IDS[home]), home_team_tricode=f"T{home:02d}",
            away_team_id=int(TEAM_IDS[away]), away_team_tricode=f"T{away:02d}",
            status="final", source_observation_id=observation_id,
            checksum=hashlib.sha256(game_id.encode()).hexdigest(),
            raw_checksum=hashlib.sha256(game_id.encode()).hexdigest(),
            retrieved_at=cutoff, updated_at=cutoff,
        ))
        connection.execute(LedgerObservationEvidence.__table__.insert().values(
            observation_id=observation_id, game_id=game_id, created_at=cutoff,
        ))


def provenance(prefix: str, count: int) -> dict[str, str]:
    return {f"pbp:{prefix}-{index}": f"{prefix}-{index}" for index in range(count)}


OLD_PROVENANCE = provenance("old", 15)
NEW_PROVENANCE = provenance("new", len(NEW_GAMES))
OTHER_PROVENANCE = provenance("other", 1)


def payload_for(stream_key: str, season: str, *, points: int = 10):
    """A payload the stream's strict read-side decoder accepts."""

    if stream_key == PLAYER_LOGS:
        row = _log_row(
            game_id="0022500001", game_date="2026-01-02", minutes=30.0,
            points=points,
        )
        return {"rows": [{**row, "season": season}]}
    return v2_payload()


def seed_world(tmp_path, *, engine=None):
    """Return ``(engine, publications)`` with 2025-26 published."""

    if engine is None:
        engine = create_engine(f"sqlite:///{tmp_path / 'cutover.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        _authority(
            connection, season=OLD, manifest_id="manifest-old",
            cutoff=OLD_CUTOFF,
            games=[
                (f"old-{index}", index * 2, index * 2 + 1)
                for index in range(15)
            ],
        )
        _authority(
            connection, season=NEW, manifest_id="manifest-new",
            cutoff=NEW_CUTOFF,
            games=[
                (f"new-{index}", home, away)
                for index, (home, away) in enumerate(NEW_GAMES)
            ],
        )
        _authority(
            connection, season=NEW, manifest_id="manifest-other",
            cutoff=OTHER_CUTOFF, games=[("other-0", 4, 5)],
        )
    publications = PublicationService(engine, clock=lambda: NOW)
    publications.register_default_streams()
    for stream_key in ENABLED_STREAMS:
        publications.register_stream(
            stream_key, provider="ledger", owner="railway",
            required_observations=["canonical_game_ledger"],
            publication_strategy="ledger_compose", enabled=True,
        )
        publications.recompose_ledger(
            stream_key, season=OLD, cutoff=OLD_CUTOFF,
            payload=payload_for(stream_key, OLD), provenance=OLD_PROVENANCE,
            reason="2025-26 ledger composition",
        )
    return engine, publications


def compose_new(publications, stream_key, *, points=11):
    """Compose ``stream_key`` from the 2026-27 ledger, as the nightly job does."""

    return publications.recompose_ledger(
        stream_key, season=NEW, cutoff=NEW_CUTOFF,
        payload=payload_for(stream_key, NEW, points=points),
        provenance=NEW_PROVENANCE, reason="2026-27 ledger composition",
    )


def pointers(engine):
    """``{stream_key: (active publication_id, season, status, fence)}``."""

    with engine.connect() as connection:
        rows = connection.execute(
            select(
                PublicationPointer.stream_key,
                PublicationPointer.active_publication_id,
                PublicationVersion.season,
                PublicationVersion.status,
                PublicationPointer.fence,
            ).join(
                PublicationVersion,
                PublicationVersion.publication_id
                == PublicationPointer.active_publication_id,
            )
        ).all()
    return {row[0]: tuple(row[1:]) for row in rows}
