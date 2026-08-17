"""Correction propagation contracts for the canonical ledger seam (#116)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, update

from app.migrations import run_migrations
from app.models.collection_control import (
    ActiveSeason,
    CollectionManifest,
    CollectionObservation,
    CompositionJob,
    PublicationPointer,
    PublicationStream,
    PublicationVersion,
    ReconciliationItem,
)
from app.models.event_catalog import EventCatalogEntry
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
from app.services.ledger_parity import LedgerParityArtifactRepository, LegacyParityDiagnosticReader
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader, LedgerRuntime
from app.services.team_matchup_repository import TeamMatchupRepository, TeamMatchupSnapshotScope
from app.services.team_matchup_query import TeamMatchupQueryService
from tests.services.test_ledger_derivations import _league_games


UTC = timezone.utc
AS_OF = datetime(2025, 10, 15, tzinfo=UTC)


def _engine(tmp_path, name: str = "correction.sqlite3"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    run_migrations(engine)
    return engine


def _boundary_observation_values(game, *, cutoff, manifest_id):
    facts_by_team = {fact.team_id: fact for fact in game.team_facts}

    def diagnostic(team_id):
        fact = facts_by_team[team_id]
        return {
            "TeamId": team_id,
            "Points": fact.points,
            "FG2M": fact.two_pointers_made,
            "FG2A": fact.two_pointers_attempted,
            "FG3M": fact.three_pointers_made,
            "FG3A": fact.three_pointers_attempted,
            "FtPoints": fact.free_throws_made,
            "FTA": fact.free_throws_attempted,
            "OffRebounds": fact.offensive_rebounds,
            "DefRebounds": fact.defensive_rebounds,
            "Rebounds": fact.rebounds,
            "Assists": fact.assists,
            "Turnovers": fact.turnovers,
            "Steals": fact.steals,
            "Blocks": fact.blocks,
            "Fouls": fact.personal_fouls,
        }

    payload_document = {
        "stats": {
            side: {
                "FullGame": [
                    dict(row.payload)
                    for row in game.raw_rows
                    if row.side == side
                ],
            }
            for side in ("Home", "Away")
        },
        "team_results": {
            "Home": {"FullGame": diagnostic(game.home_team_id)},
            "Away": {"FullGame": diagnostic(game.away_team_id)},
        },
        "home_team_abbreviation": game.home_team_tricode,
        "away_team_abbreviation": game.away_team_tricode,
        "date": game.game_date.isoformat(),
        "participant_ids_by_team": {
            str(team_id): [
                player.player_id
                for player in game.player_facts
                if player.team_id == team_id
            ]
            for team_id in (game.home_team_id, game.away_team_id)
        },
    }
    payload = json.dumps(
        payload_document, sort_keys=True, separators=(",", ":")
    )
    return {
        "observation_id": game.source_observation_id,
        "client_observation_id": game.source_observation_id,
        "collector_id": "railway-ledger",
        "manifest_id": manifest_id,
        "environment": "server",
        "provider": "pbp",
        "observation_type": "canonical_game_ledger",
        "scope": json.dumps(
            {"game_id": game.game_id, "surface": "canonical_game_ledger"},
            sort_keys=True,
        ),
        "season": game.season,
        "cutoff": cutoff,
        "schema_version": 1,
        "checksum": hashlib.sha256(payload.encode()).hexdigest(),
        "payload": payload,
        "payload_bytes": len(payload.encode()),
        "retrieved_at": game.retrieved_at,
        "accepted_at": game.retrieved_at,
    }


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
    """A governed correction changes the active publication and public read."""
    engine = _engine(tmp_path, "counts.sqlite3")
    cutoff = AS_OF
    runtime_now = AS_OF + timedelta(hours=18)
    games = _league_games()

    def raw_document(game):
        facts_by_team = {fact.team_id: fact for fact in game.team_facts}

        def diagnostic(team_id):
            fact = facts_by_team[team_id]
            return {
                "TeamId": team_id,
                "Points": fact.points,
                "FG2M": fact.two_pointers_made,
                "FG2A": fact.two_pointers_attempted,
                "FG3M": fact.three_pointers_made,
                "FG3A": fact.three_pointers_attempted,
                "FtPoints": fact.free_throws_made,
                "FTA": fact.free_throws_attempted,
                "OffRebounds": fact.offensive_rebounds,
                "DefRebounds": fact.defensive_rebounds,
                "Rebounds": fact.rebounds,
                "Assists": fact.assists,
                "Turnovers": fact.turnovers,
                "Steals": fact.steals,
                "Blocks": fact.blocks,
                "Fouls": fact.personal_fouls,
            }

        return {
            "stats": {
                side: {
                    "FullGame": [
                        dict(row.payload)
                        for row in game.raw_rows
                        if row.side == side
                    ],
                }
                for side in ("Home", "Away")
            },
            "team_results": {
                "Home": {"FullGame": diagnostic(game.home_team_id)},
                "Away": {"FullGame": diagnostic(game.away_team_id)},
            },
            "home_team_abbreviation": game.home_team_tricode,
            "away_team_abbreviation": game.away_team_tricode,
            "date": game.game_date.isoformat(),
            "participant_ids_by_team": {
                str(team_id): [player.player_id for player in game.player_facts
                               if player.team_id == team_id]
                for team_id in (game.home_team_id, game.away_team_id)
            },
        }

    def accepted_observation(game):
        payload = json.dumps(
            raw_document(game), sort_keys=True, separators=(",", ":")
        )
        return {
            "observation_id": game.source_observation_id,
            "client_observation_id": game.source_observation_id,
            "collector_id": "railway-ledger",
            "manifest_id": "ledger-manifest",
            "environment": "server",
            "provider": "pbp",
            "observation_type": "canonical_game_ledger",
            "scope": json.dumps(
                {"game_id": game.game_id, "surface": "canonical_game_ledger"},
                sort_keys=True,
            ),
            "season": game.season,
            "cutoff": cutoff,
            "schema_version": 1,
            "checksum": hashlib.sha256(payload.encode()).hexdigest(),
            "payload": payload,
            "payload_bytes": len(payload.encode()),
            "retrieved_at": game.retrieved_at,
            "accepted_at": game.retrieved_at,
        }

    with engine.begin() as connection:
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=cutoff, activated_at=cutoff, activated_by="test",
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="ledger-manifest", season="2025-26", cutoff=cutoff,
            collect_before=cutoff + timedelta(days=30), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum="ledger-manifest",
            status="active", created_at=cutoff,
        ))
        connection.execute(EventCatalogEntry.__table__.insert(), [{
            "nba_game_id": game.game_id,
            "season": game.season,
            "home_team_id": game.home_team_id,
            "home_team_name": f"Team {game.home_team_id}",
            "home_team_tricode": game.home_team_tricode,
            "away_team_id": game.away_team_id,
            "away_team_name": f"Team {game.away_team_id}",
            "away_team_tricode": game.away_team_tricode,
            "scheduled_at": datetime.combine(game.game_date, datetime.min.time(), UTC),
            "status_text": "Final",
            "status_code": 3,
            "classification": "Regular Season",
            "first_seen_at": cutoff,
            "last_seen_at": cutoff,
        } for game in games])

    publications = PublicationService(engine, clock=lambda: runtime_now)
    publications.register_default_streams()
    for stream_key, windows in (
        ("traditional_opponent_season", ("season",)),
        ("traditional_opponent_l15", ("l15",)),
    ):
        publications.register_stream(
            stream_key,
            provider="ledger",
            owner="railway",
            required_observations=("canonical_game_ledger",),
            publication_strategy="ledger_compose",
            supported_windows=windows,
            enabled=True,
            completeness_rule="league_complete",
            freshness_rule="cutoff_current",
        )

    queue = LedgerCorrectionQueue(
        clock=lambda: runtime_now,
        require_governance=True,
    )
    ledger = CanonicalGameLedgerRepository(engine, correction_sink=queue)
    accepted = {
        game.source_observation_id: accepted_observation(game)
        for game in games
    }
    ledger.replace_games_atomic(games, accepted_observations=accepted)

    matchup = TeamMatchupRepository(engine)
    matchup_materialization = LedgerMatchupMaterializationService(
        ledger, matchup, clock=lambda: runtime_now
    )

    materialization = LedgerMaterializationService(
        ledger,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=LegacyParityDiagnosticReader(engine),
        publication_service=publications,
        clock=lambda: runtime_now,
    )
    runtime = LedgerRuntime(
        backfill=None,
        repository=ledger,
        materialization=materialization,
        governance=ActiveManifestLedgerGovernanceReader(
            engine, clock=lambda: runtime_now
        ),
        matchup_materialization=matchup_materialization,
        publication_service=publications,
        clock=lambda: runtime_now,
    )

    assert runtime.compose_queued("2025-26") == len(LedgerCorrectionQueue.STREAMS)
    query = TeamMatchupQueryService(matchup, clock=lambda: runtime_now)
    scope = TeamMatchupSnapshotScope("2025-26", cutoff.date())
    before_window = query.get_window(scope)
    before_metric = next(
        metric
        for metric in before_window.team_metrics[1]
        if metric.base == "traditional" and metric.stat_key == "OPP_REB"
    )
    before_snapshot = matchup.get_snapshot(scope)
    before_fact = next(
        fact
        for fact in before_snapshot.facts
        if fact.team_id == 1 and fact.base == "traditional" and fact.stat_key == "OPP_REB"
    )
    before_publication = publications.current("traditional_opponent_season")
    assert before_publication is not None
    assert before_fact.raw_value == 75
    assert before_metric.allowed_per_48 == 5.0
    assert before_metric.rank == 1

    original = games[0]
    corrected_team_facts = tuple(
        replace(
            fact,
            defensive_rebounds=fact.defensive_rebounds + 10,
            rebounds=fact.rebounds + 10,
        )
        if fact.team_id == original.away_team_id
        else fact
        for fact in original.team_facts
    )
    corrected = replace(
        original,
        team_facts=corrected_team_facts,
        source_observation_id=f"obs:correction:{original.game_id}",
        retrieved_at=cutoff + timedelta(hours=1),
    )
    corrected = replace(
        corrected, raw_rows=raw_rows_from_facts(corrected)
    ).with_checksum()
    result = ledger.replace_games_atomic(
        (corrected,),
        accepted_observations={
            corrected.source_observation_id: accepted_observation(corrected),
        },
    )
    assert result[0].replaced
    assert result[0].checksum == corrected.checksum

    assert runtime.compose_queued("2025-26") == len(LedgerCorrectionQueue.STREAMS)
    after_window = query.get_window(scope)
    after_metric = next(
        metric
        for metric in after_window.team_metrics[1]
        if metric.base == "traditional" and metric.stat_key == "OPP_REB"
    )
    after_snapshot = matchup.get_snapshot(scope)
    after_fact = next(
        fact
        for fact in after_snapshot.facts
        if fact.team_id == 1 and fact.base == "traditional" and fact.stat_key == "OPP_REB"
    )
    after_publication = publications.current("traditional_opponent_season")
    stored = ledger.get_game(original.game_id)
    assert after_publication is not None
    assert after_publication.publication_id != before_publication.publication_id
    assert after_publication.status == "active"
    assert after_publication.reason == "correction"
    assert stored is not None
    assert stored.source_observation_id == corrected.source_observation_id
    assert stored.checksum == corrected.checksum
    assert stored.raw_checksum == corrected.raw_checksum
    assert after_fact.raw_value == 85
    assert after_fact.ledger_checksum != before_fact.ledger_checksum
    assert corrected.source_observation_id in after_fact.source_observation_ids
    assert after_metric.allowed_per_48 == pytest.approx(17 / 3)
    assert after_metric.rank == 30
    assert after_metric.allowed_per_48 != before_metric.allowed_per_48
    assert after_metric.rank != before_metric.rank
    assert all(
        observation.recomposition_reason == "correction"
        and corrected.source_observation_id in observation.source_observation_ids
        and observation.ledger_checksum == after_fact.ledger_checksum
        for observation in after_snapshot.observations
    )
    with engine.connect() as connection:
        correction_observation = connection.execute(
            select(CollectionObservation).where(
                CollectionObservation.observation_id == corrected.source_observation_id,
            )
        ).mappings().one()
        jobs = connection.execute(select(CompositionJob)).mappings().all()
    assert correction_observation["checksum"] == hashlib.sha256(
        correction_observation["payload"].encode()
    ).hexdigest()
    assert len(jobs) == len(LedgerCorrectionQueue.STREAMS)
    assert all(job["status"] == "succeeded" for job in jobs)
    assert all(
        corrected.source_observation_id in json.loads(job["source_observation_ids"])
        for job in jobs
    )


def test_correction_changes_exact_l15_boundary(tmp_path):
    """A newly completed governed game rolls the exact L15 boundary forward."""
    engine = _engine(tmp_path, "boundary.sqlite3")
    manifest_id = "boundary-manifest"
    cutoff = AS_OF + timedelta(days=1, hours=5, minutes=22)
    runtime_now = cutoff + timedelta(hours=18)
    games = _league_games()
    base_game = games[0]
    boundary_game_id = "boundary-game-16"

    def boundary_game(*, source_observation_id, retrieved_at, rebounds):
        home_fact = replace(
            base_game.team_facts[0],
            opponent_team_id=2,
            opponent_team_tricode="T02",
        )
        away_fact = replace(
            base_game.team_facts[1],
            team_id=2,
            team_tricode="T02",
            opponent_team_id=1,
            opponent_team_tricode="T01",
            defensive_rebounds=rebounds - 1,
            rebounds=rebounds,
        )
        away_player = replace(
            base_game.player_facts[1],
            team_id=2,
            team_tricode="T02",
        )
        candidate = replace(
            base_game,
            game_id=boundary_game_id,
            game_date=base_game.game_date + timedelta(days=15),
            away_team_id=2,
            away_team_tricode="T02",
            team_facts=(home_fact, away_fact),
            player_facts=(base_game.player_facts[0], away_player),
            source_observation_id=source_observation_id,
            retrieved_at=retrieved_at,
            participant_ids_by_team=((1, (1001,)), (2, (1030,))),
        )
        return replace(
            candidate,
            raw_rows=raw_rows_from_facts(candidate),
        ).with_checksum()

    first_boundary_game = boundary_game(
        source_observation_id="obs:boundary:first",
        retrieved_at=cutoff + timedelta(hours=1),
        rebounds=14,
    )
    corrected_boundary_game = boundary_game(
        source_observation_id="obs:boundary:corrected",
        retrieved_at=cutoff + timedelta(hours=2),
        rebounds=15,
    )

    def event_values(game, *, status_text, status_code):
        scheduled_at = datetime.combine(game.game_date, datetime.min.time(), UTC)
        return {
            "nba_game_id": game.game_id,
            "season": game.season,
            "home_team_id": game.home_team_id,
            "home_team_name": f"Team {game.home_team_id}",
            "home_team_tricode": game.home_team_tricode,
            "away_team_id": game.away_team_id,
            "away_team_name": f"Team {game.away_team_id}",
            "away_team_tricode": game.away_team_tricode,
            "scheduled_at": scheduled_at,
            "status_text": status_text,
            "status_code": status_code,
            "classification": "Regular Season",
            "first_seen_at": cutoff,
            "last_seen_at": cutoff,
        }

    with engine.begin() as connection:
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=cutoff, activated_at=cutoff, activated_by="test",
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id=manifest_id, season="2025-26", cutoff=cutoff,
            collect_before=cutoff + timedelta(days=30), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum=manifest_id,
            status="active", created_at=cutoff,
        ))
        connection.execute(EventCatalogEntry.__table__.insert(), [
            *(
                event_values(game, status_text="Final", status_code=3)
                for game in games
            ),
            event_values(
                first_boundary_game,
                status_text="Scheduled",
                status_code=1,
            ),
        ])

    publications = PublicationService(engine, clock=lambda: runtime_now)
    publications.register_default_streams()
    for stream_key, windows in (
        ("traditional_opponent_season", ("season",)),
        ("traditional_opponent_l15", ("l15",)),
    ):
        publications.register_stream(
            stream_key,
            provider="ledger",
            owner="railway",
            required_observations=("canonical_game_ledger",),
            publication_strategy="ledger_compose",
            supported_windows=windows,
            enabled=True,
            completeness_rule="league_complete",
            freshness_rule="cutoff_current",
        )

    queue = LedgerCorrectionQueue(
        clock=lambda: runtime_now,
        require_governance=True,
    )
    ledger = CanonicalGameLedgerRepository(engine, correction_sink=queue)
    ledger.replace_games_atomic(
        games,
        accepted_observations={
            game.source_observation_id: _boundary_observation_values(
                game, cutoff=cutoff, manifest_id=manifest_id
            )
            for game in games
        },
    )
    matchup = TeamMatchupRepository(engine)
    matchup_materialization = LedgerMatchupMaterializationService(
        ledger, matchup, clock=lambda: runtime_now
    )
    materialization = LedgerMaterializationService(
        ledger,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=LegacyParityDiagnosticReader(engine),
        publication_service=publications,
        clock=lambda: runtime_now,
    )
    governance_reader = ActiveManifestLedgerGovernanceReader(
        engine, clock=lambda: runtime_now
    )
    runtime = LedgerRuntime(
        backfill=None,
        repository=ledger,
        materialization=materialization,
        governance=governance_reader,
        matchup_materialization=matchup_materialization,
        publication_service=publications,
        clock=lambda: runtime_now,
    )

    before_governance = governance_reader.read_for_composition(
        "2025-26", cutoff, manifest_id
    )
    assert boundary_game_id not in before_governance.expected_game_ids
    assert len(before_governance.expected_game_ids) == len(games)
    assert len(before_governance.expected_l15_game_ids[1]) == 15
    assert runtime.compose_queued("2025-26") == len(LedgerCorrectionQueue.STREAMS)

    query = TeamMatchupQueryService(matchup, clock=lambda: runtime_now)
    l15_scope = TeamMatchupSnapshotScope("2025-26", cutoff.date(), 15)
    season_scope = TeamMatchupSnapshotScope("2025-26", cutoff.date())
    before_l15 = matchup.get_snapshot(l15_scope)
    before_l15_fact = next(
        fact
        for fact in before_l15.facts
        if fact.team_id == 1
        and fact.base == "traditional"
        and fact.stat_key == "OPP_REB"
    )
    before_l15_observation = next(
        observation
        for observation in before_l15.observations
        if observation.surface == "traditional"
    )
    before_season = matchup.get_snapshot(season_scope)
    before_season_observation = next(
        observation
        for observation in before_season.observations
        if observation.surface == "traditional"
    )
    before_l15_metric = next(
        metric
        for metric in query.get_window(l15_scope).team_metrics[1]
        if metric.base == "traditional" and metric.stat_key == "OPP_REB"
    )
    before_selected_ids = frozenset(before_l15_fact.game_ids)
    before_governed_ids = frozenset(before_governance.expected_l15_game_ids[1])
    assert before_selected_ids == before_governed_ids
    assert len(before_selected_ids) == 15
    assert boundary_game_id not in before_selected_ids
    assert before_l15_fact.raw_value == 75
    assert before_l15_metric.allowed_per_48 == 5.0
    assert before_l15_fact.game_set_checksum == (
        "5df170f5cd61b6674db39e5e2bb4c3ff7e5baf7d2a0c2ededfc2970eb216ccc9"
    )
    assert before_l15_observation.game_set_checksum
    assert before_l15_observation.ledger_checksum
    assert len(before_season_observation.game_ids) == len(games)

    with engine.begin() as connection:
        connection.execute(
            update(EventCatalogEntry)
            .where(EventCatalogEntry.nba_game_id == boundary_game_id)
            .values(status_text="Final", status_code=3, last_seen_at=runtime_now)
        )
    after_governance = governance_reader.read_for_composition(
        "2025-26", cutoff, manifest_id
    )
    after_governed_ids = frozenset(after_governance.expected_l15_game_ids[1])
    assert boundary_game_id in after_governance.expected_game_ids
    assert boundary_game_id in after_governed_ids
    assert len(after_governance.expected_game_ids) == len(games) + 1
    assert len(after_governed_ids) == 15
    assert len(before_governed_ids & after_governed_ids) == 14

    first_result = ledger.replace_games_atomic(
        (first_boundary_game,),
        accepted_observations={
            first_boundary_game.source_observation_id: _boundary_observation_values(
                first_boundary_game, cutoff=cutoff, manifest_id=manifest_id
            ),
        },
    )
    correction_result = ledger.replace_games_atomic(
        (corrected_boundary_game,),
        accepted_observations={
            corrected_boundary_game.source_observation_id: _boundary_observation_values(
                corrected_boundary_game,
                cutoff=cutoff,
                manifest_id=manifest_id,
            ),
        },
    )
    assert first_result[0].inserted
    assert correction_result[0].replaced
    assert correction_result[0].checksum == corrected_boundary_game.checksum
    assert correction_result[0].checksum != first_result[0].checksum

    assert runtime.compose_queued("2025-26") == len(LedgerCorrectionQueue.STREAMS)
    after_l15 = matchup.get_snapshot(l15_scope)
    after_l15_fact = next(
        fact
        for fact in after_l15.facts
        if fact.team_id == 1
        and fact.base == "traditional"
        and fact.stat_key == "OPP_REB"
    )
    after_l15_observation = next(
        observation
        for observation in after_l15.observations
        if observation.surface == "traditional"
    )
    after_season = matchup.get_snapshot(season_scope)
    after_season_fact = next(
        fact
        for fact in after_season.facts
        if fact.team_id == 1
        and fact.base == "traditional"
        and fact.stat_key == "OPP_REB"
    )
    after_season_observation = next(
        observation
        for observation in after_season.observations
        if observation.surface == "traditional"
    )
    after_l15_metric = next(
        metric
        for metric in query.get_window(l15_scope).team_metrics[1]
        if metric.base == "traditional" and metric.stat_key == "OPP_REB"
    )
    after_l15_union = frozenset(after_governance.expected_game_ids)
    assert frozenset(after_l15_fact.game_ids) == after_governed_ids
    assert frozenset(after_l15_fact.game_ids) != before_selected_ids
    assert after_l15_fact.raw_value == 85
    assert after_l15_metric.allowed_per_48 == pytest.approx(17 / 3)
    assert after_l15_fact.game_set_checksum == (
        "899e6e47627677b3ea20ef7a231dac681f87629586854744c025c55189ea36e7"
    )
    assert after_l15_observation.game_ids == tuple(sorted(after_l15_union))
    assert after_l15_observation.game_set_checksum != before_l15_observation.game_set_checksum
    assert after_l15_observation.ledger_checksum != before_l15_observation.ledger_checksum
    assert corrected_boundary_game.source_observation_id in after_l15_fact.source_observation_ids
    assert after_l15_fact.game_set_checksum != before_l15_fact.game_set_checksum
    assert after_l15_fact.ledger_checksum != before_l15_fact.ledger_checksum
    assert after_l15_observation.recomposition_reason == "correction"

    assert after_season_observation.status == "available"
    assert after_season_observation.game_ids == tuple(
        sorted(after_governance.expected_game_ids)
    )
    assert len(after_season_observation.game_ids) == len(games) + 1
    assert after_season_fact.raw_value == 90
    assert corrected_boundary_game.source_observation_id in after_season_fact.source_observation_ids
    assert after_season_observation.ledger_checksum != before_season_observation.ledger_checksum
    assert after_season_observation.recomposition_reason == "correction"

    assert publications.current("traditional_opponent_l15") is not None
    assert publications.current("traditional_opponent_season") is not None
    assert publications.current("traditional_opponent_l15").reason == "correction"
    assert publications.current("traditional_opponent_season").reason == "correction"
    stored_boundary_game = ledger.get_game(boundary_game_id)
    assert stored_boundary_game is not None
    assert stored_boundary_game.source_observation_id == corrected_boundary_game.source_observation_id
    assert stored_boundary_game.checksum == corrected_boundary_game.checksum
    with engine.connect() as connection:
        jobs = connection.execute(select(CompositionJob)).mappings().all()
    assert len(jobs) == len(LedgerCorrectionQueue.STREAMS)
    assert all(job["status"] == "succeeded" for job in jobs)
    assert all(job["recomposition_reason"] == "correction" for job in jobs)
    assert all(
        corrected_boundary_game.source_observation_id
        in json.loads(job["source_observation_ids"])
        for job in jobs
    )


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
