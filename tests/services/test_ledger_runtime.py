"""Manifest-owned ledger runtime governance contracts."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, update

from app.migrations import run_migrations
from app.models.collection_control import ActiveSeason, CollectionManifest, CompositionJob
from app.models.event_catalog import EventCatalogEntry
from app.services.ledger_runtime import (
    ActiveManifestLedgerGovernanceReader,
    LedgerGovernance,
    LedgerRuntime,
)
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository, raw_rows_from_facts
from app.services.ledger_materialization import LedgerMaterializationService
from app.services.ledger_parity import LedgerParityArtifactRepository
from app.services.team_matchup_repository import (
    TeamMatchupRepository,
    TeamMatchupSnapshotScope,
)
from tests.services.test_ledger_derivations import _league_games


def test_runtime_governance_fails_closed_without_active_manifest(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'missing.sqlite3'}")
    run_migrations(engine)

    with pytest.raises(ValueError, match="active manifest"):
        ActiveManifestLedgerGovernanceReader(
            engine, clock=lambda: datetime(2025, 10, 1, tzinfo=timezone.utc)
        ).read(
            "2025-26", datetime(2025, 11, 1, tzinfo=timezone.utc)
        )


def test_runtime_governance_owns_exact_games_teams_cutoff_and_l15(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'governed.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2025, 11, 1, tzinfo=timezone.utc)
    teams = list(range(1, 31))
    events = []
    for round_index in range(15):
        for pair_index in range(15):
            home = teams[pair_index]
            away = teams[-1 - pair_index]
            game_id = f"game-{round_index:02d}-{pair_index:02d}"
            scheduled = cutoff - timedelta(days=15 - round_index)
            events.append({
                "nba_game_id": game_id,
                "season": "2025-26",
                "home_team_id": home,
                "home_team_name": f"Team {home}",
                "home_team_tricode": f"T{home:02d}",
                "away_team_id": away,
                "away_team_name": f"Team {away}",
                "away_team_tricode": f"T{away:02d}",
                "scheduled_at": scheduled,
                "status_text": "Final",
                "status_code": 3,
                "classification": "Regular Season",
                "first_seen_at": scheduled,
                "last_seen_at": cutoff,
            })
        teams = [teams[0], teams[-1], *teams[1:-1]]
    with engine.begin() as connection:
        events[0]["status_code"] = None
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=cutoff, activated_at=cutoff, activated_by="test",
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="manifest", season="2025-26", cutoff=cutoff,
            collect_before=cutoff + timedelta(hours=1), accepted_versions="[1]",
            scopes="[\"canonical_game_ledger\"]", checksum="manifest",
            status="active", created_at=cutoff,
        ))
        connection.execute(EventCatalogEntry.__table__.insert(), events)

    governance = ActiveManifestLedgerGovernanceReader(
        engine, clock=lambda: cutoff - timedelta(hours=1)
    ).read("2025-26", cutoff)

    assert governance.cutoff == cutoff
    assert len(governance.expected_game_ids) == 225
    assert len(governance.team_ids) == 30
    assert all(len(game_ids) == 15 for game_ids in governance.expected_l15_game_ids.values())
    assert ActiveManifestLedgerGovernanceReader(
        engine, clock=lambda: cutoff - timedelta(hours=1)
    ).read_for_collection("2025-26").expected_game_ids == governance.expected_game_ids

    with engine.begin() as connection:
        connection.execute(update(CollectionManifest).values(
            status="expired", collect_before=cutoff,
        ))
    after_deadline = ActiveManifestLedgerGovernanceReader(
        engine, clock=lambda: cutoff + timedelta(days=1)
    )
    assert after_deadline.read_for_composition(
        "2025-26", cutoff,
    ).expected_game_ids == governance.expected_game_ids
    with pytest.raises(ValueError, match="active manifest"):
        after_deadline.read_for_collection("2025-26")

    with engine.begin() as connection:
        connection.execute(update(EventCatalogEntry).where(
            EventCatalogEntry.nba_game_id == events[0]["nba_game_id"],
        ).values(postponed_status="postponed"))
    incomplete = ActiveManifestLedgerGovernanceReader(
        engine, clock=lambda: cutoff - timedelta(hours=1)
    ).read("2025-26", cutoff)
    assert len(incomplete.expected_l15_game_ids) == 30
    assert sorted(
        len(game_ids) for game_ids in incomplete.expected_l15_game_ids.values()
    ) == [14, 14, *([15] * 28)]


def test_composition_jobs_complete_independently_when_assists_are_missing(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    games = []
    for game in _league_games():
        without_locations = replace(
            game,
            player_facts=tuple(replace(
                player,
                two_point_assists=None, three_point_assists=None,
                arc3_assists=None, corner3_assists=None, at_rim_assists=None,
                short_mid_range_assists=None, long_mid_range_assists=None,
            ) for player in game.player_facts),
            checksum=None,
        )
        games.append(
            replace(without_locations, raw_rows=raw_rows_from_facts(without_locations)).with_checksum()
        )
    games = tuple(games)
    repository.replace_games_atomic(games)
    cutoff = datetime(2025, 10, 15, 5, 22, tzinfo=timezone.utc)
    team_ids = frozenset(range(1, 31))
    expected = frozenset(game.game_id for game in games)
    expected_l15 = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in team_ids
    }
    streams = (
        "player_game_logs", "traditional_opponent_season", "traditional_opponent_l15",
        "assist_locations_season", "assist_locations_l15", "player_per36",
    )
    with engine.begin() as connection:
        connection.execute(CompositionJob.__table__.insert(), [{
            "job_id": stream, "stream_key": stream, "manifest_id": None,
            "season": "2025-26", "cutoff": cutoff, "status": "queued",
            "attempts": 0, "created_at": cutoff, "updated_at": cutoff,
        } for stream in streams])

    class Governance:
        def read_for_composition(self, season, governed_cutoff, manifest_id=None):
            return LedgerGovernance(season, governed_cutoff, expected, team_ids, expected_l15)

    class Parity:
        def read(self, stream_key):
            return ()

    materialization = LedgerMaterializationService(
        repository,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=Parity(),
    )
    captured = {}
    real_compose = materialization.compose

    def capture_compose(*args, **kwargs):
        captured.update(kwargs)
        return real_compose(*args, **kwargs)

    materialization.compose = capture_compose
    runtime = LedgerRuntime(
        backfill=None,
        repository=repository,
        materialization=materialization,
        governance=Governance(),
        clock=lambda: cutoff,
    )

    assert runtime.compose_queued("2025-26") == 4
    assert captured["cutoff"] == cutoff
    with engine.connect() as connection:
        jobs = {
            row["stream_key"]: row
            for row in connection.execute(select(CompositionJob.__table__)).mappings()
        }
    assert jobs["player_per36"]["status"] == "succeeded"
    assert jobs["assist_locations_season"]["status"] == "failed"
    assert jobs["assist_locations_season"]["last_error"] == "assist_location_evidence_incomplete"


def test_compose_queued_publishes_ledger_matchup_facts_at_the_shared_cutoff(
    tmp_path,
):
    from app.services.ledger_matchup_materialization import (
        LedgerMatchupMaterializationService,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'matchup-compose.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    games = _league_games()
    repository.replace_games_atomic(games)
    cutoff = datetime(2025, 10, 15, 5, 22, tzinfo=timezone.utc)
    team_ids = frozenset(range(1, 31))
    expected = frozenset(game.game_id for game in games)
    expected_l15 = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in team_ids
    }
    with engine.begin() as connection:
        connection.execute(CompositionJob.__table__.insert().values(
            job_id="matchup", stream_key="traditional_opponent_season",
            manifest_id=None, season="2025-26", cutoff=cutoff, status="queued",
            attempts=0, created_at=cutoff, updated_at=cutoff,
        ))

    class Governance:
        def read_for_composition(self, season, governed_cutoff, manifest_id=None):
            return LedgerGovernance(
                season, governed_cutoff, expected, team_ids, expected_l15
            )

    class Parity:
        def read(self, stream_key):
            return ()

    matchup_materialization = LedgerMatchupMaterializationService(
        repository,
        TeamMatchupRepository(engine),
        clock=lambda: cutoff + timedelta(hours=1),
    )
    runtime = LedgerRuntime(
        backfill=None,
        repository=repository,
        materialization=LedgerMaterializationService(
            repository,
            parity_repository=LedgerParityArtifactRepository(engine),
            parity_reader=Parity(),
        ),
        governance=Governance(),
        matchup_materialization=matchup_materialization,
        clock=lambda: cutoff + timedelta(hours=1),
    )

    assert runtime.compose_queued("2025-26") == 1

    season = TeamMatchupRepository(engine).get_snapshot(
        TeamMatchupSnapshotScope("2025-26", cutoff.date())
    )
    assert {item.surface for item in season.observations} == {
        "traditional",
        "assist_locations",
    }
    assert {fact.base for fact in season.facts} == {
        "traditional",
        "assist_locations",
    }
    assert all(fact.ledger_checksum for fact in season.facts)
    assert all(fact.game_ids for fact in season.facts)


def test_compose_queued_persists_incomplete_governed_l15_as_missing(tmp_path):
    from types import SimpleNamespace

    from app.services.ledger_matchup_materialization import (
        LedgerMatchupMaterializationService,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'incomplete-l15.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    games = _league_games()
    governed = games[:-1]
    repository.replace_games_atomic(governed)
    cutoff = datetime(2025, 10, 15, 5, 22, tzinfo=timezone.utc)
    events = [{
        "nba_game_id": game.game_id,
        "season": game.season,
        "home_team_id": game.home_team_id,
        "home_team_name": f"Team {game.home_team_id}",
        "home_team_tricode": game.home_team_tricode,
        "away_team_id": game.away_team_id,
        "away_team_name": f"Team {game.away_team_id}",
        "away_team_tricode": game.away_team_tricode,
        "scheduled_at": datetime.combine(
            game.game_date, datetime.min.time(), timezone.utc
        ),
        "status_text": "Final",
        "status_code": 3,
        "classification": "Regular Season",
        "first_seen_at": datetime.combine(
            game.game_date, datetime.min.time(), timezone.utc
        ),
        "last_seen_at": cutoff,
    } for game in governed]
    with engine.begin() as connection:
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=cutoff, activated_at=cutoff, activated_by="test",
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="manifest", season="2025-26", cutoff=cutoff,
            collect_before=cutoff + timedelta(hours=1), accepted_versions="[1]",
            scopes="[\"canonical_game_ledger\"]", checksum="manifest",
            status="active", created_at=cutoff,
        ))
        connection.execute(EventCatalogEntry.__table__.insert(), events)
        connection.execute(CompositionJob.__table__.insert().values(
            job_id="matchup", stream_key="traditional_opponent_season",
            manifest_id="manifest", season="2025-26", cutoff=cutoff, status="queued",
            attempts=0, created_at=cutoff, updated_at=cutoff,
        ))

    class RecordingMaterialization:
        def __init__(self):
            self.compose_kwargs = None

        def compose(self, games, **kwargs):
            self.compose_kwargs = kwargs
            return SimpleNamespace(
                assist_location_season=None, assist_location_l15=None
            )

    materialization = RecordingMaterialization()
    runtime = LedgerRuntime(
        backfill=None,
        repository=repository,
        materialization=materialization,
        governance=ActiveManifestLedgerGovernanceReader(
            engine, clock=lambda: cutoff + timedelta(hours=1)
        ),
        matchup_materialization=LedgerMatchupMaterializationService(
            repository,
            TeamMatchupRepository(engine),
            clock=lambda: cutoff + timedelta(hours=1),
        ),
        clock=lambda: cutoff + timedelta(hours=1),
    )

    runtime.compose_queued("2025-26")

    l15 = TeamMatchupRepository(engine).get_snapshot(
        TeamMatchupSnapshotScope("2025-26", cutoff.date(), 15)
    )
    assert {
        (item.surface, item.status, item.unavailable_reason)
        for item in l15.observations
    } == {
        ("assist_locations", "missing", "insufficient_governed_games"),
        ("traditional", "missing", "insufficient_governed_games"),
    }
    assert len(materialization.compose_kwargs["expected_l15_game_ids"]) == 30
    assert {
        len(game_ids)
        for game_ids in materialization.compose_kwargs["expected_l15_game_ids"].values()
    } == {14, 15}


def test_refresh_fails_governance_before_any_backfill_or_provider_work():
    class Governance:
        def read_for_collection(self, season):
            raise ValueError("active manifest required")

    class Backfill:
        def refresh(self, *args, **kwargs):
            raise AssertionError("provider-capable backfill must not be entered")

    runtime = LedgerRuntime(
        backfill=Backfill(), repository=None, materialization=None,
        governance=Governance(),
    )

    with pytest.raises(ValueError, match="active manifest"):
        runtime.refresh("2025-26")


def test_refresh_rejects_expired_scope_and_version_before_backfill(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'authorization.sqlite3'}")
    run_migrations(engine)
    now = datetime(2025, 11, 1, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=now, activated_at=now, activated_by="test",
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="manifest", season="2025-26", cutoff=now,
            collect_before=now + timedelta(hours=1), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum="manifest",
            status="active", created_at=now,
        ))
        connection.execute(EventCatalogEntry.__table__.insert(), [{
            "nba_game_id": "game-1", "season": "2025-26",
            "home_team_id": 1, "home_team_name": "Team 1",
            "home_team_tricode": "T01",
            "away_team_id": 2, "away_team_name": "Team 2",
            "away_team_tricode": "T02",
            "scheduled_at": now - timedelta(days=1), "status_text": "Final",
            "status_code": 3, "classification": "Regular Season",
            "first_seen_at": now - timedelta(days=1), "last_seen_at": now,
        }])

    class Backfill:
        calls = 0
        kwargs = None

        def refresh(self, *args, **kwargs):
            self.calls += 1
            self.kwargs = kwargs
            return object()

    backfill = Backfill()
    runtime = LedgerRuntime(
        backfill=backfill, repository=None, materialization=None,
        governance=ActiveManifestLedgerGovernanceReader(
            engine, clock=lambda: now,
        ),
    )
    for values in (
        {"scopes": '["player_game_logs"]'},
        {"scopes": '["canonical_game_ledger"]', "accepted_versions": "[2]"},
        {"accepted_versions": "[1]", "collect_before": now},
    ):
        with engine.begin() as connection:
            connection.execute(update(CollectionManifest).values(**values))
        with pytest.raises(ValueError, match="active manifest"):
            runtime.refresh("2025-26")

    assert backfill.calls == 0
    with engine.begin() as connection:
        connection.execute(update(CollectionManifest).values(
            collect_before=now + timedelta(hours=1),
        ))
    runtime.refresh("2025-26")
    assert backfill.calls == 1
    assert backfill.kwargs["manifest_id"] == "manifest"
    assert backfill.kwargs["manifest_scope"] == "canonical_game_ledger"
    assert backfill.kwargs["collect_before"] == now + timedelta(hours=1)
