"""Canonical ledger semantic-parity evidence contracts."""

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.migrations import run_migrations
from app.models.collection_control import (
    CatalogPublication,
    CollectionManifest,
    CollectionObservation,
    PublicationVersion,
)
from app.models.collection_control import AuditEvent

from app.services.ledger_derivations import (
    derive_player_per36_facts,
)
from app.services.ledger_parity import (
    LedgerParityArtifactRepository,
    LedgerParityReport,
    LegacyParityDiagnosticReader,
    PER36_DIAGNOSTIC_CAPTURE_STREAM,
    Per36DiagnosticCaptureRepository,
    SemanticDifference,
    compare_ledger_to_legacy,
    generate_semantic_difference_report,
    matchup_parity_artifact_is_activatable,
)
from app.services.ledger_lineage import LedgerLineage
from app.services.nba_stats_adapter import player_per36_request_descriptor
from tests.services.test_canonical_game_ledger import _game


def _legacy_player_rows(game):
    return tuple(
        {
            "GAME_ID": game.game_id,
            "PLAYER_ID": player.player_id,
            "PTS": player.points,
            "REB": player.rebounds,
            "AST": player.assists,
            "MIN": player.minutes,
        }
        for player in game.player_facts
    )


def _candidate(engine, *, stream_key="player_per36", checksum="a" * 64):
    publication_id = f"candidate-{stream_key}"
    with engine.begin() as connection:
        connection.execute(PublicationVersion.__table__.insert().values(
            publication_id=publication_id,
            stream_key=stream_key,
            season="2024-25",
            cutoff=datetime(2024, 11, 16, tzinfo=timezone.utc),
            version=1,
            status="candidate",
            checksum=checksum,
            payload="{}",
            created_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
            fence=0,
        ))
    return publication_id, checksum


def _traditional_season_rows(game):
    by_team = {fact.team_id: fact for fact in game.team_facts}
    return tuple({
        "TEAM_ID": team_fact.team_id,
        "OPP_PTS": float(by_team[team_fact.opponent_team_id].points) * 48.0 / team_fact.team_minutes,
        "OPP_REB": float(by_team[team_fact.opponent_team_id].rebounds) * 48.0 / team_fact.team_minutes,
        "OPP_AST": float(by_team[team_fact.opponent_team_id].assists) * 48.0 / team_fact.team_minutes,
    } for team_fact in game.team_facts)


def test_identity_set_differences_are_symmetric_and_prevent_exact_parity():
    game = _game()
    legacy = list(_legacy_player_rows(game))
    missing_from_legacy = legacy.pop()
    legacy.append({"GAME_ID": game.game_id, "PLAYER_ID": 999999, "PTS": 1})

    report = compare_ledger_to_legacy((game,), legacy, season=game.season)

    assert not report.exact
    assert report.adjudication_required
    assert {difference.classification for difference in report.differences} == {
        "missing_legacy_identity",
        "missing_ledger_identity",
    }
    assert any(str(missing_from_legacy["PLAYER_ID"]) in item.identity for item in report.differences)


def test_empty_comparison_cannot_claim_exact_parity():
    report = compare_ledger_to_legacy((), (), season="2024-25")

    assert not report.exact
    assert report.status == "adjudication_required"
    assert report.differences[0].classification == "empty_comparison"


def test_per36_well_formed_differences_persist_pending_and_are_unapprovable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'per36-differences.sqlite3'}")
    run_migrations(engine)
    publication_id, checksum = _candidate(engine)
    differences = (
        SemanticDifference(
            identity="per36:1", field="points", pbp_value=10,
            legacy_value=11, classification="raw_count_difference",
        ),
        SemanticDifference(
            identity="per36:2", field="player_id", pbp_value=2,
            legacy_value=None, classification="identity_mismatch",
        ),
        SemanticDifference(
            identity="per36:999", field="player_id", pbp_value=None,
            legacy_value=999, classification="identity_mismatch",
        ),
    )

    artifact = LedgerParityArtifactRepository(engine).record(
        "player_per36", cutoff=datetime(2024, 11, 16, tzinfo=timezone.utc),
        report=LedgerParityReport(
            season="2024-25", game_count=1, compared_count=1,
            differences=differences, adjudication_required=True,
        ),
        publication_id=publication_id, payload_checksum=checksum,
    )

    assert artifact.status == "pending_adjudication"
    assert artifact.decision is None
    assert len(json.loads(artifact.report)["differences"]) == 3
    with Session(engine) as session:
        assert not matchup_parity_artifact_is_activatable(
            artifact, stream_key="player_per36", session=session
        )


def test_traditional_opponent_and_per36_compare_derived_semantics():
    game = _game()
    traditional = _traditional_season_rows(game)
    per36 = tuple(asdict(fact) for fact in derive_player_per36_facts((game,), season=game.season))

    report = compare_ledger_to_legacy(
        (game,),
        _legacy_player_rows(game),
        season=game.season,
        legacy_traditional_rows=traditional,
        legacy_per36_rows=per36,
    )

    assert report.exact
    assert report.compared_count == len(game.player_facts) * 2 + len(game.team_facts)

    changed_traditional = list(traditional)
    changed_traditional[0] = {**changed_traditional[0], "OPP_PTS": 999}
    changed_per36 = list(per36)
    changed_per36[0] = {**changed_per36[0], "points_per36": 999.0}
    different = compare_ledger_to_legacy(
        (game,),
        _legacy_player_rows(game),
        season=game.season,
        legacy_traditional_rows=changed_traditional,
        legacy_per36_rows=changed_per36,
    )

    assert not different.exact
    assert {item.field for item in different.differences} == {
        "opponent_points",
        "points_per36",
    }

    changed_minutes = list(per36)
    changed_minutes[0] = {**changed_minutes[0], "minutes": 999.0}
    minutes_report = compare_ledger_to_legacy(
        (game,),
        _legacy_player_rows(game),
        season=game.season,
        legacy_per36_rows=changed_minutes,
    )
    assert any(item.field == "minutes" for item in minutes_report.differences)

    changed_game_count = list(per36)
    changed_game_count[0] = {**changed_game_count[0], "game_count": 1.0 + 0.5e-9}
    game_count_report = compare_ledger_to_legacy(
        (game,),
        _legacy_player_rows(game),
        season=game.season,
        legacy_per36_rows=changed_game_count,
    )
    assert any(item.field == "game_count" for item in game_count_report.differences)


def test_generated_report_persists_required_artifact(tmp_path):
    game = _game()
    engine = create_engine(f"sqlite:///{tmp_path / 'generated.sqlite3'}")
    run_migrations(engine)
    repository = LedgerParityArtifactRepository(engine)
    publication_id, checksum = _candidate(engine, stream_key="traditional_opponent")
    legacy = list(_legacy_player_rows(game))
    legacy[0] = {**legacy[0], "PTS": 999}

    report = generate_semantic_difference_report(
        (game,),
        legacy,
        season=game.season,
        artifact_repository=repository,
        stream_key="traditional_opponent",
        cutoff=datetime(2024, 11, 16, tzinfo=timezone.utc),
        publication_id=publication_id,
        payload_checksum=checksum,
    )

    artifact = repository.latest("traditional_opponent", game.season)
    assert report.adjudication_required
    assert artifact.status == "pending_adjudication"


def test_parity_artifact_is_required_durable_activation_evidence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'parity.sqlite3'}")
    run_migrations(engine)
    game = _game()
    report = compare_ledger_to_legacy(
        (game,), (), season=game.season, legacy_per36_rows=(),
    )
    repository = LedgerParityArtifactRepository(engine)
    publication_id, checksum = _candidate(engine)

    artifact = repository.record(
        "player_per36",
        cutoff=datetime(2024, 11, 16, tzinfo=timezone.utc),
        report=report,
        publication_id=publication_id,
        payload_checksum=checksum,
    )

    assert artifact.status == "pending_adjudication"
    assert repository.latest("player_per36", game.season).artifact_id == artifact.artifact_id

    with pytest.raises(ValueError, match="cannot be approved"):
        repository.adjudicate(
            artifact.artifact_id,
            decision="approved",
            actor="operator@example.com",
            reason="parent-approved: no identity parity",
        )


def test_per36_capture_is_scoped_immutable_and_rejects_stale_window(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'per36-capture.sqlite3'}")
    run_migrations(engine)
    game = _game()
    fact = derive_player_per36_facts((game,), season=game.season)[0]
    raw_fields = {
        field: sum(getattr(player, field) for player in game.player_facts if player.player_id == fact.player_id)
        for field in (
            "points", "rebounds", "assists", "field_goals_made",
            "field_goals_attempted", "three_pointers_made",
            "three_pointers_attempted", "free_throws_made",
            "free_throws_attempted", "turnovers", "steals", "blocks",
            "personal_fouls",
        )
    }
    row = {
        **raw_fields,
        **{
            field: getattr(fact, field)
            for field in fact.__dataclass_fields__
            if field.endswith("_per36")
        },
        "player_id": fact.player_id,
        "minutes": fact.minutes,
        "game_count": fact.game_count,
        "team_ids_at_game": list(fact.team_ids_at_game),
    }
    cutoff = datetime(2024, 11, 16, tzinfo=timezone.utc)
    publication_id, payload_checksum = _candidate(engine)
    request_identity = {
        "season": game.season,
        "window": "season",
        "cutoff": cutoff.isoformat(),
        "provider_start_date": "2024-10-22",
        "provider_end_date": "2024-11-16",
    }
    transport_request = player_per36_request_descriptor(season=game.season)
    request_checksum = hashlib.sha256(json.dumps(
        transport_request, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    provider_identity = {
        **request_identity,
        "transport_request": transport_request,
        "game_ids": [game.game_id],
        "request_checksum": request_checksum,
        "returned_row_count": 1,
        "returned_game_count": 1,
        "event_catalog_mapping_trace": {game.game_id: game.game_id},
    }
    observation_payload = json.dumps({
        "rows": [row],
        "provider_window_identity": provider_identity,
        "request_checksum": request_checksum,
    }, sort_keys=True, separators=(",", ":"))
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO collection_observations "
            "(observation_id, client_observation_id, collector_id, manifest_id, environment, provider, observation_type, scope, season, cutoff, schema_version, checksum, payload, payload_bytes, retrieved_at, accepted_at) "
            "VALUES ('per36-observation', 'client-per36', 'collector', 'manifest-1', 'test', 'nba', 'player_per36_diagnostic', '{}', :season, :cutoff, 1, :checksum, :payload, :payload_bytes, :cutoff, :cutoff)"
        ), {
            "season": game.season,
            "cutoff": cutoff,
            "checksum": hashlib.sha256(observation_payload.encode()).hexdigest(),
            "payload": observation_payload,
            "payload_bytes": len(observation_payload.encode()),
        })
    repository = Per36DiagnosticCaptureRepository(engine)
    capture = repository.record(
        capture_id="capture-per36",
        publication_id=publication_id,
        payload_checksum=payload_checksum,
        season=game.season,
        cutoff=cutoff,
        manifest_id="manifest-1",
        event_catalog_publication_id="catalog-1",
        event_catalog_checksum="b" * 64,
        game_set_checksum="c" * 64,
        request_checksum=request_checksum,
        provider_window_identity=provider_identity,
        rows=(row,),
        actor="operator@example.com",
        source_observation_id="per36-observation",
    )
    assert capture.capture_id == "capture-per36"
    assert repository.read(capture.capture_id).rows == (row,)
    parity_artifact = LedgerParityArtifactRepository(engine).record(
        "player_per36",
        cutoff=cutoff,
        report=LedgerParityReport(
            season=game.season, game_count=1, compared_count=1,
            differences=(), adjudication_required=False,
        ),
        publication_id=publication_id,
        payload_checksum=payload_checksum,
        lineage={
            "capture_id": capture.capture_id,
            "capture_checksum": capture.capture_checksum,
            "request_checksum": capture.request_checksum,
            "source_observation_id": capture.source_observation_id,
        },
    )
    with Session(engine) as session:
        assert matchup_parity_artifact_is_activatable(
            parity_artifact, stream_key="player_per36", session=session
        )
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT stream_key FROM canonical_game_ledger_parity_artifacts WHERE artifact_id = 'capture-per36'")
        ).scalar_one() == PER36_DIAGNOSTIC_CAPTURE_STREAM
    with pytest.raises(ValueError, match="source observation|window identity"):
        repository.record(
            publication_id=publication_id,
            payload_checksum="a" * 64,
            season=game.season,
            cutoff=cutoff,
            manifest_id="manifest-1",
            event_catalog_publication_id="catalog-1",
            event_catalog_checksum="b" * 64,
            game_set_checksum="c" * 64,
            request_checksum=request_checksum,
            provider_window_identity={
                "season": game.season,
                "window": "l15",
                "cutoff": cutoff.isoformat(),
                "game_ids": [game.game_id],
                "request_checksum": request_checksum,
            },
            rows=(row,),
            actor="operator@example.com",
            source_observation_id="per36-observation",
        )


def test_operator_per36_capture_flow_creates_observation_artifact_and_audit(
    tmp_path, monkeypatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'operator-capture.sqlite3'}")
    run_migrations(engine)
    game = _game()
    import app.services.ledger_runtime as ledger_runtime

    monkeypatch.setattr(
        ledger_runtime,
        "ActiveManifestLedgerGovernanceReader",
        lambda engine: SimpleNamespace(
            read_for_composition=lambda *args, **kwargs: SimpleNamespace(
                expected_game_ids=frozenset({game.game_id})
            )
        ),
    )
    fact = derive_player_per36_facts((game,), season=game.season)[0]
    raw_fields = (
        "points", "rebounds", "assists", "field_goals_made",
        "field_goals_attempted", "three_pointers_made",
        "three_pointers_attempted", "free_throws_made",
        "free_throws_attempted", "turnovers", "steals", "blocks",
        "personal_fouls",
    )
    row = {
        **{
            field: sum(
                getattr(player, field) for player in game.player_facts
                if player.player_id == fact.player_id
            )
            for field in raw_fields
        },
        **{
            field: getattr(fact, field)
            for field in fact.__dataclass_fields__ if field.endswith("_per36")
        },
        "player_id": fact.player_id, "minutes": fact.minutes,
        "game_count": fact.game_count,
        "team_ids_at_game": list(fact.team_ids_at_game),
    }
    cutoff = datetime(2024, 11, 16, tzinfo=timezone.utc)
    request_identity = {
        "season": game.season, "window": "season",
        "cutoff": cutoff.isoformat(), "provider_start_date": "2024-10-22",
        "provider_end_date": "2024-11-16",
    }
    transport_request = player_per36_request_descriptor(season=game.season)
    request_checksum = hashlib.sha256(json.dumps(
        transport_request, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    identity = {
        **request_identity,
        "transport_request": transport_request,
        "game_ids": [game.game_id], "request_checksum": request_checksum,
        "returned_row_count": 1, "returned_game_count": 1,
        "event_catalog_mapping_trace": {game.game_id: game.game_id},
    }
    catalog_payload = "{}"
    catalog_checksum = hashlib.sha256(catalog_payload.encode()).hexdigest()
    with engine.begin() as connection:
        connection.execute(CatalogPublication.__table__.insert().values(
            publication_id="catalog", season=game.season,
            catalog_type="event", cutoff=cutoff, version="v1",
            checksum=catalog_checksum, payload=catalog_payload, complete=True,
            published_at=cutoff,
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="manifest", season=game.season, cutoff=cutoff,
            collect_before=datetime(2024, 11, 17, tzinfo=timezone.utc),
            accepted_versions="[1]", scopes='["player_per36_diagnostic"]',
            checksum="manifest-checksum", event_catalog_publication_id="catalog",
            event_catalog_checksum=catalog_checksum, status="active",
            created_at=cutoff,
        ))
        connection.execute(PublicationVersion.__table__.insert().values(
            publication_id="candidate", stream_key="player_per36",
            season=game.season, cutoff=cutoff, version=1, status="candidate",
            checksum=hashlib.sha256(b"{}").hexdigest(), payload="{}",
            manifest_id="manifest",
            event_catalog_publication_id="catalog",
            event_catalog_checksum=catalog_checksum, created_at=cutoff, fence=0,
        ))

    capture = Per36DiagnosticCaptureRepository(engine).record_operator_evidence(
        publication_id="candidate", season=game.season, cutoff=cutoff,
        manifest_id="manifest", event_catalog_publication_id="catalog",
        event_catalog_checksum=catalog_checksum,
        game_set_checksum=LedgerLineage.for_game_ids([game.game_id]),
        request_checksum=request_checksum, provider_window_identity=identity,
        rows=(row,), actor="operator@example.com",
    )

    with Session(engine) as session:
        observation = session.get(CollectionObservation, capture.source_observation_id)
        audit = session.scalar(select(AuditEvent).where(
            AuditEvent.resource == capture.capture_id
        ))
    assert observation.observation_type == "player_per36_diagnostic"
    assert audit.action == "ledger.per36_capture_recorded"
    assert Per36DiagnosticCaptureRepository(engine).read(
        capture.capture_id
    ).capture_checksum == capture.capture_checksum

    for field, invalid_value in (
        ("endpoint", "LeagueDashTeamStats"),
        ("operation", "player_totals"),
    ):
        invalid_identity = json.loads(json.dumps(identity))
        invalid_identity["transport_request"][field] = invalid_value
        invalid_checksum = hashlib.sha256(json.dumps(
            invalid_identity["transport_request"],
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        invalid_identity["request_checksum"] = invalid_checksum
        with pytest.raises(ValueError, match="transport request"):
            Per36DiagnosticCaptureRepository(engine).record_operator_evidence(
                publication_id="candidate", season=game.season, cutoff=cutoff,
                manifest_id="manifest", event_catalog_publication_id="catalog",
                event_catalog_checksum=catalog_checksum,
                game_set_checksum=LedgerLineage.for_game_ids([game.game_id]),
                request_checksum=invalid_checksum,
                provider_window_identity=invalid_identity,
                rows=(row,), actor="operator@example.com",
            )

    invalid_identity = json.loads(json.dumps(identity))
    invalid_identity["transport_request"]["parameters"]["PerMode"] = "Totals"
    invalid_checksum = hashlib.sha256(json.dumps(
        invalid_identity["transport_request"],
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    invalid_identity["request_checksum"] = invalid_checksum
    with pytest.raises(ValueError, match="transport request"):
        Per36DiagnosticCaptureRepository(engine).record_operator_evidence(
            publication_id="candidate", season=game.season, cutoff=cutoff,
            manifest_id="manifest", event_catalog_publication_id="catalog",
            event_catalog_checksum=catalog_checksum,
            game_set_checksum=LedgerLineage.for_game_ids([game.game_id]),
            request_checksum=invalid_checksum,
            provider_window_identity=invalid_identity,
            rows=(row,), actor="operator@example.com",
        )

    for mutate in (
        lambda params: params.pop("Weight"),
        lambda params: params.update({"Unexpected": ""}),
        lambda params: params.update({"LastNGames": 0}),
    ):
        invalid_identity = json.loads(json.dumps(identity))
        mutate(invalid_identity["transport_request"]["parameters"])
        invalid_checksum = hashlib.sha256(json.dumps(
            invalid_identity["transport_request"],
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        invalid_identity["request_checksum"] = invalid_checksum
        with pytest.raises(ValueError, match="transport request"):
            Per36DiagnosticCaptureRepository(engine).record_operator_evidence(
                publication_id="candidate", season=game.season, cutoff=cutoff,
                manifest_id="manifest", event_catalog_publication_id="catalog",
                event_catalog_checksum=catalog_checksum,
                game_set_checksum=LedgerLineage.for_game_ids([game.game_id]),
                request_checksum=invalid_checksum,
                provider_window_identity=invalid_identity,
                rows=(row,), actor="operator@example.com",
            )
    with pytest.raises(ValueError, match="game set"):
        Per36DiagnosticCaptureRepository(engine).record_operator_evidence(
            publication_id="candidate", season=game.season, cutoff=cutoff,
            manifest_id="manifest", event_catalog_publication_id="catalog",
            event_catalog_checksum=catalog_checksum,
            game_set_checksum="c" * 64, request_checksum=request_checksum,
            provider_window_identity=identity, rows=(row,),
            actor="operator@example.com",
        )


@pytest.mark.parametrize(
    ("first", "second"),
    (("rejected", "approved"), ("approved", "rejected")),
)
def test_parity_adjudication_decision_is_immutable(tmp_path, first, second):
    engine = create_engine(f"sqlite:///{tmp_path / f'adjudication-{first}.sqlite3'}")
    run_migrations(engine)
    game = _game()
    publication_id, checksum = _candidate(engine, stream_key="player_game_logs")
    report = compare_ledger_to_legacy(
        (game,), _legacy_player_rows(game), season=game.season
    )
    repository = LedgerParityArtifactRepository(engine)
    artifact = repository.record(
        "player_game_logs",
        cutoff=datetime(2024, 11, 16, tzinfo=timezone.utc),
        report=report,
        publication_id=publication_id,
        payload_checksum=checksum,
    )
    repository.adjudicate(
        artifact.artifact_id,
        decision=first,
        actor="operator@example.com",
        reason="first immutable operator decision",
    )

    with pytest.raises(ValueError, match="decision is immutable"):
        repository.adjudicate(
            artifact.artifact_id,
            decision=second,
            actor="other@example.com",
            reason="second stale operator decision",
        )

    assert repository.latest("player_game_logs", game.season).decision == first


def test_traditional_parity_reads_general_opponent_stats_semantics(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite3'}")
    game = _game()
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE general_opponent_stats ("
            "TEAM_ID INTEGER, OPP_PTS REAL, OPP_REB REAL, OPP_AST REAL)"
        ))
        for row in _traditional_season_rows(game):
            connection.execute(text(
                "INSERT INTO general_opponent_stats VALUES (:TEAM_ID, :OPP_PTS, :OPP_REB, :OPP_AST)"
            ), row)

    report = compare_ledger_to_legacy(
        (game,), None, season=game.season,
        legacy_traditional_rows=LegacyParityDiagnosticReader(engine).read(
            "traditional_opponent"
        ),
    )

    assert report.exact


def test_traditional_season_parity_reports_missing_team_and_semantic_difference():
    game = _game()
    rows = list(_traditional_season_rows(game))
    rows.pop()
    rows[0] = {**rows[0], "OPP_PTS": 101.0}

    report = compare_ledger_to_legacy(
        (game,), None, season=game.season, legacy_traditional_rows=rows,
    )

    assert not report.exact
    assert {item.classification for item in report.differences} == {
        "missing_legacy_identity", "semantic_difference",
    }
