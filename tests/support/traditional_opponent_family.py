"""A seeded traditional-opponent publication family on a real control plane.

Fifteen governed games give thirty teams one game each, which is the smallest
arrangement that is still a canonical league.  Both windows are composed and
active with real manifest, Event Catalog, and accepted ledger provenance, so
rebuild tests exercise the same authority production does rather than a
hand-built pointer.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.models.canonical_game_ledger import (
    CanonicalGameLedgerGame,
    LedgerObservationEvidence,
)
from app.models.collection_control import (
    CatalogPublication,
    CollectionManifest,
    CollectionObservation,
)
from app.services.collection_control import NBA_TEAM_IDS, PublicationService
from app.services.traditional_opponent_publications import (
    TRADITIONAL_OPPONENT_V1,
    TRADITIONAL_OPPONENT_V2,
)

UTC = UTC
SEASON = "2025-26"
NOW = datetime(2026, 8, 12, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 12, 4, tzinfo=UTC)
OPENING_CUTOFF = CUTOFF - timedelta(days=1)
GAME_COUNT = 15
TEAM_IDS = tuple(sorted(NBA_TEAM_IDS))
SEASON_STREAM = "traditional_opponent_season"
L15_STREAM = "traditional_opponent_l15"

#: One team's counts, with an exact rebound split (44 = 11 + 33).
COUNTS = {
    "points": 110.0,
    "rebounds": 44.0,
    "assists": 25.0,
    "field_goals_made": 40.0,
    "field_goals_attempted": 88.0,
    "three_pointers_made": 12.0,
    "three_pointers_attempted": 33.0,
    "free_throws_made": 18.0,
    "free_throws_attempted": 22.0,
    "turnovers": 14.0,
    "steals": 7.0,
    "blocks": 5.0,
    "personal_fouls": 19.0,
    "offensive_rebounds": 11.0,
    "defensive_rebounds": 33.0,
}


def game_id_for_team(team_id: int) -> str:
    return f"game-{TEAM_IDS.index(team_id) // 2}"


def payload(publication_format=TRADITIONAL_OPPONENT_V2, *, mutate=None):
    """The canonical thirty rows in one exact publication format."""

    rows = []
    for team_id in TEAM_IDS:
        block = {metric: COUNTS[metric] for metric in publication_format.metrics}
        rows.append(
            {
                "team_id": int(team_id),
                "team_tricode": f"T{TEAM_IDS.index(team_id):02d}",
                "game_ids": [game_id_for_team(team_id)],
                "game_count": 1,
                "counts": dict(block),
                "team_minutes": 48.0,
                "per48": dict(block),
                "league_average": dict(block),
                "population_sigma": {metric: 0.0 for metric in publication_format.metrics},
                "competition_rank": {metric: 1 for metric in publication_format.metrics},
            }
        )
    if mutate is not None:
        mutate(rows)
    return rows


def v2_payload(**kwargs):
    return payload(TRADITIONAL_OPPONENT_V2, **kwargs)


def v1_payload(**kwargs):
    """A payload in the retired format, for proving it is now refused."""

    return payload(TRADITIONAL_OPPONENT_V1, **kwargs)


def provenance():
    return {f"pbp:game-{index}": f"game-{index}" for index in range(GAME_COUNT)}


def seed_family(tmp_path, *, clock=None, publication_format=TRADITIONAL_OPPONENT_V2, opening_games=0):
    """Return an engine whose family is active in ``publication_format``.

    The first ``opening_games`` games were accepted the night before, under
    ``opening-manifest``, as an in-season ledger keeps them.
    """

    engine = create_engine(f"sqlite:///{tmp_path / 'family.sqlite3'}")
    run_migrations(engine)
    catalog_payload = "{}"
    catalog_checksum = hashlib.sha256(catalog_payload.encode()).hexdigest()
    with engine.begin() as connection:
        connection.execute(
            CatalogPublication.__table__.insert().values(
                publication_id="event-catalog",
                season=SEASON,
                catalog_type="event",
                cutoff=CUTOFF,
                version="v1",
                checksum=catalog_checksum,
                payload=catalog_payload,
                complete=True,
                published_at=NOW,
            )
        )
        connection.execute(
            CollectionManifest.__table__.insert().values(
                manifest_id="ledger-manifest",
                season=SEASON,
                cutoff=CUTOFF,
                collect_before=NOW + timedelta(hours=1),
                accepted_versions="[1]",
                scopes='["canonical_game_ledger"]',
                checksum="ledger-manifest",
                event_catalog_publication_id="event-catalog",
                event_catalog_checksum=catalog_checksum,
                status="active",
                created_at=NOW,
            )
        )
        connection.execute(
            CollectionManifest.__table__.insert().values(
                manifest_id="opening-manifest",
                season=SEASON,
                cutoff=OPENING_CUTOFF,
                collect_before=OPENING_CUTOFF + timedelta(hours=8),
                accepted_versions="[1]",
                scopes='["canonical_game_ledger"]',
                checksum="opening-manifest",
                event_catalog_publication_id="event-catalog",
                event_catalog_checksum=catalog_checksum,
                status="superseded",
                created_at=OPENING_CUTOFF,
            )
        )
        connection.execute(
            CollectionObservation.__table__.insert(),
            [
                {
                    "observation_id": f"pbp:game-{index}",
                    "client_observation_id": f"pbp:game-{index}",
                    "collector_id": "test",
                    "manifest_id": "opening-manifest" if index < opening_games else "ledger-manifest",
                    "environment": "testing",
                    "provider": "pbp+nba_live_data",
                    "observation_type": "canonical_game_ledger",
                    "scope": json.dumps(
                        {
                            "game_id": f"game-{index}",
                            "surface": "canonical_game_ledger",
                        }
                    ),
                    "season": SEASON,
                    "cutoff": OPENING_CUTOFF if index < opening_games else CUTOFF,
                    "schema_version": 1,
                    "checksum": f"pbp:game-{index}",
                    "payload": "{}",
                    "payload_bytes": 2,
                    "retrieved_at": OPENING_CUTOFF if index < opening_games else NOW,
                    "accepted_at": OPENING_CUTOFF if index < opening_games else NOW,
                }
                for index in range(GAME_COUNT)
            ],
        )
        for index in range(GAME_COUNT):
            connection.execute(
                CanonicalGameLedgerGame.__table__.insert().values(
                    game_id=f"game-{index}",
                    season=SEASON,
                    season_type="Regular Season",
                    game_date=CUTOFF.date(),
                    home_team_id=int(TEAM_IDS[index * 2]),
                    home_team_tricode=f"T{index * 2:02d}",
                    away_team_id=int(TEAM_IDS[index * 2 + 1]),
                    away_team_tricode=f"T{index * 2 + 1:02d}",
                    status="final",
                    source_observation_id=f"pbp:game-{index}",
                    checksum=f"{index:064d}",
                    raw_checksum=f"{index:064d}",
                    retrieved_at=NOW,
                    updated_at=NOW,
                )
            )
            connection.execute(
                LedgerObservationEvidence.__table__.insert().values(
                    observation_id=f"pbp:game-{index}",
                    game_id=f"game-{index}",
                    created_at=NOW,
                )
            )
    publications = PublicationService(engine, clock=clock or (lambda: NOW))
    publications.register_default_streams()
    for stream_key in (SEASON_STREAM, L15_STREAM):
        publications.register_stream(
            stream_key,
            provider="ledger",
            owner="railway",
            required_observations=["canonical_game_ledger"],
            publication_strategy="ledger_compose",
            enabled=True,
        )
        publications.recompose_ledger(
            stream_key,
            season=SEASON,
            cutoff=CUTOFF,
            payload=payload(publication_format),
            provenance=provenance(),
            reason="initial ledger composition",
        )
    return engine


def active_expectation(publications):
    """The expected active pair and fences an operator would approve."""

    from app.services.traditional_opponent_rebuild import FamilyExpectation

    season = publications.current(SEASON_STREAM)
    l15 = publications.current(L15_STREAM)
    with publications.session() as session:
        from app.models.collection_control import PublicationPointer

        fences = {
            stream_key: int(session.get(PublicationPointer, stream_key).fence)
            for stream_key in (SEASON_STREAM, L15_STREAM)
        }
    return FamilyExpectation(
        season_publication_id=season.publication_id,
        season_fence=fences[SEASON_STREAM],
        l15_publication_id=l15.publication_id,
        l15_fence=fences[L15_STREAM],
    )
