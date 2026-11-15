"""The governed player Synergy path, from source receipts to profile reads."""

import gzip
import hashlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collector.normalizers import PLAY_TYPES, normalize_synergy_response
from app.domain.publication_integrity import publication_payload_checksum
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
    NBA_TEAM_IDS,
    CollectionControlService,
    CollectionOperationsService,
    CollectorTokenService,
    ControlPlaneError,
    ObservationIngestionService,
)
from app.services.database_first_activation import DatabaseFirstPublicationReader
from tests.test_residential_collector import (
    NOW,
    _compose_queued_slice,
    _grouped_shot_type_control_plane,
)


@pytest.fixture
def plane(tmp_path):
    engine, _, publications, _ = _grouped_shot_type_control_plane(tmp_path, "synergy.sqlite3")
    control = CollectionControlService(engine, clock=lambda: NOW)
    manifest = control.create_manifest(
        "2025-26",
        cutoff=NOW,
        scopes={"grouped_shot_types", "synergy_play_types", "canonical_game_ledger"},
        collect_before=NOW + timedelta(hours=1),
    )
    CollectionOperationsService(engine, publication_service=publications, clock=lambda: NOW).activate_stream(
        "synergy_play_types", actor="test", reason="first player Synergy collection"
    )
    tokens = CollectorTokenService(engine, environment="testing", signing_secret="test", clock=lambda: NOW)
    identity = tokens.create_identity(
        "test", scopes=["ingest"], providers=["nba"], surfaces=["synergy_play_types"]
    )
    claims = tokens.validate(tokens.issue_for_secret(identity["identity_id"], identity["secret"]))
    ticks = iter(range(1, 10000))
    ingest = ObservationIngestionService(
        engine,
        publication_service=publications,
        clock=lambda: NOW + timedelta(seconds=next(ticks)),
    )

    def deliver(
        *,
        suffix="",
        categories=PLAY_TYPES,
        share=0.01,
        ambiguous=False,
        games=20,
        teammate=False,
        possessions=1,
        on=manifest,
    ):
        cutoff = on.cutoff.replace(tzinfo=NOW.tzinfo)
        receipts = []
        for category in categories:
            rows = [
                {
                    "PLAYER_ID": 2544,
                    "TEAM_ID": 1610612737,
                    "GP": games,
                    "POSS": possessions,
                    "POSS_PCT": share,
                    "PLAY_TYPE": category,
                }
            ]
            if teammate:
                rows.append(
                    {
                        "PLAYER_ID": 201939,
                        "TEAM_ID": 1610612737,
                        "GP": 7,
                        "POSS": 1,
                        "POSS_PCT": share,
                        "PLAY_TYPE": category,
                    }
                )
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
                cutoff=cutoff,
                scope={
                    "window": "season",
                    "subject": "player",
                    "phase": "Regular Season",
                    "play_type": category,
                },
            )
            raw = json.dumps(observation.payload, sort_keys=True, separators=(",", ":")).encode()
            envelope = {
                "client_observation_id": category + suffix,
                "observation_type": "synergy_play_types",
                "provider": "nba",
                "season": "2025-26",
                "cutoff": cutoff.isoformat(),
                "schema_version": 2,
                "retrieved_at": cutoff.isoformat(),
                "manifest_id": on.manifest_id,
                "scope": observation.scope,
                "environment": "testing",
                "checksum": hashlib.sha256(raw).hexdigest(),
            }
            receipts.append(ingest.ingest(claims, envelope, gzip.compress(raw), compressed=True))
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
            select(CompositionJob).where(CompositionJob.stream_key == "synergy_play_types")
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

    diets = PlayerDietRepository(engine, publication_reader=DatabaseFirstPublicationReader(engine))
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
    assert any(o.base == "play_types" and o.status == "available" for o in result.observations)
    from app.errors import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError):
        service.get_player_profile("Withheld Player", "Playtypes")


def _active_synergy_payload(engine):
    with Session(engine) as session:
        pointer = session.get(PublicationPointer, "synergy_play_types")
        return json.loads(session.get(PublicationVersion, pointer.active_publication_id).payload)


def test_synergy_recomposes_a_withheld_player_once_the_lagging_play_type_catches_up(plane):
    # The 2026-11-03 collection read one player's Isolation through 6 games
    # and every other play type through 7, so the player was withheld.  The
    # re-collection later that morning read Isolation through 7.
    engine, _, _, deliver = plane
    deliver(
        categories=[category for category in PLAY_TYPES if category != "Isolation"], games=7, teammate=True
    )
    deliver(categories=("Isolation",), games=6, teammate=True)
    assert _compose_queued_slice(engine) == 1
    payload = _active_synergy_payload(engine)
    assert payload["withheld_players"] == [{"player_id": 2544, "reason": "inconsistent team games played"}]
    assert {row["player_id"] for row in payload["rows"]} == {201939}

    deliver(suffix="-recollected", categories=("Isolation",), games=7, teammate=True)
    assert _compose_queued_slice(engine) == 1

    payload = _active_synergy_payload(engine)
    assert payload["withheld_players"] == []
    assert {(row["player_id"], row["slice_key"], row["games_played"]) for row in payload["rows"]} == {
        (player_id, category, 7) for player_id in (2544, 201939) for category in PLAY_TYPES
    }
    with Session(engine) as session:
        job = session.scalars(
            select(CompositionJob).where(CompositionJob.stream_key == "synergy_play_types")
        ).one()
        assert (job.status, job.attempts, job.generation) == ("succeeded", 0, 2)


def _compose_synergy(publications, manifest):
    version = publications.compose_from_observations(
        "synergy_play_types",
        season="2025-26",
        cutoff=NOW,
        manifest_id=manifest.manifest_id,
    )
    return version.publication_id, json.loads(version.payload)


def _rows_by_player(payload, player_id):
    return sorted(
        (row["slice_key"], row["volume"], row["games_played"])
        for row in payload["rows"]
        if row["player_id"] == player_id
    )


def _open_day(engine, days):
    """The manifest a later collection day runs under, with its own Event Catalog."""

    day = NOW + timedelta(days=days)
    control = CollectionControlService(engine, clock=lambda: day)
    request = control.create_bootstrap_request("2025-26", "event", cutoff=day)
    control.publish_catalog(
        request.request_id,
        {
            "complete_snapshot": True,
            "events": [
                {
                    "nba_game_id": f"game-{index}",
                    "home_team_id": team_ids[index * 2],
                    "away_team_id": team_ids[index * 2 + 1],
                    "scheduled_at": (NOW - timedelta(days=2, hours=index)).isoformat(),
                    "phase": "Regular Season",
                    "status": "Final",
                }
                for team_ids in [sorted(NBA_TEAM_IDS)]
                for index in range(15)
            ],
        },
        version=f"event-day-{days}",
    )
    return control.create_manifest(
        "2025-26",
        cutoff=day,
        scopes={"synergy_play_types", "canonical_game_ledger"},
        collect_before=day + timedelta(hours=1),
    )


def _compose_day(publications, manifest):
    version = publications.compose_from_observations(
        "synergy_play_types",
        season="2025-26",
        cutoff=manifest.cutoff.replace(tzinfo=NOW.tzinfo),
        manifest_id=manifest.manifest_id,
    )
    return version.publication_id, json.loads(version.payload)


def test_synergy_carries_a_player_torn_mid_refresh_from_the_last_version_that_had_him(plane):
    # NBA.com refreshes one play type at a time after a game night, so on
    # 2026-11-14 Brunson read 11 games in Isolation and 12 in the others.
    from app.services.player_diet import PlayerDietRepository

    engine, manifest, publications, deliver = plane
    deliver(games=6, teammate=True)
    first_id, _ = _compose_synergy(publications, manifest)

    deliver(
        suffix="-refresh",
        categories=[category for category in PLAY_TYPES if category != "Isolation"],
        games=7,
        teammate=True,
    )
    second_id, second = _compose_synergy(publications, manifest)
    assert second["withheld_players"] == []
    assert _rows_by_player(second, 2544) == [(category, 1, 6) for category in sorted(PLAY_TYPES)]
    assert second["carried_players"] == [
        {"player_id": 2544, "publication_id": first_id, "manifest_id": manifest.manifest_id}
    ]
    assert _rows_by_player(second, 201939) == [(category, 1, 7) for category in sorted(PLAY_TYPES)]

    # Still torn at the next composition: the rows keep naming where they came from.
    deliver(suffix="-again", categories=("Cut",), games=7, teammate=True)
    _, third = _compose_synergy(publications, manifest)
    assert third["carried_players"] == [
        {"player_id": 2544, "publication_id": first_id, "manifest_id": manifest.manifest_id}
    ]
    assert _rows_by_player(third, 2544) == [(category, 1, 6) for category in sorted(PLAY_TYPES)]

    diets = PlayerDietRepository(engine, publication_reader=DatabaseFirstPublicationReader(engine))
    served = diets.get_for_players("2025-26", [2544])
    assert sorted(
        (fact.slice_key, fact.games_played) for fact in served.players[2544] if fact.base == "play_types"
    ) == [(category, 6) for category in sorted(PLAY_TYPES)]
    with Session(engine) as session:
        assert session.get(PublicationVersion, second_id).status == "superseded"


def test_synergy_torn_player_no_earlier_version_had_stays_withheld(plane):
    _, manifest, publications, deliver = plane
    deliver(teammate=True, games=7)
    deliver(suffix="-torn", categories=("Isolation",), games=6, teammate=True)
    _, payload = _compose_synergy(publications, manifest)
    assert payload["withheld_players"] == [{"player_id": 2544, "reason": "inconsistent team games played"}]
    assert payload["carried_players"] == []
    assert _rows_by_player(payload, 2544) == []


def test_synergy_player_withheld_for_another_reason_is_not_carried(plane):
    _, manifest, publications, deliver = plane
    deliver(games=7, teammate=True)
    _compose_synergy(publications, manifest)
    deliver(suffix="-zero", games=7, possessions=0, teammate=True)
    _, payload = _compose_synergy(publications, manifest)
    assert payload["withheld_players"] == [
        {"player_id": 2544, "reason": "zero possessions contradict the rounded share"}
    ]
    assert payload["carried_players"] == []
    assert _rows_by_player(payload, 2544) == []


def _tamper_volume(version):
    version.payload = version.payload.replace('"volume":1', '"volume":99')


def _tamper_authority(version):
    version.event_catalog_checksum = "0" * 64


def _tamper_decodable(version):
    version.payload = version.payload.replace('"slice_key":"Isolation"', '"slice_key":"Bogus"')
    version.checksum = publication_payload_checksum(version.payload)


@pytest.mark.parametrize("tamper", [_tamper_volume, _tamper_authority, _tamper_decodable])
def test_synergy_skips_a_source_the_reader_would_refuse_and_carries_from_an_older_one(plane, tamper):
    engine, manifest, publications, deliver = plane
    deliver(games=5, teammate=True)
    intact_id, _ = _compose_synergy(publications, manifest)
    deliver(suffix="-next", games=6, teammate=True)
    refused_id, _ = _compose_synergy(publications, manifest)
    with Session(engine) as session, session.begin():
        tamper(session.get(PublicationVersion, refused_id))

    deliver(suffix="-torn", categories=("Isolation",), games=7, teammate=True)
    _, payload = _compose_synergy(publications, manifest)
    assert payload["carried_players"] == [
        {"player_id": 2544, "publication_id": intact_id, "manifest_id": manifest.manifest_id}
    ]
    assert _rows_by_player(payload, 2544) == [(category, 1, 5) for category in sorted(PLAY_TYPES)]


def test_synergy_never_carries_from_a_later_cutoff(plane):
    engine, manifest, publications, deliver = plane
    deliver(
        categories=[category for category in PLAY_TYPES if category != "Isolation"], games=6, teammate=True
    )
    deliver(suffix="-torn", categories=("Isolation",), games=7, teammate=True)
    tomorrow = _open_day(engine, 1)
    deliver(on=tomorrow, suffix="-tomorrow", games=8, teammate=True)
    _compose_day(publications, tomorrow)

    _, payload = _compose_synergy(publications, manifest)
    assert payload["withheld_players"] == [{"player_id": 2544, "reason": "inconsistent team games played"}]
    assert payload["carried_players"] == []


def test_synergy_never_carries_from_a_withdrawn_version(plane):
    engine, manifest, publications, deliver = plane
    deliver(games=6, teammate=True)
    _compose_synergy(publications, manifest)
    tomorrow = _open_day(engine, 1)
    deliver(on=tomorrow, suffix="-tomorrow", games=7, teammate=True)
    _compose_day(publications, tomorrow)
    restored = publications.rollback("synergy_play_types", reason="withdraw the bad day")

    deliver(on=tomorrow, suffix="-torn", categories=("Isolation",), games=8, teammate=True)
    _, payload = _compose_day(publications, tomorrow)
    assert payload["carried_players"] == [
        {"player_id": 2544, "publication_id": restored.publication_id, "manifest_id": manifest.manifest_id}
    ]
    assert _rows_by_player(payload, 2544) == [(category, 1, 6) for category in sorted(PLAY_TYPES)]


def test_synergy_carried_rows_keep_their_evidence_after_prune_and_gc(plane):
    engine, manifest, publications, deliver = plane
    deliver(games=6, teammate=True)
    origin_id, _ = _compose_synergy(publications, manifest)
    for day in (1, 2):
        later = _open_day(engine, day)
        deliver(
            on=later,
            suffix=f"-day{day}",
            categories=[category for category in PLAY_TYPES if category != "Isolation"],
            games=6 + day,
            teammate=True,
        )
        deliver(on=later, suffix=f"-day{day}-torn", categories=("Isolation",), games=7 + day, teammate=True)
        _, payload = _compose_day(publications, later)
        assert payload["carried_players"] == [
            {"player_id": 2544, "publication_id": origin_id, "manifest_id": manifest.manifest_id}
        ]

    publications.prune_history(stream_key="synergy_play_types")
    operations = CollectionOperationsService(engine, publication_service=publications, clock=lambda: NOW)
    assert operations.gc_observations(now=NOW + timedelta(days=90)) == 0
    with Session(engine) as session:
        assert session.get(PublicationVersion, origin_id) is None
        origin_evidence = session.scalars(
            select(CollectionObservation.observation_id).where(
                CollectionObservation.manifest_id == manifest.manifest_id
            )
        ).all()
    assert len(origin_evidence) == len(PLAY_TYPES)


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
        chosen = next(row for row in observations if json.loads(row.scope)["play_type"] == "Isolation")
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
            session.get(PublicationPointer, "synergy_play_types").active_publication_id == good.publication_id
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
            session.get(PublicationPointer, "synergy_play_types").active_publication_id == good.publication_id
        )
        assert session.get(PublicationVersion, good.publication_id).payload == good.payload


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
    definition = next(d for d in module.SURFACE_REGISTRY if d.stream_key == "synergy_play_types")
    assert definition.required == ("synergy_play_types",)
