"""Correction propagation contracts for the canonical ledger seam (#116)."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from types import SimpleNamespace
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from app.migrations import run_migrations
from app.models.collection_control import (
    ActiveSeason,
    CatalogPublication,
    CollectionManifest,
    CollectionObservation,
    CompositionJob,
    PublicationPointer,
    PublicationObservation,
    PublicationStream,
    PublicationVersion,
    ReconciliationItem,
)
from app.models.event_catalog import EventCatalogEntry
from app.models.canonical_game_ledger import LedgerParityArtifact, LedgerPublication
from app.models.canonical_game_ledger import LedgerObservationEvidence
from app.services.collection_control import ControlPlaneError, PublicationService
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
from app.services.ledger_lineage import LedgerLineage
from app.services.ledger_matchup_materialization import LedgerMatchupMaterializationService
from app.services.ledger_parity import LedgerParityArtifactRepository, LegacyParityDiagnosticReader
from app.services.ledger_runtime import (
    ActiveManifestLedgerGovernanceReader,
    LedgerGovernance,
    LedgerRuntime,
)
from app.domain.slate_time import slate_date_for_instant
from app.services.team_matchup_repository import (
    TeamMatchupFact,
    TeamMatchupObservation,
    TeamMatchupRepository,
    TeamMatchupSnapshotScope,
)
from app.services.team_matchup_query import TeamMatchupQueryService
from tests.services.test_ledger_derivations import _league_games


UTC = timezone.utc
AS_OF = datetime(2025, 10, 15, 5, 22, tzinfo=UTC)


def _event_catalog_publication(
    games,
    cutoff,
    publication_id,
    *,
    scheduled_game_ids=frozenset(),
):
    events = []
    for game in games:
        scheduled = game.game_id in scheduled_game_ids
        events.append({
            "nba_game_id": game.game_id,
            "home_team_id": game.home_team_id,
            "away_team_id": game.away_team_id,
            "phase": "Regular Season",
            "status": "Scheduled" if scheduled else "Final",
            "status_code": 1 if scheduled else 3,
            "scheduled_at": (
                datetime.combine(game.game_date, datetime.min.time(), UTC)
                + timedelta(hours=5, minutes=22)
            ).isoformat(),
        })
    payload = json.dumps(
        {"events": events}, sort_keys=True, separators=(",", ":")
    )
    return {
        "publication_id": publication_id,
        "season": "2025-26",
        "catalog_type": "event",
        "cutoff": cutoff,
        "version": "event-v1",
        "checksum": hashlib.sha256(payload.encode()).hexdigest(),
        "payload": payload,
        "complete": True,
        "published_at": cutoff - timedelta(minutes=1),
        "expires_at": None,
    }


def test_keyed_lineage_merge_is_commutative_and_replaces_only_corrected_game():
    baseline_a = LedgerLineage.single(
        game_id="game-a", source_observation_id="obs:a:old",
        ledger_checksum="checksum:a:old", cutoff=AS_OF,
        reason="initial_acceptance",
    )
    baseline_b = LedgerLineage.single(
        game_id="game-b", source_observation_id="obs:b",
        ledger_checksum="checksum:b", cutoff=AS_OF,
        reason="initial_acceptance",
    )
    correction_a = LedgerLineage.single(
        game_id="game-a", source_observation_id="obs:a:new",
        ledger_checksum="checksum:a:new", cutoff=AS_OF,
        reason="correction",
    )

    merged = baseline_a.merge(baseline_b).merge(correction_a)
    reverse = correction_a.merge(baseline_b.merge(baseline_a))
    repeated = merged.merge(correction_a)

    assert merged == reverse == repeated
    assert merged.game_ids == ("game-a", "game-b")
    assert merged.ledger_checksums == ("checksum:a:new", "checksum:b")
    assert set(merged.source_observation_ids) == {
        "obs:a:old", "obs:a:new", "obs:b",
    }
    assert merged.recomposition_reason == "correction"
    z_correction = LedgerLineage.single(
        game_id="game-a", source_observation_id="obs:a:z",
        ledger_checksum="zzz", cutoff=AS_OF,
        reason="correction", accepted_at=AS_OF + timedelta(hours=1),
    )
    a_correction = LedgerLineage.single(
        game_id="game-a", source_observation_id="obs:a:latest",
        ledger_checksum="aaa", cutoff=AS_OF,
        reason="correction", accepted_at=AS_OF + timedelta(hours=2),
    )
    assert z_correction.merge(a_correction).ledger_checksums == ("aaa",)
    assert a_correction.merge(z_correction).ledger_checksums == ("aaa",)


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


def test_replay_successful_correction_is_idempotent(tmp_path):
    """A replayed accepted correction leaves every governed projection unchanged."""
    engine = _engine(tmp_path, "replay.sqlite3")
    manifest_id = "replay-manifest"
    cutoff = AS_OF
    runtime_now = cutoff + timedelta(hours=18)
    games = _league_games()
    streams = (
        "traditional_opponent_season",
        "traditional_opponent_l15",
    )

    def event_values(game):
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
            "status_text": "Final",
            "status_code": 3,
            "classification": "Regular Season",
            "first_seen_at": cutoff,
            "last_seen_at": cutoff,
        }

    with engine.begin() as connection:
        catalog = _event_catalog_publication(
            games, cutoff, "replay-event-catalog"
        )
        connection.execute(CatalogPublication.__table__.insert().values(**catalog))
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=cutoff, activated_at=cutoff, activated_by="test",
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id=manifest_id, season="2025-26", cutoff=cutoff,
            collect_before=cutoff + timedelta(days=30), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum=manifest_id,
            event_catalog_publication_id=catalog["publication_id"],
            event_catalog_checksum=catalog["checksum"],
            status="active", created_at=cutoff,
        ))
        connection.execute(EventCatalogEntry.__table__.insert(), [
            event_values(game) for game in games
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

    governance = governance_reader.read_for_composition(
        "2025-26", cutoff, manifest_id
    )
    assert len(governance.expected_game_ids) == len(games)
    assert len(governance.expected_l15_game_ids) == 30
    assert runtime.compose_queued("2025-26") == len(LedgerCorrectionQueue.STREAMS)

    query = TeamMatchupQueryService(matchup, clock=lambda: runtime_now)
    season_scope = TeamMatchupSnapshotScope("2025-26", cutoff.date())
    l15_scope = TeamMatchupSnapshotScope("2025-26", cutoff.date(), 15)

    def public_state():
        season_snapshot = matchup.get_snapshot(season_scope)
        l15_snapshot = matchup.get_snapshot(l15_scope)
        season_window = query.get_window(season_scope)
        l15_window = query.get_window(l15_scope)
        season_fact = next(
            fact
            for fact in season_snapshot.facts
            if fact.team_id == 1
            and fact.base == "traditional"
            and fact.stat_key == "OPP_REB"
        )
        l15_fact = next(
            fact
            for fact in l15_snapshot.facts
            if fact.team_id == 1
            and fact.base == "traditional"
            and fact.stat_key == "OPP_REB"
        )
        season_metric = next(
            metric
            for metric in season_window.team_metrics[1]
            if metric.base == "traditional" and metric.stat_key == "OPP_REB"
        )
        l15_metric = next(
            metric
            for metric in l15_window.team_metrics[1]
            if metric.base == "traditional" and metric.stat_key == "OPP_REB"
        )
        return {
            "season_snapshot": season_snapshot,
            "l15_snapshot": l15_snapshot,
            "season_window": season_window,
            "l15_window": l15_window,
            "season_fact": season_fact,
            "l15_fact": l15_fact,
            "season_metric": season_metric,
            "l15_metric": l15_metric,
        }

    def durable_state():
        with engine.connect() as connection:
            jobs = tuple(sorted(
                (
                    row["job_id"],
                    row["stream_key"],
                    row["status"],
                    row["attempts"],
                    row["last_error"],
                    row["manifest_id"],
                    row["trigger_game_id"],
                    row["trigger_game_ids"],
                    row["affected_team_ids"],
                    row["source_observation_ids"],
                    row["ledger_checksum"],
                    row["game_set_checksum"],
                    row["ledger_evidence"],
                    row["recomposition_reason"],
                )
                for row in connection.execute(
                    select(CompositionJob.__table__)
                ).mappings()
            ))
            versions = tuple(sorted(
                (
                    row["stream_key"],
                    row["publication_id"],
                    row["version"],
                    row["status"],
                    row["checksum"],
                    row["reason"],
                    row["fence"],
                )
                for row in connection.execute(
                    select(PublicationVersion.__table__)
                ).mappings()
            ))
            pointers = tuple(sorted(
                (
                    row["stream_key"],
                    row["active_publication_id"],
                    row["previous_publication_id"],
                    row["fence"],
                    row["updated_at"],
                )
                for row in connection.execute(
                    select(PublicationPointer.__table__).where(
                        PublicationPointer.__table__.c.stream_key.in_(streams)
                    )
                ).mappings()
            ))
            publication_lineage = tuple(sorted(
                (
                    row["publication_id"],
                    row["observation_id"],
                    row["role"],
                    row["slice_key"],
                )
                for row in connection.execute(
                    select(PublicationObservation.__table__)
                ).mappings()
            ))
            collection_observations = tuple(sorted(
                (
                    row["observation_id"],
                    row["checksum"],
                )
                for row in connection.execute(
                    select(CollectionObservation.__table__)
                ).mappings()
            ))
        return {
            "jobs": jobs,
            "versions": versions,
            "pointers": pointers,
            "publication_lineage": publication_lineage,
            "collection_observations": collection_observations,
        }

    baseline_public = public_state()
    baseline_durable = durable_state()
    assert len(baseline_durable["jobs"]) == len(LedgerCorrectionQueue.STREAMS)
    assert {row[2] for row in baseline_durable["jobs"]} == {"succeeded"}
    assert len(baseline_durable["versions"]) == 6
    assert len(baseline_durable["collection_observations"]) == len(games)
    assert len(baseline_durable["publication_lineage"]) == 1350
    assert baseline_public["season_fact"].raw_value == 75
    assert baseline_public["l15_fact"].raw_value == 75
    assert baseline_public["season_metric"].rank == 1
    assert baseline_public["l15_metric"].rank == 1

    original_game = games[0]
    corrected_team_facts = tuple(
        replace(
            fact,
            defensive_rebounds=fact.defensive_rebounds + 10,
            rebounds=fact.rebounds + 10,
        )
        if fact.team_id == original_game.away_team_id
        else fact
        for fact in original_game.team_facts
    )
    corrected_game = replace(
        original_game,
        team_facts=corrected_team_facts,
        source_observation_id="obs:replay:corrected",
        retrieved_at=cutoff + timedelta(days=20),
    )
    corrected_game = replace(
        corrected_game,
        raw_rows=raw_rows_from_facts(corrected_game),
    ).with_checksum()
    correction_observation = _boundary_observation_values(
        corrected_game, cutoff=cutoff, manifest_id=manifest_id
    )
    first_result = ledger.replace_games_atomic(
        (corrected_game,),
        accepted_observations={
            corrected_game.source_observation_id: correction_observation,
        },
    )[0]
    assert first_result.replaced
    assert not first_result.inserted
    assert first_result.checksum == corrected_game.checksum
    assert corrected_game.source_observation_id != original_game.source_observation_id
    assert corrected_game.checksum != original_game.checksum
    assert runtime.compose_queued("2025-26") == len(LedgerCorrectionQueue.STREAMS)

    after_success_public = public_state()
    after_success_durable = durable_state()
    assert len(after_success_durable["jobs"]) == len(baseline_durable["jobs"])
    assert {row[2] for row in after_success_durable["jobs"]} == {"succeeded"}
    assert after_success_public["season_fact"].raw_value == 85
    assert after_success_public["l15_fact"].raw_value == 85
    assert after_success_public["season_metric"].allowed_per_48 == pytest.approx(17 / 3)
    assert after_success_public["l15_metric"].allowed_per_48 == pytest.approx(17 / 3)
    assert after_success_public["season_metric"].rank == 30
    assert after_success_public["l15_metric"].rank == 30
    assert corrected_game.source_observation_id in (
        after_success_public["season_fact"].source_observation_ids
    )
    assert corrected_game.source_observation_id in (
        after_success_public["l15_fact"].source_observation_ids
    )
    assert after_success_public["season_fact"].ledger_checksum != (
        baseline_public["season_fact"].ledger_checksum
    )
    assert after_success_public["l15_fact"].ledger_checksum != (
        baseline_public["l15_fact"].ledger_checksum
    )
    assert after_success_public["season_fact"].ledger_checksum == (
        "53617324660245c98262bfe374f4a7e2daa1ac9f2428a1e1355d0b5822389f54"
    )
    assert after_success_public["l15_fact"].ledger_checksum == (
        "53617324660245c98262bfe374f4a7e2daa1ac9f2428a1e1355d0b5822389f54"
    )
    assert after_success_public["season_fact"].game_set_checksum == (
        "5df170f5cd61b6674db39e5e2bb4c3ff7e5baf7d2a0c2ededfc2970eb216ccc9"
    )
    assert after_success_public["l15_fact"].game_set_checksum == (
        "5df170f5cd61b6674db39e5e2bb4c3ff7e5baf7d2a0c2ededfc2970eb216ccc9"
    )
    assert len(after_success_durable["collection_observations"]) == len(
        baseline_durable["collection_observations"]
    ) + 1
    assert len(after_success_durable["versions"]) == 12
    assert len(after_success_durable["publication_lineage"]) == 2700
    assert all(row[2] == "succeeded" for row in after_success_durable["jobs"])
    assert all(
        row[6] is None
        and json.loads(row[7]) == []
        and json.loads(row[8]) == []
        and json.loads(row[9]) == []
        and row[10] is None
        and row[11] is None
        and json.loads(row[12]) == {}
        and row[13] is None
        for row in after_success_durable["jobs"]
    )
    baseline_pointers = dict(
        (row[0], row) for row in baseline_durable["pointers"]
    )
    success_pointers = dict(
        (row[0], row) for row in after_success_durable["pointers"]
    )
    assert set(success_pointers) == set(streams)
    for stream in streams:
        assert baseline_pointers[stream][3] == 1
        assert success_pointers[stream][3] == 2
        assert success_pointers[stream][1] != baseline_pointers[stream][1]
        assert success_pointers[stream][3] == baseline_pointers[stream][3] + 1
    assert len(after_success_durable["versions"]) > len(
        baseline_durable["versions"]
    )
    assert all(
        row[4]
        for row in after_success_durable["versions"]
    )
    correction_rows = [
        row
        for row in after_success_durable["collection_observations"]
        if row[0] == corrected_game.source_observation_id
    ]
    assert correction_rows == [
        (
            corrected_game.source_observation_id,
            correction_observation["checksum"],
        )
    ]
    assert correction_observation["checksum"] == hashlib.sha256(
        correction_observation["payload"].encode()
    ).hexdigest()

    replay_result = ledger.replace_games_atomic(
        (corrected_game,),
        accepted_observations={
            corrected_game.source_observation_id: correction_observation,
        },
    )[0]
    assert replay_result.game_id == corrected_game.game_id
    assert replay_result.checksum == corrected_game.checksum
    assert not replay_result.inserted
    assert not replay_result.replaced
    assert replay_result.row_count == 0
    assert publications.reconcile_pending(
        season="2025-26", cutoff=cutoff
    ) == 0
    assert runtime.compose_queued("2025-26") == 0

    after_replay_public = public_state()
    after_replay_durable = durable_state()
    assert after_replay_public == after_success_public
    assert after_replay_durable == after_success_durable
    assert len(after_replay_durable["jobs"]) == len(after_success_durable["jobs"])
    assert len(after_replay_durable["versions"]) == len(after_success_durable["versions"])
    assert after_replay_durable["pointers"] == after_success_durable["pointers"]
    assert after_replay_durable["publication_lineage"] == (
        after_success_durable["publication_lineage"]
    )
    assert after_replay_durable["collection_observations"] == (
        after_success_durable["collection_observations"]
    )


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


def test_completed_lineage_is_not_reintroduced_into_later_pending_triggers(tmp_path):
    engine = _engine(tmp_path, "completed-lineage.sqlite3")
    queue = LedgerCorrectionQueue(clock=lambda: AS_OF)
    repository = CanonicalGameLedgerRepository(engine, correction_sink=queue)
    first, second = _league_games()[:2]
    repository.replace_games_atomic((first, second))
    with engine.begin() as connection:
        connection.execute(update(CompositionJob.__table__).values(
            status="succeeded",
            trigger_game_ids=json.dumps([first.game_id, second.game_id]),
            trigger_game_id=None,
            ledger_evidence=json.dumps({
                first.game_id: first.checksum,
                second.game_id: second.checksum,
            }),
        ))
    corrected_first = replace(
        first,
        team_facts=tuple(replace(fact, points=fact.points + 1) for fact in first.team_facts),
    )
    corrected_first = replace(corrected_first, raw_rows=raw_rows_from_facts(corrected_first)).with_checksum()
    repository.replace_game(corrected_first)
    with engine.begin() as connection:
        connection.execute(update(CompositionJob.__table__).values(status="succeeded"))
    corrected_second = replace(
        second,
        team_facts=tuple(replace(fact, points=fact.points + 1) for fact in second.team_facts),
    )
    corrected_second = replace(corrected_second, raw_rows=raw_rows_from_facts(corrected_second)).with_checksum()
    repository.replace_game(corrected_second)
    with engine.connect() as connection:
        rows = connection.execute(select(CompositionJob.__table__)).mappings().all()
    assert all(json.loads(row["trigger_game_ids"]) == [second.game_id] for row in rows)

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
    # Values remain untouched for unaffected teams, while their lineage is
    # atomically refreshed with the surface observation.  Keeping the whole
    # dataclass equal here would accept the mixed-provenance bug this test is
    # intended to guard against.
    assert {
        key: (
            fact.raw_value,
            fact.denominator_value,
            fact.game_ids,
            fact.game_set_checksum,
        )
        for key, fact in before_unaffected.items()
    } == {
        key: (
            fact.raw_value,
            fact.denominator_value,
            fact.game_ids,
            fact.game_set_checksum,
        )
        for key, fact in after_unaffected.items()
    }
    l15_observation_by_surface = {
        observation.surface: observation for observation in after.observations
    }
    for fact in after_unaffected.values():
        observation = l15_observation_by_surface[fact.base]
        assert fact.ledger_checksum == observation.ledger_checksum
        assert fact.cutoff == observation.cutoff
        assert fact.recomposition_reason == observation.recomposition_reason
    assert any(
        fact.team_id in affected and fact.recomposition_reason == "correction"
        for fact in after.facts
    )


def test_correction_changes_published_counts_and_rank(tmp_path, monkeypatch):
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
        catalog = _event_catalog_publication(
            games, cutoff, "ledger-event-catalog"
        )
        connection.execute(CatalogPublication.__table__.insert().values(**catalog))
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=cutoff, activated_at=cutoff, activated_by="test",
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="ledger-manifest", season="2025-26", cutoff=cutoff,
            collect_before=cutoff + timedelta(days=1000), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum="ledger-manifest",
            event_catalog_publication_id=catalog["publication_id"],
            event_catalog_checksum=catalog["checksum"],
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

    from app.config.settings import RuntimeSettings
    from app.dependencies import build_dependencies

    monkeypatch.setitem(
        sys.modules,
        "app.services.nl_service",
        SimpleNamespace(NLService=lambda *args, **kwargs: object()),
    )
    monkeypatch.setattr("app.utils.db.get_engine", lambda settings: engine)
    monkeypatch.setattr(
        "app.utils.cache_config.get_redis_client", lambda settings: None
    )
    dependencies = build_dependencies(RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
        database={"url": str(engine.url)},
    ))
    publications = dependencies.publication_service
    assert publications is not None
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

    ledger = dependencies.canonical_game_ledger_repository
    assert ledger is not None
    accepted = {
        game.source_observation_id: accepted_observation(game)
        for game in games
    }
    ledger.replace_games_atomic(games, accepted_observations=accepted)

    matchup_materialization = dependencies.ledger_matchup_materialization_service
    materialization = dependencies.ledger_materialization_service
    assert matchup_materialization is not None
    assert materialization is not None
    matchup = matchup_materialization.matchup_repository
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

    with pytest.raises(ControlPlaneError, match="legacy_write_fenced"):
        matchup.replace_snapshots(
            ((scope, (replace(before_fact, raw_value=999),), before_snapshot.observations),),
            retrieved_at=runtime_now,
        )

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
        retrieved_at=cutoff + timedelta(days=20),
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

    governance = runtime.governance.read_for_composition(
        "2025-26", cutoff, "ledger-manifest"
    )
    with publications.session() as unauthorized, unauthorized.begin():
        with pytest.raises(ControlPlaneError, match="legacy_write_fenced"):
            matchup_materialization.materialize(
                "2025-26",
                as_of=cutoff.date(),
                cutoff=cutoff,
                recomposition_reason="correction",
                affected_team_ids=frozenset({
                    original.home_team_id,
                    original.away_team_id,
                }),
                trigger_game_ids=frozenset({original.game_id}),
                expected_game_ids=governance.expected_game_ids,
                expected_l15_game_ids=governance.expected_l15_game_ids,
                team_ids=governance.team_ids,
                session=unauthorized,
            )

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
        job["trigger_game_id"] is None
        and json.loads(job["trigger_game_ids"]) == []
        and json.loads(job["source_observation_ids"]) == []
        and job["ledger_checksum"] is None
        and job["game_set_checksum"] is None
        and json.loads(job["ledger_evidence"]) == {}
        and job["recomposition_reason"] is None
        for job in jobs
    )


def test_ledger_matchup_write_authority_is_transaction_scoped_and_single_use(
    tmp_path,
):
    engine = _engine(tmp_path, "matchup-authority.sqlite3")
    repository = TeamMatchupRepository(engine)
    manifest_id = "authority-manifest"
    streams = LedgerCorrectionQueue.STREAMS
    claims = {f"authority-{index}": 7 for index, _ in enumerate(streams)}
    with engine.begin() as connection:
        connection.execute(CompositionJob.__table__.insert(), [
            {
                "job_id": job_id,
                "stream_key": stream_key,
                "manifest_id": manifest_id,
                "season": "2025-26",
                "cutoff": AS_OF,
                "status": "running",
                "attempts": 0,
                "created_at": AS_OF,
                "updated_at": AS_OF,
                "generation": generation,
                "claimed_generation": generation,
            }
            for (job_id, generation), stream_key in zip(claims.items(), streams)
        ])
        connection.execute(CompositionJob.__table__.insert().values(
            job_id="unrelated-authority",
            stream_key="player_game_logs",
            manifest_id=manifest_id,
            season="2025-26",
            cutoff=AS_OF + timedelta(days=1),
            status="running",
            attempts=0,
            created_at=AS_OF,
            updated_at=AS_OF,
            generation=7,
            claimed_generation=7,
        ))
        connection.execute(CompositionJob.__table__.insert(), [
            {
                "job_id": "stale-traditional",
                "stream_key": "traditional_opponent_season",
                "manifest_id": manifest_id,
                "season": "2025-26",
                "cutoff": AS_OF + timedelta(days=2),
                "status": "running",
                "attempts": 0,
                "created_at": AS_OF,
                "updated_at": AS_OF,
                "generation": 7,
                "claimed_generation": 7,
            },
            {
                "job_id": "stale-assist",
                "stream_key": "assist_locations_season",
                "manifest_id": manifest_id,
                "season": "2025-26",
                "cutoff": AS_OF + timedelta(days=2),
                "status": "running",
                "attempts": 0,
                "created_at": AS_OF,
                "updated_at": AS_OF,
                "generation": 8,
                "claimed_generation": 7,
            },
        ])

    season_scope = TeamMatchupSnapshotScope("2025-26", AS_OF.date())
    l15_scope = TeamMatchupSnapshotScope("2025-26", AS_OF.date(), 15)

    def snapshot(scope):
        return (
            scope,
            (
                TeamMatchupFact(
                    team_id=1,
                    base="traditional",
                    slice_key="traditional",
                    stat_key="OPP_REB",
                    raw_value=10,
                    denominator_value=48,
                    denominator_unit="minutes",
                    provider="ledger",
                    cutoff=AS_OF,
                ),
                TeamMatchupFact(
                    team_id=1,
                    base="assist_locations",
                    slice_key="assist_locations",
                    stat_key="OPP_ASSISTS_AT_RIM",
                    raw_value=4,
                    denominator_value=48,
                    denominator_unit="minutes",
                    provider="ledger",
                    cutoff=AS_OF,
                ),
            ),
            (
                TeamMatchupObservation(
                    "traditional", "available", cutoff=AS_OF
                ),
                TeamMatchupObservation(
                    "assist_locations", "available", cutoff=AS_OF
                ),
            ),
        )

    valid_snapshots = (snapshot(season_scope), snapshot(l15_scope))
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as session, session.begin():
        with pytest.raises(PermissionError, match="not_authorized"):
            with repository._ledger_recomposition_authority(
                session,
                claimed_job_generations={"unrelated-authority": 7},
                season="2025-26",
                cutoff=AS_OF + timedelta(days=1),
                manifest_id=manifest_id,
            ):
                pass
        with pytest.raises(PermissionError, match="not_authorized"):
            with repository._ledger_recomposition_authority(
                session,
                claimed_job_generations={"stale-traditional": 7},
                season="2025-26",
                cutoff=AS_OF + timedelta(days=2),
                manifest_id=manifest_id,
            ):
                pass
        with repository._ledger_recomposition_authority(
            session,
            claimed_job_generations=claims,
            season="2025-26",
            cutoff=AS_OF,
            manifest_id=manifest_id,
        ) as authority:
            with pytest.raises(AttributeError):
                authority.season = "1999-00"
            with sessions() as other, other.begin():
                with pytest.raises(PermissionError, match="not_authorized"):
                    repository.replace_ledger_snapshots(
                        valid_snapshots,
                        retrieved_at=AS_OF + timedelta(hours=18),
                        session=other,
                        authority=authority,
                    )
            with pytest.raises(PermissionError, match="not_authorized"):
                repository.replace_ledger_snapshots(
                    (
                        snapshot(TeamMatchupSnapshotScope("1999-00", AS_OF.date())),
                        snapshot(TeamMatchupSnapshotScope("1999-00", AS_OF.date(), 15)),
                    ),
                    retrieved_at=AS_OF + timedelta(hours=18),
                    session=session,
                    authority=authority,
                )
            with pytest.raises(PermissionError, match="not_authorized"):
                repository.replace_ledger_snapshots(
                    (
                        snapshot(TeamMatchupSnapshotScope(
                            "2025-26", AS_OF.date() + timedelta(days=1)
                        )),
                        snapshot(TeamMatchupSnapshotScope(
                            "2025-26", AS_OF.date() + timedelta(days=1), 15
                        )),
                    ),
                    retrieved_at=AS_OF + timedelta(hours=18),
                    session=session,
                    authority=authority,
                )
            partial = tuple(
                (scope, facts, observations[:1])
                for scope, facts, observations in valid_snapshots
            )
            with pytest.raises(PermissionError, match="not_authorized"):
                repository.replace_ledger_snapshots(
                    partial,
                    retrieved_at=AS_OF + timedelta(hours=18),
                    session=session,
                    authority=authority,
                )
            repository.replace_ledger_snapshots(
                valid_snapshots,
                retrieved_at=AS_OF + timedelta(hours=18),
                session=session,
                authority=authority,
            )
            with pytest.raises(PermissionError, match="not_authorized"):
                repository.replace_ledger_snapshots(
                    valid_snapshots,
                    retrieved_at=AS_OF + timedelta(hours=18),
                    session=session,
                    authority=authority,
                )
        assert repository._issued_authorities == {}
        with pytest.raises(RuntimeError, match="before consumption"):
            with repository._ledger_recomposition_authority(
                session,
                claimed_job_generations=claims,
                season="2025-26",
                cutoff=AS_OF,
                manifest_id=manifest_id,
            ):
                raise RuntimeError("before consumption")
        assert repository._issued_authorities == {}


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
        catalog = _event_catalog_publication(
            (*games, first_boundary_game),
            cutoff,
            f"{manifest_id}-event-catalog",
            scheduled_game_ids={first_boundary_game.game_id},
        )
        connection.execute(CatalogPublication.__table__.insert().values(**catalog))
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=cutoff, activated_at=cutoff, activated_by="test",
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id=manifest_id, season="2025-26", cutoff=cutoff,
            collect_before=cutoff + timedelta(days=30), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum=manifest_id,
            event_catalog_publication_id=catalog["publication_id"],
            event_catalog_checksum=catalog["checksum"],
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
        final_catalog = _event_catalog_publication(
            (*games, first_boundary_game),
            cutoff,
            f"{manifest_id}-event-catalog-final",
        )
        connection.execute(
            CatalogPublication.__table__.insert().values(**final_catalog)
        )
        connection.execute(
            update(CollectionManifest)
            .where(CollectionManifest.manifest_id == manifest_id)
            .values(
                event_catalog_publication_id=final_catalog["publication_id"],
                event_catalog_checksum=final_catalog["checksum"],
            )
        )
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
    assert all(
        job["trigger_game_id"] is None
        and json.loads(job["trigger_game_ids"]) == []
        and json.loads(job["source_observation_ids"]) == []
        and job["ledger_checksum"] is None
        and job["game_set_checksum"] is None
        and json.loads(job["ledger_evidence"]) == {}
        and job["recomposition_reason"] is None
        for job in jobs
    )


def test_correction_outside_l15_preserves_l15_publication(tmp_path):
    """A governed correction outside both teams' L15 leaves that publication active."""
    engine = _engine(tmp_path, "outside-l15.sqlite3")
    manifest_id = "outside-l15-manifest"
    cutoff = AS_OF + timedelta(days=1, hours=5, minutes=22)
    runtime_now = cutoff + timedelta(hours=18)
    games = _league_games()
    original_game = games[0]
    special_game_id = "outside-l15-special"
    special_team_facts = tuple(
        replace(
            fact,
            defensive_rebounds=fact.defensive_rebounds + 10,
            rebounds=fact.rebounds + 10,
        )
        if fact.team_id == original_game.away_team_id
        else fact
        for fact in original_game.team_facts
    )
    special_game = replace(
        original_game,
        game_id=special_game_id,
        game_date=original_game.game_date + timedelta(days=15),
        team_facts=special_team_facts,
        source_observation_id="obs:outside:special",
        retrieved_at=cutoff + timedelta(hours=1),
    )
    special_game = replace(
        special_game,
        raw_rows=raw_rows_from_facts(special_game),
    ).with_checksum()

    def event_values(game):
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
            "status_text": "Final",
            "status_code": 3,
            "classification": "Regular Season",
            "first_seen_at": cutoff,
            "last_seen_at": cutoff,
        }

    all_games = (*games, special_game)
    with engine.begin() as connection:
        catalog = _event_catalog_publication(
            all_games, cutoff, f"{manifest_id}-event-catalog"
        )
        connection.execute(CatalogPublication.__table__.insert().values(**catalog))
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=cutoff, activated_at=cutoff, activated_by="test",
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id=manifest_id, season="2025-26", cutoff=cutoff,
            collect_before=cutoff + timedelta(days=30), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum=manifest_id,
            event_catalog_publication_id=catalog["publication_id"],
            event_catalog_checksum=catalog["checksum"],
            status="active", created_at=cutoff,
        ))
        connection.execute(EventCatalogEntry.__table__.insert(), [
            event_values(game) for game in all_games
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
        all_games,
        accepted_observations={
            game.source_observation_id: _boundary_observation_values(
                game, cutoff=cutoff, manifest_id=manifest_id
            )
            for game in all_games
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
    assert len(before_governance.expected_game_ids) == len(all_games)
    assert original_game.game_id in before_governance.expected_game_ids
    assert original_game.game_id not in before_governance.expected_l15_game_ids[1]
    assert original_game.game_id not in before_governance.expected_l15_game_ids[
        original_game.away_team_id
    ]
    assert special_game_id in before_governance.expected_l15_game_ids[1]
    assert special_game_id in before_governance.expected_l15_game_ids[
        original_game.away_team_id
    ]
    assert 1 in before_governance.team_ids
    assert original_game.away_team_id in before_governance.team_ids
    assert runtime.compose_queued("2025-26") == len(LedgerCorrectionQueue.STREAMS)

    query = TeamMatchupQueryService(matchup, clock=lambda: runtime_now)
    season_scope = TeamMatchupSnapshotScope("2025-26", cutoff.date())
    l15_scope = TeamMatchupSnapshotScope("2025-26", cutoff.date(), 15)
    before_season = matchup.get_snapshot(season_scope)
    before_l15 = matchup.get_snapshot(l15_scope)
    before_season_fact = next(
        fact
        for fact in before_season.facts
        if fact.team_id == 1
        and fact.base == "traditional"
        and fact.stat_key == "OPP_REB"
    )
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
    before_season_observation = next(
        observation
        for observation in before_season.observations
        if observation.surface == "traditional"
    )
    before_l15_window = query.get_window(l15_scope)
    before_season_window = query.get_window(season_scope)
    before_l15_metric = next(
        metric
        for metric in before_l15_window.team_metrics[1]
        if metric.base == "traditional" and metric.stat_key == "OPP_REB"
    )
    before_season_metric = next(
        metric
        for metric in before_season_window.team_metrics[1]
        if metric.base == "traditional" and metric.stat_key == "OPP_REB"
    )

    streams = (
        "traditional_opponent_season",
        "traditional_opponent_l15",
    )

    def active_pointers():
        with engine.connect() as connection:
            return {
                row["stream_key"]: (
                    row["active_publication_id"],
                    row["fence"],
                )
                for row in connection.execute(
                    select(PublicationPointer.__table__).where(
                        PublicationPointer.__table__.c.stream_key.in_(streams)
                    )
                ).mappings()
            }

    def publication_lineage(publication_id):
        with engine.connect() as connection:
            return tuple(sorted(
                row["observation_id"]
                for row in connection.execute(
                    select(PublicationObservation.__table__).where(
                        PublicationObservation.__table__.c.publication_id
                        == publication_id
                    )
                ).mappings()
            ))

    before_publications = {
        stream: publications.current(stream) for stream in streams
    }
    assert all(publication is not None for publication in before_publications.values())
    before_pointers = active_pointers()
    before_publication_lineage = {
        stream: publication_lineage(publication.publication_id)
        for stream, publication in before_publications.items()
    }
    assert before_pointers["traditional_opponent_season"][0] == (
        before_publications["traditional_opponent_season"].publication_id
    )
    assert before_pointers["traditional_opponent_l15"][0] == (
        before_publications["traditional_opponent_l15"].publication_id
    )
    assert original_game.source_observation_id in before_season_fact.source_observation_ids
    assert original_game.source_observation_id not in before_l15_fact.source_observation_ids
    assert special_game.source_observation_id in before_l15_fact.source_observation_ids
    assert before_l15_fact.game_ids == tuple(
        sorted(before_governance.expected_l15_game_ids[1])
    )
    assert len(before_l15_fact.game_ids) == 15
    assert before_l15_fact.raw_value == 85
    assert before_season_fact.raw_value == 90
    assert before_season_fact.game_set_checksum == (
        "28914f0b58204e7799fb4d6131bac3c1940d80d952c6b5b4a436c84068f6a019"
    )
    assert before_season_fact.ledger_checksum == (
        "cf729f2d3aa24e1457d2bdd1e46f5ea6c9c7a47adefde6a17402ef5b11017ca4"
    )
    assert before_l15_fact.game_set_checksum == (
        "87a9c816d1bc8d5371d0c8c7bba4948b2297c18c04754a8913dfedbf8d4335d9"
    )
    assert before_l15_fact.ledger_checksum == (
        "c012e844ff5d8039c22e826365f5f61433afffad18c26292dff3554f6b8f5656"
    )
    assert before_l15_metric.allowed_per_48 == pytest.approx(85 / 15)
    assert before_season_metric.allowed_per_48 == pytest.approx(90 / 16)
    expected_season_publication_ids = tuple(
        sorted(before_governance.expected_game_ids)
    )
    assert before_season_observation.game_ids == expected_season_publication_ids
    assert before_season_observation.game_set_checksum == (
        "71283c6f1fa3ee3d325d24c639e80d7aa903fc64d4cc85d554447665b19b65af"
    )
    assert before_season_observation.ledger_checksum == (
        before_season_fact.ledger_checksum
    )
    assert original_game.source_observation_id in (
        before_season_observation.source_observation_ids
    )
    expected_l15_publication_ids = tuple(sorted({
        game_id
        for game_ids in before_governance.expected_l15_game_ids.values()
        for game_id in game_ids
    }))
    assert before_l15_observation.game_ids == expected_l15_publication_ids
    assert before_l15_observation.game_set_checksum
    assert before_l15_observation.ledger_checksum
    assert before_l15_observation.source_observation_ids == (
        tuple(sorted(
            game.source_observation_id
            for game in all_games
            if game.game_id in expected_l15_publication_ids
        ))
    )

    corrected_team_facts = tuple(
        replace(
            fact,
            defensive_rebounds=fact.defensive_rebounds + 10,
            rebounds=fact.rebounds + 10,
        )
        if fact.team_id == original_game.away_team_id
        else fact
        for fact in original_game.team_facts
    )
    corrected_game = replace(
        original_game,
        team_facts=corrected_team_facts,
        source_observation_id="obs:outside:corrected",
        retrieved_at=cutoff + timedelta(days=20),
    )
    corrected_game = replace(
        corrected_game,
        raw_rows=raw_rows_from_facts(corrected_game),
    ).with_checksum()
    result = ledger.replace_games_atomic(
        (corrected_game,),
        accepted_observations={
            corrected_game.source_observation_id: _boundary_observation_values(
                corrected_game, cutoff=cutoff, manifest_id=manifest_id
            ),
        },
    )
    assert result[0].replaced
    assert result[0].checksum == corrected_game.checksum
    assert corrected_game.checksum != original_game.checksum

    assert runtime.compose_queued("2025-26") == len(LedgerCorrectionQueue.STREAMS)
    with engine.connect() as connection:
        corrected_observation = connection.execute(
            select(CollectionObservation).where(
                CollectionObservation.observation_id
                == corrected_game.source_observation_id,
            )
        ).mappings().one()
        jobs = connection.execute(select(CompositionJob)).mappings().all()
    assert corrected_observation["checksum"] == hashlib.sha256(
        corrected_observation["payload"].encode()
    ).hexdigest()
    assert len(jobs) == len(LedgerCorrectionQueue.STREAMS)
    assert all(job["status"] == "succeeded" for job in jobs)
    after_season = matchup.get_snapshot(season_scope)
    after_l15 = matchup.get_snapshot(l15_scope)
    after_season_fact = next(
        fact
        for fact in after_season.facts
        if fact.team_id == 1
        and fact.base == "traditional"
        and fact.stat_key == "OPP_REB"
    )
    after_l15_fact = next(
        fact
        for fact in after_l15.facts
        if fact.team_id == 1
        and fact.base == "traditional"
        and fact.stat_key == "OPP_REB"
    )
    after_season_observation = next(
        observation
        for observation in after_season.observations
        if observation.surface == "traditional"
    )
    after_l15_observation = next(
        observation
        for observation in after_l15.observations
        if observation.surface == "traditional"
    )
    after_l15_window = query.get_window(l15_scope)
    after_season_window = query.get_window(season_scope)
    after_l15_metric = next(
        metric
        for metric in after_l15_window.team_metrics[1]
        if metric.base == "traditional" and metric.stat_key == "OPP_REB"
    )
    after_season_metric = next(
        metric
        for metric in after_season_window.team_metrics[1]
        if metric.base == "traditional" and metric.stat_key == "OPP_REB"
    )
    after_publications = {
        stream: publications.current(stream) for stream in streams
    }
    after_pointers = active_pointers()
    after_publication_lineage = {
        stream: publication_lineage(publication.publication_id)
        for stream, publication in after_publications.items()
    }

    after_season_publication = after_publications["traditional_opponent_season"]
    after_l15_publication = after_publications["traditional_opponent_l15"]
    before_season_publication = before_publications["traditional_opponent_season"]
    before_l15_publication = before_publications["traditional_opponent_l15"]
    assert after_season_publication is not None
    assert after_l15_publication is not None
    assert after_season_publication.publication_id != before_season_publication.publication_id
    assert after_season_publication.reason == "correction"
    assert after_l15_publication.publication_id == before_l15_publication.publication_id
    assert after_l15_publication.checksum == before_l15_publication.checksum
    assert after_pointers["traditional_opponent_season"][0] != before_pointers[
        "traditional_opponent_season"
    ][0]
    assert after_pointers["traditional_opponent_season"][1] == before_pointers[
        "traditional_opponent_season"
    ][1] + 1
    assert after_pointers["traditional_opponent_l15"] == before_pointers[
        "traditional_opponent_l15"
    ]
    assert after_season_fact.raw_value == 100
    assert after_season_fact.game_ids == before_season_fact.game_ids
    assert after_season_fact.game_set_checksum == before_season_fact.game_set_checksum
    assert after_season_fact.game_set_checksum == (
        "28914f0b58204e7799fb4d6131bac3c1940d80d952c6b5b4a436c84068f6a019"
    )
    assert after_season_fact.ledger_checksum == (
        "2d809ccf7b3be8eee45bce3c4c32d785afe53fd31b2375de37954986080115d5"
    )
    assert after_season_fact.ledger_checksum != before_season_fact.ledger_checksum
    assert corrected_game.source_observation_id in after_season_fact.source_observation_ids
    assert after_season_observation.ledger_checksum == after_season_fact.ledger_checksum
    assert after_season_observation.game_ids == expected_season_publication_ids
    assert after_season_observation.game_set_checksum == (
        "71283c6f1fa3ee3d325d24c639e80d7aa903fc64d4cc85d554447665b19b65af"
    )
    assert corrected_game.source_observation_id in (
        after_season_observation.source_observation_ids
    )
    assert after_season_observation.recomposition_reason == "correction"
    assert after_season_metric.allowed_per_48 == pytest.approx(100 / 16)
    assert after_season_metric.allowed_per_48 != before_season_metric.allowed_per_48
    assert after_l15 == before_l15
    assert after_l15_fact == before_l15_fact
    assert after_l15_fact.game_set_checksum == (
        "87a9c816d1bc8d5371d0c8c7bba4948b2297c18c04754a8913dfedbf8d4335d9"
    )
    assert after_l15_fact.ledger_checksum == (
        "c012e844ff5d8039c22e826365f5f61433afffad18c26292dff3554f6b8f5656"
    )
    assert after_l15_observation == before_l15_observation
    assert after_l15_metric == before_l15_metric
    assert after_publication_lineage["traditional_opponent_l15"] == (
        before_publication_lineage["traditional_opponent_l15"]
    )
    assert corrected_game.source_observation_id not in (
        after_l15_fact.source_observation_ids
    )
    assert corrected_game.source_observation_id not in (
        after_l15_observation.source_observation_ids
    )
    assert after_publications["traditional_opponent_season"].checksum != (
        before_publications["traditional_opponent_season"].checksum
    )
    assert corrected_game.source_observation_id in (
        after_publication_lineage["traditional_opponent_season"]
    )

    completed_jobs = {
        job["stream_key"]: job for job in jobs
        if job["stream_key"] in streams
    }
    completed_generations = {
        stream: int(completed_jobs[stream]["generation"])
        for stream in streams
    }
    assert publications.reconcile_pending(
        season="2025-26", cutoff=cutoff
    ) == 0
    assert publications.reconcile_pending(
        season="2025-26", cutoff=cutoff
    ) == 0
    with engine.connect() as connection:
        reconciled_jobs = {
            row["stream_key"]: row
            for row in connection.execute(select(CompositionJob)).mappings()
            if row["stream_key"] in streams
        }
    assert {
        stream: int(reconciled_jobs[stream]["generation"])
        for stream in streams
    } == completed_generations
    assert all(reconciled_jobs[stream]["status"] == "succeeded" for stream in streams)
    assert all(
        json.loads(reconciled_jobs[stream]["trigger_game_ids"]) == []
        and json.loads(reconciled_jobs[stream]["source_observation_ids"]) == []
        and json.loads(reconciled_jobs[stream]["ledger_evidence"]) == {}
        for stream in streams
    )
    assert publications.current("traditional_opponent_season").publication_id == (
        after_season_publication.publication_id
    )
    assert publications.current("traditional_opponent_l15").publication_id == (
        after_l15_publication.publication_id
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
            return publications.recompose_ledger_batch(
                compositions, session=kwargs["session"]
            )

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


def test_production_materializer_failure_rolls_back_all_read_models_and_candidates(
    tmp_path, monkeypatch
):
    """A failure after real prewrites leaves the last-good publication intact."""
    engine = _engine(tmp_path, "atomic-production.sqlite3")
    games = _league_games()
    queue = LedgerCorrectionQueue(
        clock=lambda: AS_OF + timedelta(hours=18),
    )
    ledger = CanonicalGameLedgerRepository(engine, correction_sink=queue)
    publications = PublicationService(engine, clock=lambda: AS_OF + timedelta(hours=18))
    publications.register_default_streams()
    for stream_key in ("player_game_logs", "traditional_opponent_season"):
        publications.register_stream(
            stream_key,
            provider="ledger",
            owner="railway",
            required_observations=("canonical_game_ledger",),
            publication_strategy="ledger_compose",
            supported_windows=("season",),
            enabled=True,
            completeness_rule="league_complete",
            freshness_rule="cutoff_current",
        )
    manifest_id = "atomic-manifest"
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id=manifest_id,
            season="2025-26",
            cutoff=AS_OF,
            collect_before=AS_OF + timedelta(days=30),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum=manifest_id,
            status="active",
            created_at=AS_OF,
        ))
        connection.execute(CollectionObservation.__table__.insert(), [
            {
                "observation_id": game.source_observation_id,
                "client_observation_id": game.source_observation_id,
                "collector_id": "test",
                "manifest_id": manifest_id,
                "environment": "server",
                "provider": "pbp",
                "observation_type": "canonical_game_ledger",
                "scope": json.dumps({
                    "surface": "canonical_game_ledger",
                    "game_id": game.game_id,
                }),
                "season": game.season,
                "cutoff": AS_OF,
                "schema_version": 1,
                "checksum": game.checksum,
                "payload": "{}",
                "payload_bytes": 2,
                "retrieved_at": game.retrieved_at,
                "accepted_at": game.retrieved_at,
            }
            for game in games
        ])
    ledger.replace_games_atomic(games)
    matchup = TeamMatchupRepository(engine)
    matchup_materialization = LedgerMatchupMaterializationService(
        ledger,
        matchup,
        clock=lambda: AS_OF + timedelta(hours=18),
    )

    class Parity:
        def read(self, stream_key, **kwargs):
            return ()

    materialization = LedgerMaterializationService(
        ledger,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=Parity(),
        publication_service=publications,
        clock=lambda: AS_OF + timedelta(hours=18),
    )
    expected = frozenset(game.game_id for game in games)
    expected_l15 = {
        team_id: frozenset(
            game.game_id
            for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }
    # Establish a real last-good read model and publication before the
    # correction worker is exercised.
    materialization.compose(
        games,
        season="2025-26",
        as_of=slate_date_for_instant(AS_OF),
        cutoff=AS_OF,
        expected_game_ids=expected,
        expected_l15_game_ids=expected_l15,
        team_ids=frozenset(range(1, 31)),
        activate=True,
        recomposition_reason="initial_acceptance",
    )
    matchup_materialization.materialize(
        "2025-26",
        as_of=slate_date_for_instant(AS_OF),
        cutoff=AS_OF,
        expected_game_ids=expected,
        expected_l15_game_ids=expected_l15,
        team_ids=frozenset(range(1, 31)),
        recomposition_reason="initial_acceptance",
    )
    before_snapshot = matchup.get_snapshot(TeamMatchupSnapshotScope(
        "2025-26", slate_date_for_instant(AS_OF), 15
    ))
    with engine.connect() as connection:
        before_pointers = {
            row["stream_key"]: (
                row["active_publication_id"],
                row["previous_publication_id"],
                row["fence"],
            )
            for row in connection.execute(select(PublicationPointer.__table__)).mappings()
            if row["stream_key"] in {"player_game_logs", "traditional_opponent_season"}
        }
        before_metadata = tuple(
            connection.execute(select(LedgerPublication.__table__)).mappings().all()
        )
        before_versions = tuple(
            connection.execute(select(PublicationVersion.__table__)).mappings().all()
        )
        before_parity = tuple(
            connection.execute(select(LedgerParityArtifact.__table__)).mappings().all()
        )

    original_game = games[0]
    corrected_game = replace(
        original_game,
        team_facts=tuple(
            replace(
                fact,
                defensive_rebounds=fact.defensive_rebounds + 3,
                rebounds=fact.rebounds + 3,
            )
            if fact.team_id == original_game.home_team_id
            else fact
            for fact in original_game.team_facts
        ),
        source_observation_id="obs:atomic:correction",
        retrieved_at=AS_OF + timedelta(hours=1),
    )
    corrected_game = replace(
        corrected_game,
        raw_rows=raw_rows_from_facts(corrected_game),
    ).with_checksum()
    correction_observation = {
        "observation_id": corrected_game.source_observation_id,
        "client_observation_id": corrected_game.source_observation_id,
        "collector_id": "test",
        "manifest_id": manifest_id,
        "environment": "server",
        "provider": "pbp",
        "observation_type": "canonical_game_ledger",
        "scope": json.dumps({
            "surface": "canonical_game_ledger",
            "game_id": corrected_game.game_id,
        }),
        "season": corrected_game.season,
        "cutoff": AS_OF,
        "schema_version": 1,
        "checksum": corrected_game.checksum,
        "payload": "{}",
        "payload_bytes": 2,
        "retrieved_at": corrected_game.retrieved_at,
        "accepted_at": corrected_game.retrieved_at,
    }
    with engine.begin() as connection:
        connection.execute(CollectionObservation.__table__.insert().values(
            **correction_observation
        ))
    ledger.replace_game(corrected_game)

    calls = {"count": 0}
    original_compose = publications._compose_active_in_session

    def fail_after_real_prewrites(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("injected production failure")
        return original_compose(*args, **kwargs)

    monkeypatch.setattr(
        publications,
        "_compose_active_in_session",
        fail_after_real_prewrites,
    )

    governance = LedgerGovernance(
        season="2025-26",
        cutoff=AS_OF,
        expected_game_ids=expected,
        expected_l15_game_ids=expected_l15,
        team_ids=frozenset(range(1, 31)),
        manifest_id=manifest_id,
    )

    class Governance:
        def read_for_composition(self, season, cutoff, manifest_id=None):
            return governance

    runtime = LedgerRuntime(
        backfill=None,
        repository=ledger,
        materialization=materialization,
        governance=Governance(),
        matchup_materialization=matchup_materialization,
        publication_service=publications,
        clock=lambda: AS_OF + timedelta(hours=18),
    )
    with pytest.raises(RuntimeError, match="injected production failure"):
        runtime.compose_queued("2025-26")
    assert calls["count"] == 2

    after_snapshot = matchup.get_snapshot(TeamMatchupSnapshotScope(
        "2025-26", slate_date_for_instant(AS_OF), 15
    ))
    assert after_snapshot == before_snapshot
    with engine.connect() as connection:
        after_pointers = {
            row["stream_key"]: (
                row["active_publication_id"],
                row["previous_publication_id"],
                row["fence"],
            )
            for row in connection.execute(select(PublicationPointer.__table__)).mappings()
            if row["stream_key"] in {"player_game_logs", "traditional_opponent_season"}
        }
        after_metadata = tuple(connection.execute(select(LedgerPublication.__table__)).mappings().all())
        after_versions = tuple(connection.execute(select(PublicationVersion.__table__)).mappings().all())
        after_parity = tuple(connection.execute(select(LedgerParityArtifact.__table__)).mappings().all())
        jobs = connection.execute(select(CompositionJob.__table__)).mappings().all()
    assert after_pointers == before_pointers
    assert after_metadata == before_metadata
    assert after_versions == before_versions
    assert after_parity == before_parity
    assert all(job["status"] == "failed" for job in jobs)
    assert all(job["last_error"] == "recomposition_failed" for job in jobs)


def test_correction_accepted_during_composition_survives_claim_cas_for_next_pass(
    tmp_path,
):
    """A running worker cannot acknowledge a newer queued lineage generation."""
    engine = _engine(tmp_path, "concurrent-generation.sqlite3")
    cutoff = datetime(2025, 10, 15, tzinfo=UTC)
    game = _league_games()[0]
    queue = LedgerCorrectionQueue(clock=lambda: cutoff)
    with engine.begin() as connection:
        connection.execute(CompositionJob.__table__.insert(), [
            {
                "job_id": f"generation-job-{stream_key}",
                "stream_key": stream_key,
                "season": game.season,
                "cutoff": cutoff,
                "status": "queued",
                "attempts": 0,
                "created_at": cutoff,
                "updated_at": cutoff,
                "generation": 1,
                "claimed_generation": None,
            }
            for stream_key in LedgerCorrectionQueue.STREAMS
        ])

    class Window:
        complete = True
        reason = None

    class Materialized:
        season_window = Window()
        l15_window = Window()
        assist_location_season = Window()
        assist_location_l15 = Window()

    class Governance:
        def read_for_composition(self, season, cutoff, manifest_id=None):
            return type(
                "Governance",
                (),
                {
                    "expected_game_ids": frozenset(),
                    "expected_l15_game_ids": {},
                    "team_ids": frozenset(),
                },
            )()

    calls = {"count": 0}
    accepted = None

    class Materialization:
        publication_service = None

        def compose(self, games, **kwargs):
            nonlocal accepted
            calls["count"] += 1
            if calls["count"] == 1:
                # Use the production queue against a separate connection.
                # SQLite waits for the worker transaction, so this models an
                # acceptance racing the real durable composition writes.
                accepted = threading.Thread(
                    target=lambda: _accept(),
                    daemon=True,
                )
                accepted.start()
                self.accepted = accepted
            return Materialized()

    repository = CanonicalGameLedgerRepository(engine)

    def _accept():
        with engine.begin() as connection:
            queue(connection, game)

    runtime = LedgerRuntime(
        backfill=None,
        repository=repository,
        materialization=Materialization(),
        governance=Governance(),
        clock=lambda: cutoff,
    )

    assert runtime.compose_queued(game.season) == len(LedgerCorrectionQueue.STREAMS)
    assert accepted is not None
    accepted.join(timeout=5)
    assert not accepted.is_alive()
    with engine.connect() as connection:
        queued = connection.execute(select(CompositionJob.__table__)).mappings().all()
    assert len(queued) == len(LedgerCorrectionQueue.STREAMS)
    assert all(row["status"] == "queued" for row in queued)
    assert all(row["generation"] == 2 for row in queued)
    assert all(json.loads(row["trigger_game_ids"]) == [game.game_id] for row in queued)

    # The next worker pass claims generation 2 and is the only one allowed to
    # acknowledge the accepted correction.
    assert runtime.compose_queued(game.season) == len(LedgerCorrectionQueue.STREAMS)
    with engine.connect() as connection:
        completed = connection.execute(select(CompositionJob.__table__)).mappings().all()
    assert all(row["status"] == "succeeded" for row in completed)
    assert all(row["generation"] == 2 for row in completed)
    assert all(row["claimed_generation"] is None for row in completed)


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
        job = connection.execute(select(CompositionJob.__table__).where(
            CompositionJob.stream_key == "traditional_opponent_season",
        )).mappings().one()
    assert job["status"] == "queued"
    assert job["attempts"] == 2
    assert job["last_error"] is None


@pytest.mark.parametrize("stream_enabled", [True, False], ids=("active", "inactive"))
@pytest.mark.parametrize("attached", [False, True], ids=("unattached", "attached"))
def test_scheduled_reconciliation_requeues_accepted_lineage_missing_from_success(
    tmp_path, stream_enabled, attached,
):
    """Reconciliation finds accepted evidence even when the prior job succeeded."""
    engine = _engine(tmp_path, "reconcile-lineage.sqlite3")
    game = _league_games()[0]
    cutoff = AS_OF
    manifest_id = "reconcile-lineage-manifest"
    ledger = CanonicalGameLedgerRepository(engine)
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id=manifest_id,
            season=game.season,
            cutoff=cutoff,
            collect_before=cutoff + timedelta(days=30),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum=manifest_id,
            status="active",
            created_at=cutoff,
        ))
    ledger.replace_games_atomic(
        (game,),
        accepted_observations={
            game.source_observation_id: _boundary_observation_values(
                game, cutoff=cutoff, manifest_id=manifest_id
            ),
        },
    )
    # A provider-only/raw correction can retain the typed checksum while
    # arriving under a new accepted source observation.
    raw_only_source = "obs:raw-only-revision"
    raw_only_values = _boundary_observation_values(
        game, cutoff=cutoff, manifest_id=manifest_id
    )
    raw_only_values["observation_id"] = raw_only_source
    raw_only_values["accepted_at"] = cutoff + timedelta(days=20)
    with engine.begin() as connection:
        connection.execute(CollectionObservation.__table__.insert().values(
            observation_id=raw_only_source,
            client_observation_id=raw_only_source,
            collector_id=raw_only_values["collector_id"],
            environment=raw_only_values["environment"],
            manifest_id=manifest_id,
            observation_type="canonical_game_ledger",
            provider="pbp",
            scope=raw_only_values["scope"],
            season=game.season,
            cutoff=cutoff,
            payload=raw_only_values["payload"],
            payload_bytes=raw_only_values["payload_bytes"],
            schema_version=raw_only_values["schema_version"],
            checksum=hashlib.sha256(raw_only_values["payload"].encode()).hexdigest(),
            retrieved_at=cutoff + timedelta(days=20),
            accepted_at=cutoff + timedelta(days=20),
        ))
        if attached:
            connection.execute(LedgerObservationEvidence.__table__.insert().values(
                observation_id=raw_only_source,
                game_id=game.game_id,
                created_at=cutoff + timedelta(days=20),
            ))
    publications = PublicationService(engine, clock=lambda: AS_OF)
    publications.register_default_streams()
    with engine.begin() as connection:
        connection.execute(PublicationStream.__table__.update().where(
            PublicationStream.stream_key == "traditional_opponent_season",
        ).values(enabled=stream_enabled))
        # An older publication proves the original accepted source was
        # composed, but must not hide a newer source that reconciliation still
        # needs to queue.
        connection.execute(PublicationVersion.__table__.insert().values(
            publication_id="prior-reconcile-publication",
            stream_key="traditional_opponent_season",
            season=game.season,
            cutoff=cutoff,
            version=1,
            status="active",
            checksum="p" * 64,
            payload="{}",
            created_at=cutoff,
            reason="initial_acceptance",
            fence=1,
        ))
        connection.execute(PublicationObservation.__table__.insert().values(
            publication_id="prior-reconcile-publication",
            observation_id=game.source_observation_id,
            role="completeness_evidence",
            created_at=cutoff,
        ))
        connection.execute(CompositionJob.__table__.insert().values(
            job_id="successful-but-unrepresented",
            stream_key="traditional_opponent_season",
            manifest_id=manifest_id,
            season=game.season,
            cutoff=cutoff,
            status="succeeded",
            attempts=0,
            created_at=cutoff,
            updated_at=cutoff,
            trigger_game_ids="[]",
            source_observation_ids="[]",
            ledger_evidence="{}",
            generation=1,
        ))

    reconciled = publications.reconcile_pending(
        season=game.season,
        cutoff=cutoff,
    )
    with engine.connect() as connection:
        job = connection.execute(select(CompositionJob.__table__).where(
            CompositionJob.stream_key == "traditional_opponent_season",
        )).mappings().one()
    if not attached:
        assert reconciled != len(LedgerCorrectionQueue.STREAMS)
        assert job["status"] == "succeeded"
        assert job["generation"] == 1
        assert json.loads(job["source_observation_ids"]) == []
        assert json.loads(job["ledger_evidence"]) == {}
        return
    assert reconciled == len(LedgerCorrectionQueue.STREAMS)
    assert job["status"] == "queued"
    assert job["generation"] == 2
    assert json.loads(job["trigger_game_ids"]) == [game.game_id]
    assert raw_only_source in json.loads(job["source_observation_ids"])
    assert set(json.loads(job["source_observation_ids"])) == {raw_only_source}
    assert json.loads(job["ledger_evidence"]) == {game.game_id: game.checksum}
    assert job["recomposition_reason"] == "correction"
    if not stream_enabled:
        rebuilt = publications.compose_inactive_ledger(
            "traditional_opponent_season",
            season=game.season,
            cutoff=cutoff,
            payload={"corrected": True},
            provenance={raw_only_source: game.game_id},
            reason="correction",
        )
        with publications.session() as session:
            assert rebuilt.status == "candidate"
            assert publications._publication_provenance_matches(
                session,
                rebuilt.publication_id,
                {raw_only_source: game.game_id},
            )
            assert session.get(
                PublicationVersion, "prior-reconcile-publication"
            ).status == "superseded"
            assert session.get(
                PublicationStream, "traditional_opponent_season"
            ).enabled is False


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
        as_of=slate_date_for_instant(AS_OF),
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
        as_of=slate_date_for_instant(AS_OF),
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


def test_ledger_batch_refreshes_cached_pointer_before_advancing_fence(tmp_path):
    """A reused session advances from the row locked after its cache was populated."""
    engine = _engine(tmp_path, "cached-pointer.sqlite3")
    publications = PublicationService(engine, clock=lambda: AS_OF)
    stream_key = "ledger_cache_regression"
    publications.register_stream(
        stream_key,
        provider="ledger",
        owner="railway",
        required_observations=("canonical_game_ledger",),
        publication_strategy="ledger_compose",
        enabled=True,
    )
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="cached-pointer-manifest",
            season="2025-26",
            cutoff=AS_OF,
            collect_before=AS_OF + timedelta(days=1000),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="cached-pointer-manifest",
            status="active",
            created_at=AS_OF,
        ))
        connection.execute(CollectionObservation.__table__.insert().values(
            observation_id="cached-pointer-observation",
            client_observation_id="cached-pointer-observation",
            collector_id="test",
            manifest_id="cached-pointer-manifest",
            environment="testing",
            provider="pbp",
            observation_type="canonical_game_ledger",
            scope=json.dumps({
                "game_id": "game-1",
                "surface": "canonical_game_ledger",
            }),
            season="2025-26",
            cutoff=AS_OF,
            schema_version=1,
            checksum="a" * 64,
            payload="{}",
            payload_bytes=2,
            retrieved_at=AS_OF,
            accepted_at=AS_OF,
        ))
    provenance = {"cached-pointer-observation": "game-1"}

    first = publications.recompose_ledger_batch((LedgerPublicationComposition(
        stream_key=stream_key,
        season="2025-26",
        cutoff=AS_OF,
        payload={"revision": 1},
        provenance=provenance,
        reason="initial acceptance",
    ),))[0]
    stale_session = publications.session()
    try:
        cached_pointer = stale_session.get(PublicationPointer, stream_key)
        assert cached_pointer is not None
        assert cached_pointer.fence == first.fence
        stale_session.commit()

        second = publications.compose(
            stream_key,
            season="2025-26",
            cutoff=AS_OF,
            payload={"revision": 2},
            expected_fence=first.fence,
            reason="first concurrent advance",
            ledger_provenance=provenance,
        )
        with stale_session.begin():
            third = publications.recompose_ledger_batch(
                (LedgerPublicationComposition(
                    stream_key=stream_key,
                    season="2025-26",
                    cutoff=AS_OF,
                    payload={"revision": 3},
                    provenance=provenance,
                    reason="cached session advance",
                ),),
                session=stale_session,
            )[0]
    finally:
        stale_session.close()

    with engine.connect() as connection:
        pointer = connection.execute(select(PublicationPointer.__table__).where(
            PublicationPointer.stream_key == stream_key,
        )).mappings().one()
        versions = connection.execute(select(PublicationVersion.__table__).where(
            PublicationVersion.stream_key == stream_key,
        )).mappings().all()
    assert third.fence == second.fence + 1
    assert pointer["fence"] == third.fence
    assert pointer["active_publication_id"] == third.publication_id
    assert pointer["previous_publication_id"] == second.publication_id
    assert [row["publication_id"] for row in versions if row["status"] == "active"] == [
        third.publication_id
    ]


def test_corrected_inactive_candidate_invalidates_stale_activation_target(tmp_path):
    engine = _engine(tmp_path, "inactive-candidate.sqlite3")
    publications = PublicationService(engine, clock=lambda: AS_OF)
    stream_key = "inactive_ledger_candidate"
    publications.register_stream(
        stream_key,
        provider="ledger",
        owner="railway",
        required_observations=("canonical_game_ledger",),
        publication_strategy="ledger_compose",
        enabled=False,
    )
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="inactive-candidate-manifest",
            season="2025-26",
            cutoff=AS_OF,
            collect_before=AS_OF + timedelta(days=1000),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="inactive-candidate-manifest",
            status="active",
            created_at=AS_OF,
        ))
        connection.execute(CollectionObservation.__table__.insert(), [
            {
                "observation_id": source_id,
                "client_observation_id": source_id,
                "collector_id": "test",
                "manifest_id": "inactive-candidate-manifest",
                "environment": "testing",
                "provider": "pbp",
                "observation_type": "canonical_game_ledger",
                "scope": json.dumps({
                    "game_id": "game-1",
                    "surface": "canonical_game_ledger",
                }),
                "season": "2025-26",
                "cutoff": AS_OF,
                "schema_version": 1,
                "checksum": checksum,
                "payload": payload,
                "payload_bytes": len(payload),
                "retrieved_at": accepted_at,
                "accepted_at": accepted_at,
            }
            for source_id, checksum, payload, accepted_at in (
                ("inactive-old", "a" * 64, "{}", AS_OF),
                (
                    "inactive-correction",
                    "b" * 64,
                    '{"corrected":true}',
                    AS_OF + timedelta(hours=1),
                ),
            )
        ])

    stale = publications.compose_inactive_ledger(
        stream_key,
        season="2025-26",
        cutoff=AS_OF,
        payload={"value": 1},
        provenance={"inactive-old": "game-1"},
        reason="initial acceptance",
    )
    corrected = publications.compose_inactive_ledger(
        stream_key,
        season="2025-26",
        cutoff=AS_OF,
        payload={"value": 2},
        provenance={"inactive-correction": "game-1"},
        reason="correction",
    )

    with publications.session() as session:
        assert session.get(PublicationVersion, stale.publication_id).status == "superseded"
        assert session.get(PublicationVersion, corrected.publication_id).status == "candidate"
    with pytest.raises(ControlPlaneError, match="publication_candidate_invalid"):
        publications.activate_stream(
            stream_key,
            reason="attempt stale activation",
            candidate_publication_id=stale.publication_id,
        )


def test_correction_batch_refreshes_stream_lock_after_candidate_activation(tmp_path):
    engine = _engine(tmp_path, "activation-race.sqlite3")
    publications = PublicationService(engine, clock=lambda: AS_OF)
    stream_key = "activation_race_ledger"
    publications.register_stream(
        stream_key,
        provider="ledger",
        owner="railway",
        required_observations=("canonical_game_ledger",),
        publication_strategy="ledger_compose",
        enabled=False,
    )
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="activation-race-manifest",
            season="2025-26",
            cutoff=AS_OF,
            collect_before=AS_OF + timedelta(days=1000),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="activation-race-manifest",
            status="active",
            created_at=AS_OF,
        ))
        connection.execute(CollectionObservation.__table__.insert(), [
            {
                "observation_id": source_id,
                "client_observation_id": source_id,
                "collector_id": "test",
                "manifest_id": "activation-race-manifest",
                "environment": "testing",
                "provider": "pbp",
                "observation_type": "canonical_game_ledger",
                "scope": json.dumps({
                    "game_id": "game-1",
                    "surface": "canonical_game_ledger",
                }),
                "season": "2025-26",
                "cutoff": AS_OF,
                "schema_version": 1,
                "checksum": checksum,
                "payload": payload,
                "payload_bytes": len(payload),
                "retrieved_at": accepted_at,
                "accepted_at": accepted_at,
            }
            for source_id, checksum, payload, accepted_at in (
                ("activation-old", "a" * 64, "{}", AS_OF),
                (
                    "activation-corrected",
                    "b" * 64,
                    '{"corrected":true}',
                    AS_OF + timedelta(hours=1),
                ),
            )
        ])
    stale = publications.compose_inactive_ledger(
        stream_key,
        season="2025-26",
        cutoff=AS_OF,
        payload={"value": 1},
        provenance={"activation-old": "game-1"},
        reason="initial acceptance",
    )
    cached_session = publications.session()
    try:
        cached_stream = cached_session.get(PublicationStream, stream_key)
        assert cached_stream is not None and cached_stream.enabled is False
        cached_session.commit()
        publications.activate_stream(
            stream_key,
            reason="activate prior candidate",
            candidate_publication_id=stale.publication_id,
        )

        with cached_session.begin():
            corrected = publications.recompose_ledger_batch(
                (LedgerPublicationComposition(
                    stream_key=stream_key,
                    season="2025-26",
                    cutoff=AS_OF,
                    payload={"value": 2},
                    provenance={"activation-corrected": "game-1"},
                    reason="correction",
                ),),
                session=cached_session,
            )[0]
    finally:
        cached_session.close()

    with engine.connect() as connection:
        pointer = connection.execute(select(PublicationPointer.__table__).where(
            PublicationPointer.stream_key == stream_key,
        )).mappings().one()
        statuses = dict(connection.execute(select(
            PublicationVersion.publication_id,
            PublicationVersion.status,
        ).where(PublicationVersion.stream_key == stream_key)).all())
    assert pointer["active_publication_id"] == corrected.publication_id
    assert statuses == {
        stale.publication_id: "superseded",
        corrected.publication_id: "active",
    }


@pytest.mark.parametrize("enabled", [False, True], ids=("inactive", "active"))
def test_correction_invalidates_same_game_candidates_across_cutoffs(
    tmp_path, enabled,
):
    engine = _engine(tmp_path, f"cross-cutoff-{enabled}.sqlite3")
    publications = PublicationService(engine, clock=lambda: AS_OF)
    stream_key = "cross_cutoff_ledger"
    publications.register_stream(
        stream_key,
        provider="ledger",
        owner="railway",
        required_observations=("canonical_game_ledger",),
        publication_strategy="ledger_compose",
        enabled=False,
    )
    cutoffs = (AS_OF, AS_OF + timedelta(days=1), AS_OF + timedelta(days=2))
    sources = ("cross-old-1", "cross-old-2", "cross-corrected")
    with engine.begin() as connection:
        for index, (cutoff, source_id) in enumerate(zip(cutoffs, sources)):
            manifest_id = f"cross-manifest-{index}"
            connection.execute(CollectionManifest.__table__.insert().values(
                manifest_id=manifest_id,
                season="2025-26",
                cutoff=cutoff,
                collect_before=cutoff + timedelta(days=1000),
                accepted_versions="[1]",
                scopes='["canonical_game_ledger"]',
                checksum=manifest_id,
                status="active" if index == 2 else "superseded",
                created_at=cutoff,
            ))
            connection.execute(CollectionObservation.__table__.insert().values(
                observation_id=source_id,
                client_observation_id=source_id,
                collector_id="test",
                manifest_id=manifest_id,
                environment="testing",
                provider="pbp",
                observation_type="canonical_game_ledger",
                scope=json.dumps({
                    "game_id": "game-1",
                    "surface": "canonical_game_ledger",
                }),
                season="2025-26",
                cutoff=cutoff,
                schema_version=1,
                checksum=str(index + 1) * 64,
                payload=json.dumps({"revision": index}),
                payload_bytes=len(json.dumps({"revision": index})),
                retrieved_at=cutoff,
                accepted_at=cutoff,
            ))
    stale_one = publications.compose_inactive_ledger(
        stream_key,
        season="2025-26",
        cutoff=cutoffs[0],
        payload={"value": 1},
        provenance={sources[0]: "game-1"},
    )
    stale_two = publications.compose_inactive_ledger(
        stream_key,
        season="2025-26",
        cutoff=cutoffs[1],
        payload={"value": 2},
        provenance={sources[1]: "game-1"},
    )
    publications.activate_stream(
        stream_key,
        reason="activate newest prior cutoff",
        candidate_publication_id=stale_two.publication_id,
    )
    if enabled:
        corrected = publications.recompose_ledger_batch((
            LedgerPublicationComposition(
                stream_key=stream_key,
                season="2025-26",
                cutoff=cutoffs[2],
                payload={"value": 3},
                provenance={sources[2]: "game-1"},
                reason="correction",
                corrected_provenance={sources[2]: "game-1"},
            ),
        ))[0]
    else:
        publications.register_stream(
            stream_key,
            provider="ledger",
            owner="railway",
            required_observations=("canonical_game_ledger",),
            publication_strategy="ledger_compose",
            enabled=False,
        )
        corrected = publications.compose_inactive_ledger(
            stream_key,
            season="2025-26",
            cutoff=cutoffs[2],
            payload={"value": 3},
            provenance={sources[2]: "game-1"},
            reason="correction",
            corrected_provenance={sources[2]: "game-1"},
        )

    with engine.connect() as connection:
        statuses = dict(connection.execute(select(
            PublicationVersion.publication_id,
            PublicationVersion.status,
        ).where(PublicationVersion.stream_key == stream_key)).all())
        pointer = connection.execute(select(PublicationPointer.__table__).where(
            PublicationPointer.stream_key == stream_key,
        )).mappings().one_or_none()
    assert statuses[stale_one.publication_id] == "superseded"
    assert statuses[stale_two.publication_id] == "superseded"
    assert statuses[corrected.publication_id] == (
        "active" if enabled else "candidate"
    )
    if enabled:
        assert pointer is not None
        assert pointer["active_publication_id"] == corrected.publication_id
        assert all(
            status != "active" or publication_id == pointer["active_publication_id"]
            for publication_id, status in statuses.items()
        )
    else:
        assert pointer is not None
        assert pointer["active_publication_id"] is None
        assert "active" not in statuses.values()
