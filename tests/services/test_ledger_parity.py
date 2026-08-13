"""Canonical ledger semantic-parity evidence contracts."""

from dataclasses import asdict
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy import select, text

from app.migrations import run_migrations
from app.models.collection_control import AuditEvent, PublicationVersion

from app.services.ledger_derivations import (
    derive_player_per36_facts,
)
from app.services.ledger_parity import (
    LedgerParityArtifactRepository,
    LegacyParityDiagnosticReader,
    compare_ledger_to_legacy,
    generate_semantic_difference_report,
)
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
    return tuple({
        "TEAM_ID": team_id,
        "OPP_PTS": 96.0,
        "OPP_REB": 36.0,
        "OPP_AST": 36.0,
    } for team_id in (game.home_team_id, game.away_team_id))


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

    approved = repository.adjudicate(
        artifact.artifact_id,
        decision="approved",
        actor="operator@example.com",
        reason="semantic differences reviewed",
    )
    with engine.connect() as connection:
        audit = connection.execute(select(AuditEvent).where(
            AuditEvent.resource == artifact.artifact_id,
        )).first()
    assert approved.decision == "approved"
    assert audit is not None


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
