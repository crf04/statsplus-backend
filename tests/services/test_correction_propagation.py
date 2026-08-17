"""Correction propagation contracts for the canonical ledger seam (#116)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select

from app.migrations import run_migrations
from app.models.collection_control import (
    CollectionManifest,
    CollectionObservation,
    CompositionJob,
    PublicationPointer,
    PublicationStream,
    PublicationVersion,
    ReconciliationItem,
)
from app.services.collection_control import PublicationService
from app.services.collection_control import LedgerPublicationComposition
from app.services.canonical_game_ledger import (
    CanonicalGameLedgerRepository,
    raw_rows_from_facts,
)
from app.services.ledger_materialization import (
    LedgerCorrectionQueue,
    LedgerMaterializationService,
    LedgerMaterializationUnavailable,
)
from app.services.ledger_matchup_materialization import LedgerMatchupMaterializationService
from app.services.ledger_parity import LedgerParityArtifactRepository
from app.services.team_matchup_repository import TeamMatchupRepository, TeamMatchupSnapshotScope
from tests.services.test_ledger_derivations import _league_games


UTC = timezone.utc
AS_OF = datetime(2025, 10, 15, tzinfo=UTC)


def _engine(tmp_path, name: str = "correction.sqlite3"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    run_migrations(engine)
    return engine


def test_correction_jobs_record_target_lineage_and_replay_is_idempotent(tmp_path):
    engine = _engine(tmp_path)
    repository = CanonicalGameLedgerRepository(
        engine,
        correction_sink=LedgerCorrectionQueue(clock=lambda: AS_OF),
    )
    game = _league_games()[0]
    repository.replace_game(game)
    corrected = replace(
        game,
        team_facts=(replace(game.team_facts[0], points=game.team_facts[0].points + 1), game.team_facts[1]),
    )
    corrected = replace(corrected, raw_rows=raw_rows_from_facts(corrected)).with_checksum()

    repository.replace_game(corrected)
    with engine.connect() as connection:
        first_jobs = connection.execute(select(CompositionJob.__table__)).mappings().all()
    assert len(first_jobs) == len(LedgerCorrectionQueue.STREAMS)
    assert {
        tuple(json.loads(row["affected_team_ids"])) for row in first_jobs
    } == {
        tuple(sorted(fact.team_id for fact in corrected.team_facts))
    }
    assert {row["recomposition_reason"] for row in first_jobs} == {"correction"}
    assert {row["ledger_checksum"] for row in first_jobs} == {corrected.checksum}
    assert all(json.loads(row["source_observation_ids"]) == [corrected.source_observation_id] for row in first_jobs)
    repository.replace_game(corrected)
    with engine.connect() as connection:
        replay_jobs = connection.execute(select(CompositionJob.__table__)).mappings().all()
    assert len(replay_jobs) == len(first_jobs)
    assert {row["job_id"] for row in replay_jobs} == {row["job_id"] for row in first_jobs}


def test_coalesced_correction_union_keeps_all_trigger_and_source_lineage(tmp_path):
    engine = _engine(tmp_path, "coalesced.sqlite3")
    queue = LedgerCorrectionQueue(clock=lambda: AS_OF)
    repository = CanonicalGameLedgerRepository(engine, correction_sink=queue)
    first, second = _league_games()[:2]
    repository.replace_game(first)
    repository.replace_game(second)
    corrected = replace(first, team_facts=tuple(
        replace(fact, points=fact.points + 1) for fact in first.team_facts
    ))
    corrected = replace(corrected, raw_rows=raw_rows_from_facts(corrected)).with_checksum()
    repository.replace_game(corrected)
    with engine.connect() as connection:
        row = connection.execute(select(CompositionJob.__table__)).mappings().first()
    assert set(json.loads(row["trigger_game_ids"])) == {first.game_id, second.game_id}
    assert set(json.loads(row["source_observation_ids"])) == {
        first.source_observation_id, second.source_observation_id,
    }
    assert row["recomposition_reason"] == "correction"
    assert row["game_set_checksum"]

def test_matchup_lineage_persists_cutoff_reason_and_exact_game_set(tmp_path):
    engine = _engine(tmp_path, "matchup.sqlite3")
    games = _league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    matchup = TeamMatchupRepository(engine)
    service = LedgerMatchupMaterializationService(
        ledger,
        matchup,
        clock=lambda: AS_OF + timedelta(hours=18),
    )
    expected = frozenset(game.game_id for game in games)
    expected_l15 = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }
    service.materialize(
        "2025-26",
        as_of=AS_OF.date(),
        cutoff=AS_OF,
        recomposition_reason="correction",
        expected_game_ids=expected,
        expected_l15_game_ids=expected_l15,
        team_ids=frozenset(range(1, 31)),
    )

    season = matchup.get_snapshot(TeamMatchupSnapshotScope("2025-26", AS_OF.date()))
    assert season.observations
    assert all(observation.cutoff == AS_OF for observation in season.observations)
    assert all(observation.recomposition_reason == "correction" for observation in season.observations)
    assert all(observation.source_observation_ids for observation in season.observations)
    assert all(observation.game_set_checksum for observation in season.observations)
    assert all(fact.cutoff == AS_OF for fact in season.facts)
    assert all(fact.recomposition_reason == "correction" for fact in season.facts)
    assert all(fact.source_observation_ids for fact in season.facts)
    assert all(fact.game_set_checksum for fact in season.facts)


def test_targeted_recomposition_retains_unaffected_team_facts(tmp_path):
    engine = _engine(tmp_path, "targeted.sqlite3")
    games = _league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    matchup = TeamMatchupRepository(engine)
    service = LedgerMatchupMaterializationService(
        ledger,
        matchup,
        clock=lambda: AS_OF + timedelta(hours=18),
    )
    expected = frozenset(game.game_id for game in games)
    expected_l15 = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }
    service.materialize(
        "2025-26",
        as_of=AS_OF.date(),
        expected_game_ids=expected,
        expected_l15_game_ids=expected_l15,
        team_ids=frozenset(range(1, 31)),
    )
    scope = TeamMatchupSnapshotScope("2025-26", AS_OF.date(), 15)
    before = matchup.get_snapshot(scope)
    corrected_game = replace(
        games[0],
        team_facts=(replace(games[0].team_facts[0], points=games[0].team_facts[0].points + 2), games[0].team_facts[1]),
    )
    corrected_game = replace(corrected_game, raw_rows=raw_rows_from_facts(corrected_game)).with_checksum()
    ledger.replace_game(corrected_game)
    affected = frozenset(fact.team_id for fact in corrected_game.team_facts)
    service.materialize(
        "2025-26",
        as_of=AS_OF.date(),
        cutoff=AS_OF,
        recomposition_reason="correction",
        affected_team_ids=affected,
        expected_game_ids=expected,
        expected_l15_game_ids=expected_l15,
        team_ids=frozenset(range(1, 31)),
    )
    after = matchup.get_snapshot(scope)
    before_unaffected = {
        (fact.team_id, fact.base, fact.slice_key, fact.stat_key): fact
        for fact in before.facts if fact.team_id not in affected
    }
    after_unaffected = {
        (fact.team_id, fact.base, fact.slice_key, fact.stat_key): fact
        for fact in after.facts if fact.team_id not in affected
    }
    assert before_unaffected == after_unaffected
    assert any(
        fact.team_id in affected and fact.recomposition_reason == "correction"
        for fact in after.facts
    )


def test_correction_changes_published_counts_and_rank(tmp_path):
    """Acceptance correction changes the persisted derived metric payload."""
    engine = _engine(tmp_path, "counts.sqlite3")
    games = _league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    matchup = TeamMatchupRepository(engine)
    service = LedgerMatchupMaterializationService(ledger, matchup, clock=lambda: AS_OF + timedelta(hours=18))
    expected = frozenset(game.game_id for game in games)
    expected_l15 = {team_id: frozenset(game.game_id for game in games
                                      if team_id in {game.home_team_id, game.away_team_id})
                    for team_id in range(1, 31)}
    service.materialize("2025-26", as_of=AS_OF.date(), expected_game_ids=expected,
                        expected_l15_game_ids=expected_l15,
                        cutoff=AS_OF + timedelta(hours=18), team_ids=frozenset(range(1, 31)))
    before = matchup.get_snapshot(TeamMatchupSnapshotScope("2025-26", AS_OF.date()))
    corrected = replace(games[0], team_facts=(replace(games[0].team_facts[0], points=99), games[0].team_facts[1]))
    corrected = replace(corrected, raw_rows=raw_rows_from_facts(corrected)).with_checksum()
    ledger.replace_game(corrected)
    service.materialize("2025-26", as_of=AS_OF.date(), cutoff=AS_OF + timedelta(hours=18), recomposition_reason="correction",
                        expected_game_ids=expected, expected_l15_game_ids=expected_l15,
                        team_ids=frozenset(range(1, 31)))
    after = matchup.get_snapshot(TeamMatchupSnapshotScope("2025-26", AS_OF.date()))
    assert before.facts != after.facts
    assert any(fact.recomposition_reason == "correction" for fact in after.facts)


def test_recomposition_failure_after_first_staged_stream_rolls_back_batch(tmp_path, monkeypatch):
    engine = _engine(tmp_path, "atomic-runtime.sqlite3")
    publications = PublicationService(engine, clock=lambda: AS_OF)
    publications.register_default_streams()
    streams = ("traditional_opponent_season", "player_game_logs")
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="manifest", season="2025-26", cutoff=AS_OF,
            collect_before=AS_OF + timedelta(days=1), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum="manifest",
            status="active", created_at=AS_OF,
        ))
        connection.execute(CollectionObservation.__table__.insert().values(
            observation_id="obs:one", client_observation_id="obs:one", collector_id="test",
            manifest_id="manifest", environment="testing", provider="pbp",
            observation_type="canonical_game_ledger",
            scope=json.dumps({"surface": "canonical_game_ledger", "game_id": "game-one"}),
            season="2025-26", cutoff=AS_OF, schema_version=1, checksum="a" * 64,
            payload="{}", payload_bytes=2, retrieved_at=AS_OF, accepted_at=AS_OF,
        ))
        connection.execute(PublicationStream.__table__.update().where(
            PublicationStream.stream_key.in_(streams),
        ).values(enabled=True))
        connection.execute(CompositionJob.__table__.insert(), [
            {"job_id": f"job-{index}", "stream_key": stream, "manifest_id": "manifest",
             "season": "2025-26", "cutoff": AS_OF, "status": "queued", "attempts": 0,
             "created_at": AS_OF, "updated_at": AS_OF}
            for index, stream in enumerate(streams)
        ])
    provenance = {"obs:one": "game-one"}
    import app.services.collection_control as collection_control
    monkeypatch.setattr(collection_control, "_write_publication_projection", lambda *args: None)
    def payload_for(stream, value):
        return [{"value": value}]
    for stream in streams:
        publications.recompose_ledger(stream, season="2025-26", cutoff=AS_OF,
                                       payload=payload_for(stream, "last-good"), provenance=provenance)
    with engine.connect() as connection:
        before = {
            row["stream_key"]: row["active_publication_id"]
            for row in connection.execute(select(PublicationPointer.__table__)).mappings()
            if row["stream_key"] in streams
        }

    class Governance:
        def read_for_composition(self, season, cutoff, manifest_id=None):
            return type("Governance", (), {
                "expected_game_ids": frozenset(), "expected_l15_game_ids": {},
                "team_ids": frozenset(),
            })()

    class Materialization:
        publication_service = publications

        def compose(self, games, **kwargs):
            compositions = [LedgerPublicationComposition(
                stream_key=stream, season="2025-26", cutoff=AS_OF,
                payload=payload_for(stream, "new"), provenance=provenance,
            ) for stream in streams]
            return publications.recompose_ledger_batch(compositions)

    repository = CanonicalGameLedgerRepository(engine)
    runtime = __import__("app.services.ledger_runtime", fromlist=["LedgerRuntime"]).LedgerRuntime(
        backfill=None, repository=repository, materialization=Materialization(),
        governance=Governance(), clock=lambda: AS_OF,
    )
    original = publications._compose_active_in_session
    calls = {"count": 0}

    def fail_after_first(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise LedgerMaterializationUnavailable("injected staged-stream failure")
        return original(*args, **kwargs)

    publications._compose_active_in_session = fail_after_first
    assert runtime.compose_queued("2025-26") == 0
    assert calls["count"] == 2
    with engine.connect() as connection:
        after = {
            row["stream_key"]: row["active_publication_id"]
            for row in connection.execute(select(PublicationPointer.__table__)).mappings()
            if row["stream_key"] in streams
        }
        versions = connection.execute(select(PublicationVersion.__table__)).all()
        jobs = connection.execute(select(CompositionJob.__table__)).mappings().all()
    assert after == before
    assert len(versions) == 2
    assert all(job["status"] == "failed" for job in jobs)
    assert all(job["last_error"] == "recomposition_failed" for job in jobs)


def test_recomposition_failure_records_retryable_state_without_reconciliation_loss(tmp_path):
    engine = _engine(tmp_path, "failure.sqlite3")
    with engine.begin() as connection:
        connection.execute(CompositionJob.__table__.insert().values(
            job_id="job-1", stream_key="traditional_opponent_season", manifest_id=None,
            season="2025-26", cutoff=AS_OF, status="queued", attempts=0,
            created_at=AS_OF, updated_at=AS_OF,
        ))
    repository = CanonicalGameLedgerRepository(engine)

    class Governance:
        def read_for_composition(self, season, cutoff, manifest_id=None):
            raise LedgerMaterializationUnavailable("provider boundary unavailable")

    class Materialization:
        publication_service = None

    from app.services.ledger_runtime import LedgerRuntime

    runtime = LedgerRuntime(
        backfill=None,
        repository=repository,
        materialization=Materialization(),
        governance=Governance(),
        clock=lambda: AS_OF + timedelta(hours=18),
    )
    assert runtime.compose_queued("2025-26") == 0
    with engine.connect() as connection:
        job = connection.execute(select(CompositionJob.__table__)).mappings().one()
        reconciliation = connection.execute(select(ReconciliationItem.__table__)).mappings().one()
    assert job["status"] == "failed"
    assert job["attempts"] == 1
    assert job["last_error"] == "recomposition_failed"
    assert reconciliation["status"] == "open"


def test_scheduled_reconciliation_requeues_failed_ledger_job(tmp_path):
    engine = _engine(tmp_path, "reconcile.sqlite3")
    publications = PublicationService(engine, clock=lambda: AS_OF)
    publications.register_default_streams()
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="manifest", season="2025-26", cutoff=AS_OF,
            collect_before=AS_OF + timedelta(days=1), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum="manifest",
            status="active", created_at=AS_OF,
        ))
        connection.execute(CollectionObservation.__table__.insert().values(
            observation_id="obs:game-1", client_observation_id="obs:game-1",
            collector_id="test", manifest_id="manifest", environment="testing",
            provider="pbp", observation_type="canonical_game_ledger",
            scope=json.dumps({"surface": "canonical_game_ledger", "game_id": "game-1"}),
            season="2025-26", cutoff=AS_OF, schema_version=1,
            checksum="a" * 64, payload="{}", payload_bytes=2,
            retrieved_at=AS_OF, accepted_at=AS_OF,
        ))
        connection.execute(PublicationStream.__table__.update().where(
            PublicationStream.stream_key == "traditional_opponent_season",
        ).values(enabled=True))
        connection.execute(CompositionJob.__table__.insert().values(
            job_id="retry-job", stream_key="traditional_opponent_season",
            manifest_id="manifest", season="2025-26", cutoff=AS_OF,
            status="failed", attempts=1, created_at=AS_OF, updated_at=AS_OF,
            last_error="recomposition_failed",
        ))

    assert publications.reconcile_pending(
        season="2025-26", cutoff=AS_OF,
    ) == 1
    with engine.connect() as connection:
        job = connection.execute(select(CompositionJob.__table__)).mappings().one()
    assert job["status"] == "queued"
    assert job["attempts"] == 2
    assert job["last_error"] is None


def test_active_ledger_publication_advances_once_and_replay_keeps_pointer(tmp_path):
    engine = _engine(tmp_path, "publication.sqlite3")
    games = _league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    publications = PublicationService(engine, clock=lambda: AS_OF + timedelta(hours=18))
    publications.register_default_streams()
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="manifest", season="2025-26", cutoff=AS_OF,
            collect_before=AS_OF + timedelta(days=30), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum="manifest",
            status="active", created_at=AS_OF,
        ))
        connection.execute(CollectionObservation.__table__.insert(), [
            {
                "observation_id": game.source_observation_id,
                "client_observation_id": game.source_observation_id,
                "collector_id": "test",
                "manifest_id": "manifest",
                "environment": "testing",
                "provider": "pbp",
                "observation_type": "canonical_game_ledger",
                "scope": json.dumps({"surface": "canonical_game_ledger", "game_id": game.game_id}),
                "season": game.season,
                "cutoff": AS_OF,
                "schema_version": 1,
                "checksum": game.checksum,
                "payload": "{}",
                "payload_bytes": 2,
                "retrieved_at": AS_OF,
                "accepted_at": AS_OF,
            }
            for game in games
        ])
        connection.execute(PublicationStream.__table__.update().where(
            PublicationStream.stream_key == "traditional_opponent_season",
        ).values(enabled=True))

    materialization = LedgerMaterializationService(
        ledger,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=type("Parity", (), {"read": lambda self, key: ()})(),
        publication_service=publications,
    )
    expected = frozenset(game.game_id for game in games)
    expected_l15 = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }
    materialization.compose(
        games,
        season="2025-26",
        as_of=AS_OF.date(),
        cutoff=AS_OF,
        expected_game_ids=expected,
        expected_l15_game_ids=expected_l15,
        team_ids=frozenset(range(1, 31)),
        activate=True,
        recomposition_reason="correction",
    )
    first = publications.current("traditional_opponent_season")
    assert first is not None
    with engine.connect() as connection:
        first_pointer = connection.execute(select(PublicationPointer.__table__).where(
            PublicationPointer.stream_key == "traditional_opponent_season",
        )).mappings().one()
        first_count = connection.execute(select(PublicationVersion.__table__).where(
            PublicationVersion.stream_key == "traditional_opponent_season",
        )).all()

    materialization.compose(
        games,
        season="2025-26",
        as_of=AS_OF.date(),
        cutoff=AS_OF,
        expected_game_ids=expected,
        expected_l15_game_ids=expected_l15,
        team_ids=frozenset(range(1, 31)),
        activate=True,
        recomposition_reason="correction",
    )
    second = publications.current("traditional_opponent_season")
    assert second is not None
    assert second.publication_id == first.publication_id
    with engine.connect() as connection:
        second_pointer = connection.execute(select(PublicationPointer.__table__).where(
            PublicationPointer.stream_key == "traditional_opponent_season",
        )).mappings().one()
        second_count = connection.execute(select(PublicationVersion.__table__).where(
            PublicationVersion.stream_key == "traditional_opponent_season",
        )).all()
    assert second_pointer.fence == first_pointer.fence
    assert len(second_count) == len(first_count)
