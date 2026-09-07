"""The governed player Synergy path, from source receipts to profile reads."""

from datetime import timedelta
import gzip
import hashlib
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collector.normalizers import normalize_synergy_response, PLAY_TYPES
from app.models.collection_control import (
    CollectionManifest,
    CollectionObservation,
    CompositionJob,
    PublicationPointer,
    PublicationStream,
    PublicationVersion,
)
from app.services import collection_control as module
from app.services.collection_control import (
    CollectionControlService,
    CollectionOperationsService,
    CollectorTokenService,
    ObservationIngestionService,
    ControlPlaneError,
)
from app.services.database_first_activation import DatabaseFirstPublicationReader
from tests.test_residential_collector import (
    NOW,
    _grouped_shot_type_control_plane,
    _compose_queued_slice,
)


@pytest.fixture
def plane(tmp_path):
    engine, _, publications, _ = _grouped_shot_type_control_plane(
        tmp_path, "synergy.sqlite3"
    )
    control = CollectionControlService(engine, clock=lambda: NOW)
    manifest = control.create_manifest(
        "2025-26",
        cutoff=NOW,
        scopes={"grouped_shot_types", "synergy_play_types", "canonical_game_ledger"},
        collect_before=NOW + timedelta(hours=1),
    )
    CollectionOperationsService(
        engine, publication_service=publications, clock=lambda: NOW
    ).activate_stream(
        "synergy_play_types", actor="test", reason="first player Synergy collection"
    )
    tokens = CollectorTokenService(
        engine, environment="testing", signing_secret="test", clock=lambda: NOW
    )
    identity = tokens.create_identity(
        "test", scopes=["ingest"], providers=["nba"], surfaces=["synergy_play_types"]
    )
    claims = tokens.validate(
        tokens.issue_for_secret(identity["identity_id"], identity["secret"])
    )
    ticks = iter(range(1, 10000))
    ingest = ObservationIngestionService(
        engine,
        publication_service=publications,
        clock=lambda: NOW + timedelta(seconds=next(ticks)),
    )

    def deliver(*, suffix="", categories=PLAY_TYPES, share=0.01, ambiguous=False):
        receipts = []
        for category in categories:
            rows = [
                {
                    "PLAYER_ID": 2544,
                    "TEAM_ID": 1610612737,
                    "GP": 20,
                    "POSS": 1,
                    "POSS_PCT": share,
                    "PLAY_TYPE": category,
                }
            ]
            if ambiguous:
                rows += [
                    {
                        "PLAYER_ID": 2,
                        "TEAM_ID": team,
                        "GP": 10,
                        "POSS": 1,
                        "POSS_PCT": 0.001,
                        "PLAY_TYPE": category,
                    }
                    for team in (1610612737, 1610612738)
                ]
            observation = normalize_synergy_response(
                rows,
                season="2025-26",
                cutoff=NOW,
                scope={
                    "window": "season",
                    "subject": "player",
                    "phase": "Regular Season",
                    "play_type": category,
                },
            )
            raw = json.dumps(
                observation.payload, sort_keys=True, separators=(",", ":")
            ).encode()
            envelope = {
                "client_observation_id": category + suffix,
                "observation_type": "synergy_play_types",
                "provider": "nba",
                "season": "2025-26",
                "cutoff": NOW.isoformat(),
                "schema_version": 2,
                "retrieved_at": NOW.isoformat(),
                "manifest_id": manifest.manifest_id,
                "scope": observation.scope,
                "environment": "testing",
                "checksum": hashlib.sha256(raw).hexdigest(),
            }
            receipts.append(
                ingest.ingest(claims, envelope, gzip.compress(raw), compressed=True)
            )
        return receipts

    return engine, manifest, publications, deliver


def test_synergy_ingestion_queue_publication_profile_and_replay(plane):
    from app.services.player_diet import PlayerDietRepository
    from app.services.player_service import PlayerProfileReader, PlayerService
    from tests.services.test_player_service import _catalog_row, _settings

    engine, manifest, publications, deliver = plane
    # Seed a valid old player fact before the governed publication takes over.
    from app.models.player_diet import (
        PlayerDietFactRow,
        PlayerDietSurfaceObservationRow,
    )

    with engine.begin() as connection:
        connection.execute(
            PlayerDietFactRow.__table__.insert().values(
                season="2025-26",
                player_id=2,
                base="play_types",
                slice_key="Isolation",
                share=0.2,
                volume=20,
                games_played=10,
                volume_unit="possessions",
                provider="nba_synergy",
                retrieved_at=NOW,
            )
        )
        connection.execute(
            PlayerDietSurfaceObservationRow.__table__.insert().values(
                season="2025-26",
                base="play_types",
                status="available",
                retrieved_at=NOW,
            )
        )
    receipts = deliver(ambiguous=True)
    replay = deliver(ambiguous=True)
    assert [r.observation_id for r in receipts] == [r.observation_id for r in replay]
    assert _compose_queued_slice(engine) == 1
    with Session(engine) as session:
        jobs = session.scalars(
            select(CompositionJob).where(
                CompositionJob.stream_key == "synergy_play_types"
            )
        ).all()
        assert len(jobs) == 1 and jobs[0].status == "succeeded"
    with Session(engine) as session:
        pointer = session.get(PublicationPointer, "synergy_play_types")
        version = session.get(PublicationVersion, pointer.active_publication_id)
    payload = json.loads(version.payload)
    assert version.manifest_id == manifest.manifest_id
    assert len(payload["source_observations"]) == 11
    assert {row["slice_key"] for row in payload["rows"]} == set(PLAY_TYPES)
    assert payload["withheld_players"] == [
        {"player_id": 2, "reason": "team possession denominator is not uniquely proven"}
    ]

    class Catalog:
        def get_catalog(self, season, *, active_only=False):
            return [
                _catalog_row(2544, "Valid Player"),
                _catalog_row(2, "Withheld Player"),
            ]

    diets = PlayerDietRepository(
        engine, publication_reader=DatabaseFirstPublicationReader(engine)
    )
    service = PlayerService(
        engine,
        profile_reader=PlayerProfileReader(Catalog(), diets),
        settings=_settings(),
    )
    profile = service.get_player_profile("Valid Player", "Playtypes")
    assert all(profile[category + "%"] == 1 for category in PLAY_TYPES)
    # Withholding one player does not mark the entire source unavailable or
    # permit legacy facts to refill that player's profile.
    result = diets.get_for_players("2025-26", [2, 2544])
    assert not [fact for fact in result.players.get(2, ()) if fact.base == "play_types"]
    assert any(
        o.base == "play_types" and o.status == "available" for o in result.observations
    )
    from app.errors import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError):
        service.get_player_profile("Withheld Player", "Playtypes")


def test_synergy_latest_per_category_retry_preserves_all_eleven(plane):
    engine, manifest, publications, deliver = plane
    deliver()
    deliver(suffix="-retry", categories=("Isolation",), share=0.02)
    version = publications.compose_from_observations(
        "synergy_play_types",
        season="2025-26",
        cutoff=NOW,
        manifest_id=manifest.manifest_id,
    )
    payload = json.loads(version.payload)
    assert len(payload["rows"]) == len(payload["source_observations"]) == 11
    shares = {row["slice_key"]: row["share"] for row in payload["rows"]}
    assert shares["Isolation"] == 0.02
    assert shares["Cut"] == 0.01


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "empty",
        "category",
        "checksum",
        "provider",
        "season",
        "cutoff",
        "manifest",
        "window",
        "phase",
        "subject",
        "value_mode",
        "type_grouping",
    ],
)
def test_synergy_bad_source_candidate_keeps_last_good(plane, defect):
    engine, manifest, publications, deliver = plane
    deliver()
    good = publications.compose_from_observations(
        "synergy_play_types",
        season="2025-26",
        cutoff=NOW,
        manifest_id=manifest.manifest_id,
    )
    with Session(engine) as session, session.begin():
        observations = session.scalars(
            select(CollectionObservation).where(
                CollectionObservation.observation_type == "synergy_play_types"
            )
        ).all()
        chosen = next(
            row
            for row in observations
            if json.loads(row.scope)["play_type"] == "Isolation"
        )
        ids = {row.observation_id for row in observations}
        if defect == "missing":
            ids.remove(chosen.observation_id)
        elif defect in {"empty", "category"}:
            payload = json.loads(chosen.payload)
            if defect == "empty":
                payload["records"] = []
            else:
                payload["records"][0]["category"] = "Cut"
            chosen.payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            chosen.checksum = hashlib.sha256(chosen.payload.encode()).hexdigest()
        elif defect == "checksum":
            chosen.checksum = "0" * 64
        elif defect == "provider":
            chosen.provider = "pbp"
        elif defect == "season":
            chosen.season = "2024-25"
        elif defect == "cutoff":
            chosen.cutoff = NOW - timedelta(days=1)
        elif defect == "manifest":
            chosen.manifest_id = session.scalar(
                select(CollectionManifest.manifest_id).where(
                    CollectionManifest.manifest_id != manifest.manifest_id
                )
            )
        else:
            scope = json.loads(chosen.scope)
            scope[defect] = {
                "window": "l15",
                "phase": "Playoffs",
                "subject": "opponent",
                "value_mode": "per_game",
                "type_grouping": "Defensive",
            }[defect]
            chosen.scope = json.dumps(scope)
        stream = session.get(PublicationStream, "synergy_play_types")
        with pytest.raises(ValueError):
            module._compose_player_diet_observation_payload(
                session,
                stream=stream,
                stream_key="synergy_play_types",
                season="2025-26",
                cutoff=NOW,
                manifest_id=manifest.manifest_id,
                provenance_ids=ids,
            )
    with Session(engine) as session:
        assert (
            session.get(PublicationPointer, "synergy_play_types").active_publication_id
            == good.publication_id
        )


def test_synergy_missing_category_does_not_pass_collection_gate(plane):
    _, manifest, publications, deliver = plane
    deliver(categories=PLAY_TYPES[:-1])
    with pytest.raises(ControlPlaneError):
        publications.compose_from_observations(
            "synergy_play_types",
            season="2025-26",
            cutoff=NOW,
            manifest_id=manifest.manifest_id,
        )


def test_synergy_invalid_new_generation_preserves_active_publication(plane):
    engine, manifest, publications, deliver = plane
    deliver()
    good = publications.compose_from_observations(
        "synergy_play_types",
        season="2025-26",
        cutoff=NOW,
        manifest_id=manifest.manifest_id,
    )
    deliver(suffix="-invalid", share=0.5)
    with pytest.raises(ControlPlaneError, match="publication_candidate_invalid"):
        publications.compose_from_observations(
            "synergy_play_types",
            season="2025-26",
            cutoff=NOW,
            manifest_id=manifest.manifest_id,
        )
    with Session(engine) as session:
        assert (
            session.get(PublicationPointer, "synergy_play_types").active_publication_id
            == good.publication_id
        )
        assert (
            session.get(PublicationVersion, good.publication_id).payload == good.payload
        )


def test_player_synergy_manifest_descriptor_freezes_source_contract():
    descriptors = module._collector_scope_descriptors({"synergy_play_types"}, NOW)
    assert len(descriptors) == 11
    assert {d["parameters"]["play_type"] for d in descriptors} == set(PLAY_TYPES)
    for descriptor in descriptors:
        assert descriptor["scope"] == "synergy_play_types"
        assert descriptor["parameters"] == {
            "play_type": descriptor["parameters"]["play_type"],
            "window": "season",
            "subject": "player",
            "subject_code": "P",
            "type_grouping": "Offensive",
            "per_mode": "Totals",
            "value_mode": "totals",
            "phase": "Regular Season",
        }
    definition = next(
        d for d in module.SURFACE_REGISTRY if d.stream_key == "synergy_play_types"
    )
    assert definition.required == ("synergy_play_types",)
