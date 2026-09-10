"""The governed player Zone Shooting path, from collector evidence to the tab.

``exact_shot_zones`` is the only player Diet stream with a second consumer: the
five canonical zone slices feed the Diet, and an auxiliary profile section on
the same publication feeds the Player Profile's "Zone Shooting" category.  The
tests here exercise both, because activating the stream cuts both over at once.
"""

from datetime import timedelta
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.collector.contracts import canonical_json
from app.collector.normalizers import SHOT_ZONES, normalize_zone_response
from app.domain.player_shot_zone_taxonomy import (
    PLAYER_SHOT_ZONE_PROFILE_CATEGORIES,
)
from app.errors import ResourceNotFoundError
from app.migrations import run_migrations
from app.models.collection_control import (
    CollectionObservation,
    CompositionJob,
    PublicationPointer,
    PublicationStream,
    PublicationVersion,
)
from app.services.collection_control import (
    CollectionControlService,
    CollectionOperationsService,
    CollectorTokenService,
    ControlPlaneError,
    NBA_TEAM_IDS,
    ObservationIngestionService,
    PublicationService,
)
from app.services.database_first_activation import (
    DatabaseFirstPublicationReader,
    PublicationPayloadError,
    decode_player_diet,
    decode_player_shot_zones,
)
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from tests.test_residential_collector import NOW, _compose_queued_slice

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "player_diets"
     / "player_shot_zones_league.json").read_text(encoding="utf-8")
)
SEASON = "2025-26"
#: LeBron James in the recorded fixture; the row every assertion below reads.
SAMPLE_PLAYER_ID = 2544
SAMPLE_PLAYER_NAME = "LeBron James"


def _result_set(kind):
    from copy import deepcopy
    return {"resultSets": deepcopy(FIXTURE[kind])}


def _fixture_player_ids():
    return [row[0] for row in FIXTURE["totals"]["rowSet"]]


def _zone_observation(**overrides):
    arguments = {
        "response": _result_set("per_game"),
        "totals_response": _result_set("totals"),
        "games_response": _result_set("games"),
    }
    arguments.update(overrides)
    response = arguments.pop("response")
    return normalize_zone_response(
        response, season=SEASON, cutoff=NOW,
        scope={"window": "season", "subject": "player", "phase": "Regular Season"},
        **arguments,
    )


@pytest.fixture
def plane(tmp_path):
    """A control plane authorized to ingest the player shot-zone surface."""

    engine = create_engine(f"sqlite:///{tmp_path / 'zones.sqlite3'}")
    run_migrations(engine)
    control = CollectionControlService(engine, clock=lambda: NOW)
    control.activate_season(SEASON, actor="operator")
    team_ids = sorted(NBA_TEAM_IDS)
    event_request = control.create_bootstrap_request(SEASON, "event", cutoff=NOW)
    control.publish_catalog(event_request.request_id, {
        "complete_snapshot": True,
        "events": [{
            "nba_game_id": f"game-{index}",
            "home_team_id": team_ids[index * 2],
            "away_team_id": team_ids[index * 2 + 1],
            "phase": "Regular Season", "status": "Final",
            "scheduled_at": (NOW - timedelta(days=2, hours=index)).isoformat(),
        } for index in range(15)],
    }, version="event-v1")
    athlete_request = control.create_bootstrap_request(SEASON, "athlete", cutoff=NOW)
    control.publish_catalog(athlete_request.request_id, {
        "complete_snapshot": True,
        "identities": [{
            "player_id": str(player_id), "team_id": team_ids[0], "status": "active",
            "event_ids": [f"game-{index}" for index in range(15)],
        } for player_id in _fixture_player_ids()],
    }, version="athlete-v1")
    manifest = control.create_manifest(
        SEASON, cutoff=NOW,
        scopes={"exact_shot_zones", "canonical_game_ledger"},
        collect_before=NOW + timedelta(hours=1),
    )
    publications = PublicationService(
        engine, clock=lambda: NOW,
        l15_expectation_resolver=ActiveManifestLedgerGovernanceReader(engine),
    )
    # The production registration, verbatim from the surface registry.  Nothing
    # here restates what the stream requires, so a registry naming an
    # observation type the real normalizer never emits fails here exactly as it
    # would fail production.
    publications.register_default_streams()
    operations = CollectionOperationsService(
        engine, publication_service=publications, clock=lambda: NOW,
    )
    operations.activate_stream(
        "exact_shot_zones", actor="operator", reason="enable for first collection",
    )
    tokens = CollectorTokenService(
        engine, environment="testing", signing_secret="test", clock=lambda: NOW,
    )
    identity = tokens.create_identity(
        "collector", scopes=["ingest"], owner="residential_collector",
        providers=["nba"], surfaces=["exact_shot_zones"],
    )
    claims = tokens.validate(tokens.issue_for_secret(
        identity["identity_id"], identity["secret"], scopes=["ingest"]
    ))
    ticks = iter(range(1, 10_000))
    ingestion = ObservationIngestionService(
        engine, publication_service=publications,
        clock=lambda: NOW + timedelta(seconds=next(ticks)),
    )

    def deliver(observation=None, *, suffix=""):
        observation = observation or _zone_observation()
        # The collector's own canonical encoder, not a convenient equivalent.
        # It is the wire form the ingestion service re-derives and stores, and
        # the two must agree byte for byte or composition refuses the
        # observation on its checksum.
        raw = canonical_json(observation.payload)
        return ingestion.ingest(claims, {
            "client_observation_id": f"zones{suffix}",
            "observation_type": "exact_shot_zones",
            "provider": "nba", "season": SEASON,
            "cutoff": NOW.isoformat(), "schema_version": 2,
            "retrieved_at": NOW.isoformat(),
            "manifest_id": manifest.manifest_id,
            "scope": observation.scope, "environment": "testing",
            "checksum": hashlib.sha256(raw).hexdigest(),
        }, gzip.compress(raw), compressed=True)

    return engine, manifest, publications, deliver


def _deactivate(engine, stream_key="exact_shot_zones"):
    """Return the stream to its preactivation state, legacy-authoritative."""

    with Session(engine) as session, session.begin():
        session.get(PublicationStream, stream_key).enabled = False


def _active_payload(engine, stream_key="exact_shot_zones"):
    with Session(engine) as session:
        pointer = session.get(PublicationPointer, stream_key)
        version = session.get(PublicationVersion, pointer.active_publication_id)
        return json.loads(version.payload), version


def test_zone_observations_compose_through_the_queue_into_a_two_reader_publication(plane):
    engine, manifest, _, deliver = plane
    deliver()

    with Session(engine) as session:
        jobs = session.scalars(select(CompositionJob).where(
            CompositionJob.stream_key == "exact_shot_zones",
        )).all()
    assert [job.status for job in jobs] == ["queued"]
    assert _compose_queued_slice(engine) == 1

    payload, version = _active_payload(engine)
    assert version.status == "active"
    assert version.manifest_id == manifest.manifest_id
    assert payload["base"] == "shot_zones"

    # The Diet half: five canonical slices per player, season Totals volumes,
    # and an explicit games-played denominator.
    assert {row["slice_key"] for row in payload["rows"]} == set(SHOT_ZONES)
    assert {row["volume_unit"] for row in payload["rows"]} == {"field_goal_attempts"}
    facts = decode_player_diet(payload, base="shot_zones", retrieved_at=NOW)
    restricted = next(
        fact for fact in facts
        if fact.player_id == SAMPLE_PLAYER_ID and fact.slice_key == "Restricted Area"
    )
    assert restricted.volume == 340.0
    assert restricted.games_played == 60

    # The profile half: the wider provider vocabulary, on the same publication.
    assert set(payload["profile"]["categories"]) == set(
        PLAYER_SHOT_ZONE_PROFILE_CATEGORIES
    )
    rows = decode_player_shot_zones(payload)
    lebron = next(row for row in rows if row.player_id == SAMPLE_PLAYER_ID)
    assert lebron.player_name == SAMPLE_PLAYER_NAME
    assert lebron.categories["Restricted Area"]["FGA"] == 5.7
    assert lebron.categories["Backcourt"]["FGA"] == 0.0


def test_zone_publication_retains_every_source_row_for_the_league_reference(plane):
    """A row with no Diet facts still counts toward the profile's league mean."""

    engine, _, _, deliver = plane
    observation = _zone_observation()
    # Zero every published zone for one player, exactly as the provider reports
    # someone who took no field goals in any of them.
    dropped = observation.payload["records"][0]["player_id"]
    observation.payload["records"] = [
        row for row in observation.payload["records"]
        if row["player_id"] != dropped
    ]
    deliver(observation)
    assert _compose_queued_slice(engine) == 1

    payload, _ = _active_payload(engine)
    assert dropped not in {row["player_id"] for row in payload["rows"]}
    assert dropped in {row["player_id"] for row in payload["profile"]["rows"]}


def test_a_zone_candidate_missing_its_profile_section_does_not_publish(plane):
    engine, manifest, publications, deliver = plane
    observation = _zone_observation()
    del observation.payload["profile"]
    deliver(observation)
    with pytest.raises(ControlPlaneError, match="publication_candidate_invalid"):
        publications.compose_from_observations(
            "exact_shot_zones", season=SEASON, cutoff=NOW,
            manifest_id=manifest.manifest_id,
        )


@pytest.mark.parametrize("defect", [
    "shot_type_slices",
    "unstated_value_mode",
    "per_game_diet_volumes",
    "makes_above_attempts",
    "share_above_one",
    "missing_slice",
    "invented_games_played",
    "profile_taxonomy",
    "profile_identity",
    "profile_partial_triplet",
    "profile_row_value_mode",
    "per_player_missing_slice",
    "inconsistent_games_played",
    "restated_share",
    "rounded_per_game_counts",
    "checksum",
    "manifest",
])
def test_a_bad_zone_candidate_keeps_the_last_good_publication(plane, defect):
    engine, manifest, publications, deliver = plane
    deliver()
    good = publications.compose_from_observations(
        "exact_shot_zones", season=SEASON, cutoff=NOW,
        manifest_id=manifest.manifest_id,
    )

    observation = _zone_observation()
    payload = observation.payload
    if defect == "shot_type_slices":
        for row in payload["records"]:
            row["slice_key"] = row["category"] = "Catch and Shoot"
    elif defect == "unstated_value_mode":
        # The shape the old scalar collector would have produced: no statement
        # of which per-mode each half was read in.
        del payload["coverage"]["profile_value_mode"]
    elif defect == "per_game_diet_volumes":
        payload["coverage"]["diet_value_mode"] = "PerGame"
    elif defect == "makes_above_attempts":
        payload["records"][0]["makes"] = payload["records"][0]["attempts"] + 1
    elif defect == "share_above_one":
        payload["records"][0]["share"] = 1.5
    elif defect == "missing_slice":
        payload["records"] = [
            row for row in payload["records"]
            if row["slice_key"] != "Mid-Range"
        ]
    elif defect == "invented_games_played":
        payload["records"][0]["games_played"] = 0
    elif defect == "profile_taxonomy":
        for row in payload["profile"]["rows"]:
            del row["Backcourt_FGA"]
        payload["profile"]["categories"] = [
            category for category in payload["profile"]["categories"]
            if category != "Backcourt"
        ]
    elif defect == "profile_identity":
        payload["profile"]["rows"][0]["player_name"] = ""
    elif defect == "profile_partial_triplet":
        # One key left present and null while its two siblings are dropped.
        # All three then read as ``None`` and the category is otherwise
        # indistinguishable from one the provider never reported.
        row = payload["profile"]["rows"][0]
        row["Mid-Range_FGM"] = None
        del row["Mid-Range_FGA"]
        del row["Mid-Range_FG_PCT"]
    elif defect == "profile_row_value_mode":
        # The container still says ``PerGame``; the retained row says the
        # season counts it actually holds.
        payload["profile"]["rows"][0]["value_mode"] = "Totals"
    elif defect == "per_player_missing_slice":
        # One player loses a slice.  Every canonical category is still
        # present somewhere in the league, so the union check cannot see it.
        victim = payload["records"][0]["player_id"]
        payload["records"] = [
            row for row in payload["records"]
            if not (row["player_id"] == victim and row["slice_key"] == "Mid-Range")
        ]
    elif defect == "inconsistent_games_played":
        # One player's slices disagree about how many games they describe.
        payload["records"][0]["games_played"] += 1
    elif defect == "restated_share":
        # In range, and not this player's portion of their own attempts.
        first, second = payload["records"][0], payload["records"][1]
        first["share"], second["share"] = second["share"], first["share"]
    elif defect == "rounded_per_game_counts":
        # Season Totals counts are whole; a fraction is a rounded PerGame
        # reading relabelled, which understates every published volume.
        payload["records"][0]["attempts"] += 0.5

    with pytest.raises(ControlPlaneError):
        # Some defects are refused at ingestion and some only at composition.
        # Either way the bad evidence must never reach a pointer, so the two
        # boundaries are asserted as one refusal.
        if defect in {"checksum", "manifest"}:
            deliver(suffix="-retry")
            with Session(engine) as session, session.begin():
                rows = session.scalars(select(CollectionObservation).where(
                    CollectionObservation.observation_type == "exact_shot_zones",
                )).all()
                if defect == "checksum":
                    latest = max(rows, key=lambda item: item.accepted_at)
                    latest.payload = latest.payload.replace(
                        '"share":', '"share_":', 1,
                    )
                else:
                    # Every observation loses its manifest binding, so nothing
                    # this manifest authorized remains.
                    for row in rows:
                        row.manifest_id = None
        else:
            deliver(observation, suffix="-retry")
        publications.compose_from_observations(
            "exact_shot_zones", season=SEASON, cutoff=NOW,
            manifest_id=manifest.manifest_id,
        )
    with Session(engine) as session:
        pointer = session.get(PublicationPointer, "exact_shot_zones")
        assert pointer.active_publication_id == good.publication_id


def test_opponent_and_target_zone_taxonomy_is_not_widened():
    """The profile's extra categories are evidence, not shared vocabulary."""

    from app.domain.player_diet_taxonomy import PLAYER_DIET_BASE_SLICES
    from app.domain.team_matchup_taxonomy import SHOT_ZONE_SLICES

    assert PLAYER_DIET_BASE_SLICES["shot_zones"] == SHOT_ZONE_SLICES
    assert set(SHOT_ZONE_SLICES) == set(SHOT_ZONES)
    assert set(SHOT_ZONE_SLICES) < set(PLAYER_SHOT_ZONE_PROFILE_CATEGORIES)
    assert "Left Corner 3" not in SHOT_ZONE_SLICES


# --------------------------------------------------------------------------
# The strict profile decoder
# --------------------------------------------------------------------------


def _decodable_payload(plane_engine=None):
    observation = _zone_observation()
    return {
        "base": "shot_zones",
        "rows": [],
        "profile": {
            "value_mode": "PerGame",
            "categories": list(PLAYER_SHOT_ZONE_PROFILE_CATEGORIES),
            "metrics": ["FGM", "FGA", "FG_PCT"],
            "rows": [
                {
                    "player_id": row["player_id"],
                    "player_name": row["player_name"],
                    "team_id": row["team_id"],
                    "team_abbreviation": row["team_abbreviation"],
                    "age": row["age"],
                    "nickname": row["nickname"],
                    "categories": {
                        category: {
                            metric: row[f"{category}_{metric}"]
                            for metric in ("FGM", "FGA", "FG_PCT")
                        }
                        for category in PLAYER_SHOT_ZONE_PROFILE_CATEGORIES
                    },
                }
                for row in observation.payload["profile"]["rows"]
            ],
        },
    }


def test_the_profile_decoder_accepts_an_unreported_category_as_unreported():
    rows = decode_player_shot_zones(_decodable_payload())
    colby = next(row for row in rows if row.player_name == "Colby Jones")
    assert colby.categories["Left Corner 3"]["FGA"] is None
    assert colby.categories["Right Corner 3"]["FGA"] is not None


@pytest.mark.parametrize("mutate,match", [
    (lambda p: p["profile"].__setitem__("value_mode", "Totals"), "value mode"),
    (lambda p: p["profile"].__setitem__("categories", ["Restricted Area"]), "taxonomy"),
    (lambda p: p["profile"].__setitem__("rows", []), "empty"),
    (lambda p: p["profile"]["rows"][0].__setitem__("player_id", 0), "out of range"),
    (lambda p: p["profile"]["rows"][0].__setitem__("player_name", ""), "non-empty"),
    (lambda p: p["profile"]["rows"][0]["categories"].pop("Backcourt"), "taxonomy"),
    (lambda p: p["profile"]["rows"][0]["categories"]["Mid-Range"].__setitem__("FGM", 99), "FGM exceeds FGA"),
    (lambda p: p["profile"]["rows"][0]["categories"]["Mid-Range"].__setitem__("FG_PCT", 2), "out of range"),
    (lambda p: p["profile"]["rows"][0]["categories"]["Mid-Range"].__setitem__("FGA", None), "missing beside"),
    (lambda p: p["profile"]["rows"][0]["categories"]["Mid-Range"].__setitem__("FGA", "12"), "not a number"),
    (lambda p: p["profile"]["rows"][0]["categories"]["Mid-Range"].__setitem__("FGA", True), "not a number"),
    (lambda p: p["profile"]["rows"][0]["categories"]["Mid-Range"].__setitem__("FGM", -1.0), "out of range"),
    (lambda p: p["profile"]["rows"][0]["categories"]["Mid-Range"].__setitem__("FGA", float("inf")), "out of range"),
    (lambda p: p.__setitem__("profile", {}), "non-empty object"),
    (lambda p: p.__setitem__("base", "shot_types"), "base mismatch"),
    (lambda p: p["profile"]["rows"].append(p["profile"]["rows"][0]), "repeats a player"),
])
def test_the_profile_decoder_refuses_a_malformed_publication(mutate, match):
    payload = _decodable_payload()
    mutate(payload)
    with pytest.raises(PublicationPayloadError, match=match):
        decode_player_shot_zones(payload)


# --------------------------------------------------------------------------
# The Zone Shooting profile reader
# --------------------------------------------------------------------------


def _legacy_zone_frame():
    """The legacy table, built by the untouched legacy transform."""

    from app.services.player_zone_profile import transform_player_zone_profile
    from nba_api.stats.endpoints._base import Endpoint

    result_set = FIXTURE["per_game"]
    return transform_player_zone_profile(Endpoint.DataSet(
        {"headers": result_set["headers"], "data": result_set["rowSet"]}
    ).get_data_frame())


def _player_service(engine, *, publication_reader):
    from app.services.player_service import PlayerProfileReader, PlayerService
    from tests.services.test_player_service import _settings

    class Catalog:
        @staticmethod
        def get_catalog(season, *, active_only=False):
            return []

    class Diets:
        @staticmethod
        def get_for_players(season, player_ids):
            from app.services.player_diet import PlayerDietResult
            return PlayerDietResult(season=season, players={}, observations=())

    return PlayerService(
        engine, profile_reader=PlayerProfileReader(Catalog(), Diets()),
        settings=_settings(), publication_reader=publication_reader,
    )


@pytest.fixture
def published(plane):
    engine, _, _, deliver = plane
    deliver()
    assert _compose_queued_slice(engine) == 1
    # The legacy table the cutover replaces, written from the same evidence.
    _legacy_zone_frame().to_sql(
        "player_shooting_zones", engine, if_exists="replace", index=False,
    )
    # ``player_information`` is what the historical fuzzy name lookup reads.
    pd.DataFrame({"full_name": [
        row[1] for row in FIXTURE["totals"]["rowSet"]
    ]}).to_sql("player_information", engine, if_exists="replace", index=False)
    return engine


def test_zone_shooting_is_served_from_the_publication_and_matches_the_legacy_table(published):
    engine = published
    service = _player_service(
        engine, publication_reader=DatabaseFirstPublicationReader(engine),
    )
    published_row = service.get_player_profile(SAMPLE_PLAYER_NAME, "Zone Shooting")
    legacy = _legacy_zone_frame()
    legacy_row = legacy[
        legacy["PLAYER_NAME"] == SAMPLE_PLAYER_NAME
    ].to_dict(orient="records")[0]

    assert len(legacy_row) == 43
    # Exact equality, not approximate: this is a source cutover, so the same
    # provider evidence must render the same published numbers.
    assert published_row == legacy_row


def test_zone_shooting_profile_reads_warm_decodes_a_hit_never_retires_the_payload(published):
    """A warmed Diet decode cache never starves the Zone Shooting profile.

    The Target backtest and the Zone Shooting profile read the same
    ``exact_shot_zones`` publication, so one shared reader serves both.  A
    backtest or Lab edit stores the stream's decoded facts in the reader's
    decode cache; the profile, however, reads the rendered payload, so a
    decode served from that cache must never come back as a read whose
    ``payload`` is ``None`` -- before the fix, the second profile call through
    a warmed reader decoded ``None`` and the tab lost its row.
    """

    engine = published
    reader = DatabaseFirstPublicationReader(engine)
    # Simulate the Diet consumer: a read that stores this row's decode in the
    # shared reader's cache before the profile requests arrive.
    reader.read("exact_shot_zones", season=SEASON)

    service = _player_service(engine, publication_reader=reader)
    first = service.get_player_profile(SAMPLE_PLAYER_NAME, "Zone Shooting")
    second = service.get_player_profile(SAMPLE_PLAYER_NAME, "Zone Shooting")
    legacy = _legacy_zone_frame()
    legacy_row = legacy[
        legacy["PLAYER_NAME"] == SAMPLE_PLAYER_NAME
    ].to_dict(orient="records")[0]

    assert first == legacy_row
    assert second == legacy_row


def test_the_published_profile_keeps_the_source_row_order(published):
    """The league ``PTS%`` reference is a floating-point sum over this frame.

    Reordering the population changes that sum in its last digit, and every
    ``PTS%+`` column on the tab divides by it, so the publication carries the
    evidence's order rather than an identifier order.
    """

    payload, _ = _active_payload(published)
    assert [
        row["player_id"] for row in payload["profile"]["rows"]
    ] == _fixture_player_ids()
    assert _fixture_player_ids() != sorted(_fixture_player_ids())


def test_zone_shooting_reads_the_legacy_table_while_the_stream_is_inactive(published):
    engine = published
    service = _player_service(
        engine, publication_reader=DatabaseFirstPublicationReader(engine),
    )
    _deactivate(engine)
    # Make the two sources distinguishable, then prove the inactive stream
    # leaves the legacy table authoritative.
    marked = _legacy_zone_frame()
    marked.loc[
        marked["PLAYER_NAME"] == SAMPLE_PLAYER_NAME, "Restricted Area_FGM"
    ] = 99.0
    marked.to_sql("player_shooting_zones", engine, if_exists="replace", index=False)

    row = service.get_player_profile(SAMPLE_PLAYER_NAME, "Zone Shooting")
    assert row["Restricted Area_FGM"] == 99.0


def test_zone_shooting_keeps_its_historical_fuzzy_lookup_and_404(published):
    engine = published
    service = _player_service(
        engine, publication_reader=DatabaseFirstPublicationReader(engine),
    )
    # A near-miss still resolves through ``player_information`` and the profile
    # is located by the exact matched name.
    assert service.get_player_profile("lebron james", "Zone Shooting")[
        "PLAYER_NAME"
    ] == SAMPLE_PLAYER_NAME
    with pytest.raises(ResourceNotFoundError):
        service.get_player_profile("Nobody At All", "Zone Shooting")


def test_a_malformed_active_zone_publication_is_a_404_not_a_legacy_read(published):
    engine = published
    with Session(engine) as session, session.begin():
        pointer = session.get(PublicationPointer, "exact_shot_zones")
        version = session.get(PublicationVersion, pointer.active_publication_id)
        payload = json.loads(version.payload)
        payload["profile"]["value_mode"] = "Totals"
        version.payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    service = _player_service(
        engine, publication_reader=DatabaseFirstPublicationReader(engine),
    )
    with pytest.raises(ResourceNotFoundError):
        service.get_player_profile(SAMPLE_PLAYER_NAME, "Zone Shooting")


def test_the_registry_reconciles_an_already_registered_stream_without_migration(tmp_path):
    """The stream row exists in production with the wrong required name."""

    engine = create_engine(f"sqlite:///{tmp_path / 'registry.sqlite3'}")
    run_migrations(engine)
    publications = PublicationService(engine, clock=lambda: NOW)
    publications.register_default_streams()
    with Session(engine) as session, session.begin():
        session.get(PublicationStream, "exact_shot_zones").required_observations = (
            json.dumps(["shot_zones"])
        )

    publications.register_default_streams()

    with Session(engine) as session:
        stream = session.get(PublicationStream, "exact_shot_zones")
        assert json.loads(stream.required_observations) == ["exact_shot_zones"]


def test_a_player_with_no_attempts_stays_in_the_profile_and_out_of_the_diet(plane):
    """The legitimate asymmetry the per-player partition check must not break.

    A player who attempted no field goal in any published zone has no Diet
    share to state, so the collector omits them from the five slices rather
    than publishing five zero shares.  Their profile row is still retained:
    the league ``PTS%`` reference is a mean over every source row, so dropping
    them would move every other player's ``PTS%+``.
    """

    from tests.test_residential_collector import _flat_zone_records

    totals = _flat_zone_records("totals")
    benched = totals[0]["PLAYER_ID"]
    for zone in SHOT_ZONES:
        for metric in ("FGM", "FGA", "FG_PCT"):
            totals[0][f"{zone}_{metric}"] = 0.0

    engine, manifest, publications, deliver = plane
    deliver(_zone_observation(totals_response=totals))
    assert _compose_queued_slice(engine) == 1

    payload, _ = _active_payload(engine)
    assert benched not in {row["player_id"] for row in payload["rows"]}
    assert benched in {row["player_id"] for row in payload["profile"]["rows"]}
    assert len(payload["profile"]["rows"]) == len(_fixture_player_ids())
    # Every player who *is* represented still carries the full partition.
    represented = {row["player_id"] for row in payload["rows"]}
    assert len(represented) == len(_fixture_player_ids()) - 1
    for player_id in represented:
        assert {
            row["slice_key"] for row in payload["rows"]
            if row["player_id"] == player_id
        } == set(SHOT_ZONES)


def test_a_non_ascii_player_name_survives_the_wire_to_the_profile(plane):
    """The league this stream publishes is not ASCII.

    The player Zone Shooting profile is the first player surface to publish
    ``player_name`` for the whole league, and the recorded capture contains
    names like ``Bogdan Bogdanović``.  The platform stores observations in
    ``canonical_publication_json`` form, which does not escape non-ASCII, and
    composition re-derives the checksum from those stored bytes -- so a
    collector that escaped them would have every real collection refused on
    integrity while every ASCII-only fixture passed.
    """

    from tests.test_residential_collector import _flat_zone_records

    name = "Bogdan Bogdanović"
    responses = {
        kind: _flat_zone_records(kind) if kind != "games"
        else json.loads(json.dumps(_result_set("games")))
        for kind in ("per_game", "totals")
    }
    for records in responses.values():
        records[0]["PLAYER_NAME"] = name
    engine, _, _, deliver = plane
    deliver(_zone_observation(
        response=responses["per_game"], totals_response=responses["totals"],
    ))
    assert _compose_queued_slice(engine) == 1

    payload, _ = _active_payload(engine)
    assert name in {row["player_name"] for row in payload["profile"]["rows"]}
    assert decode_player_shot_zones(payload)[0].player_name == name
