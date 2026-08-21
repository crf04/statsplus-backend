"""Manifest-owned ledger runtime governance contracts."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, select, update
from sqlalchemy.orm import sessionmaker

from app.migrations import MIGRATIONS, run_migrations
from app.models.collection_control import (
    ActiveSeason,
    CatalogPublication,
    CollectionManifest,
    CompositionJob,
)
from app.models.event_catalog import EventCatalogEntry
from app.services.ledger_runtime import (
    ActiveManifestLedgerGovernanceReader,
    LedgerGovernance,
    LedgerRuntime,
)
from app.services.ledger_backfill import LedgerBackfillService
from app.domain.nba_events import is_final_event, is_postponed_event, player_game_log_season_type
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository, raw_rows_from_facts
from app.collector.normalizers import normalize_schedule_response
from app.services.ledger_materialization import LedgerCorrectionQueue, LedgerMaterializationService
from app.services.ledger_parity import LedgerParityArtifactRepository
from app.services.team_matchup_publications import NBA_PUBLICATION_STREAM_KEYS
from app.services.team_matchup_repository import (
    TeamMatchupRepository,
    TeamMatchupSnapshotScope,
)
from tests.services.test_ledger_derivations import _league_games


def _catalog_events(games, cutoff):
    return [{
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
    } for game in games]


def _immutable_event_catalog(events, cutoff, *, published_at=None):
    payload = {
        "events": [
            {
                "nba_game_id": event["nba_game_id"],
                "home_team_id": event["home_team_id"],
                "away_team_id": event["away_team_id"],
                "phase": event.get("classification", "Regular Season"),
                "status": event.get("status_text", "Scheduled"),
                "status_code": event.get("status_code"),
                "scheduled_at": event["scheduled_at"].isoformat(),
                **(
                    {"completed": event["completed"]}
                    if "completed" in event
                    else {}
                ),
                **(
                    {"postponed_status": event["postponed_status"]}
                    if "postponed_status" in event
                    else {}
                ),
                **(
                    {"postponement_evidence": event["postponement_evidence"]}
                    if "postponement_evidence" in event
                    else {}
                ),
            }
            for event in events
        ]
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {
        "publication_id": f"event-catalog-{cutoff.timestamp()}",
        "season": "2025-26",
        "catalog_type": "event",
        "cutoff": cutoff,
        "version": "event-v1",
        "checksum": hashlib.sha256(encoded.encode()).hexdigest(),
        "payload": encoded,
        "complete": True,
        "published_at": published_at or cutoff - timedelta(minutes=1),
        "expires_at": None,
    }


def _manifest_catalog_binding(events, cutoff):
    catalog = _immutable_event_catalog(events, cutoff)
    return {
        "event_catalog_publication_id": catalog["publication_id"],
        "event_catalog_checksum": catalog["checksum"],
    }


def test_runtime_governance_fails_closed_without_active_manifest(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'missing.sqlite3'}")
    run_migrations(engine)

    with pytest.raises(ValueError, match="active manifest"):
        ActiveManifestLedgerGovernanceReader(
            engine, clock=lambda: datetime(2025, 10, 1, tzinfo=timezone.utc)
        ).read(
            "2025-26", datetime(2025, 11, 1, tzinfo=timezone.utc)
        )


def test_manifest_catalog_binding_migration_backfills_only_unambiguous_rows(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'binding-migration.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2025, 11, 1, tzinfo=timezone.utc)
    events = _catalog_events(_league_games()[:1], cutoff)
    catalog = _immutable_event_catalog(events, cutoff)
    ambiguous_cutoff = cutoff + timedelta(days=1)
    ambiguous_catalog = _immutable_event_catalog(
        events,
        ambiguous_cutoff,
        published_at=cutoff + timedelta(microseconds=1),
    )
    with engine.begin() as connection:
        connection.execute(CatalogPublication.__table__.insert().values(**catalog))
        connection.execute(
            CatalogPublication.__table__.insert().values(**ambiguous_catalog)
        )
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="legacy-boundable",
            season="2025-26",
            cutoff=cutoff,
            collect_before=cutoff + timedelta(hours=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="legacy-boundable",
            status="active",
            created_at=cutoff,
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="legacy-ambiguous",
            season="2025-26",
            cutoff=ambiguous_cutoff,
            collect_before=cutoff + timedelta(days=1, hours=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="legacy-ambiguous",
            status="superseded",
            created_at=cutoff,
        ))
        next(migration for migration in MIGRATIONS if migration.version == 38).upgrade(
            connection
        )
        rows = {
            row["manifest_id"]: row
            for row in connection.execute(
                CollectionManifest.__table__.select()
            ).mappings()
        }
    assert rows["legacy-boundable"]["event_catalog_publication_id"] == catalog[
        "publication_id"
    ]
    assert rows["legacy-boundable"]["event_catalog_checksum"] == catalog[
        "checksum"
    ]
    assert rows["legacy-ambiguous"]["event_catalog_publication_id"] is None
    assert any(
        foreign_key["constrained_columns"] == ["event_catalog_publication_id"]
        and foreign_key["referred_table"] == "collection_catalog_publications"
        and foreign_key["options"].get("ondelete") == "RESTRICT"
        for foreign_key in inspect(engine).get_foreign_keys(
            "collection_manifests"
        )
    )


def test_manifest_catalog_binding_migration_rejects_two_eligible_catalogs(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ambiguous-binding.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2025, 11, 2, tzinfo=timezone.utc)
    events = _catalog_events(_league_games()[:1], cutoff)
    first = _immutable_event_catalog(events, cutoff, published_at=cutoff)
    second = {
        **_immutable_event_catalog(events, cutoff, published_at=cutoff),
        "publication_id": "event-catalog-same-timestamp-b",
        "version": "event-v2",
    }
    with engine.begin() as connection:
        connection.execute(CatalogPublication.__table__.insert(), [first, second])
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="legacy-two-eligible",
            season="2025-26",
            cutoff=cutoff,
            collect_before=cutoff + timedelta(hours=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="legacy-two-eligible",
            status="active",
            created_at=cutoff,
        ))
        next(migration for migration in MIGRATIONS if migration.version == 38).upgrade(
            connection
        )
        manifest = connection.execute(
            CollectionManifest.__table__.select().where(
                CollectionManifest.manifest_id == "legacy-two-eligible"
            )
        ).mappings().one()
    assert manifest["event_catalog_publication_id"] is None
    assert manifest["event_catalog_checksum"] is None


def test_composition_governance_rejects_unbound_manifest_even_with_legacy_events(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'unbound-composition.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2025, 11, 2, 4, 30, tzinfo=timezone.utc)
    event = _catalog_events(_league_games()[:1], cutoff)[0]
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="unbound-composition",
            season="2025-26",
            cutoff=cutoff,
            collect_before=cutoff + timedelta(hours=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="unbound-composition",
            status="active",
            created_at=cutoff,
        ))
        connection.execute(EventCatalogEntry.__table__.insert().values(**event))

    with pytest.raises(ValueError, match="immutable Event Catalog"):
        ActiveManifestLedgerGovernanceReader(engine).read_for_composition(
            "2025-26", cutoff, "unbound-composition"
        )


def test_date_cutoff_lookup_uses_eastern_slate_day_across_fall_back(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fall-back.sqlite3'}")
    run_migrations(engine)
    # 23:30 EST on November 2 is 04:30 UTC on November 3.
    cutoff = datetime(2025, 11, 3, 4, 30, tzinfo=timezone.utc)
    events = _catalog_events(_league_games()[:1], cutoff)
    catalog = _immutable_event_catalog(events, cutoff)
    with engine.begin() as connection:
        connection.execute(CatalogPublication.__table__.insert().values(**catalog))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="fall-back-manifest",
            season="2025-26",
            cutoff=cutoff,
            collect_before=cutoff + timedelta(hours=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="fall-back-manifest",
            event_catalog_publication_id=catalog["publication_id"],
            event_catalog_checksum=catalog["checksum"],
            status="active",
            created_at=cutoff,
        ))

    reader = ActiveManifestLedgerGovernanceReader(engine)
    by_date = reader.resolve_team_game_ids(
        "2025-26",
        date(2025, 11, 2),
        window="season",
        manifest_id="fall-back-manifest",
    )
    by_instant = reader.resolve_team_game_ids(
        "2025-26",
        cutoff,
        window="season",
        manifest_id="fall-back-manifest",
    )
    assert by_date == by_instant


def test_immutable_governance_excludes_false_completion_and_postponed_final(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'event-truth.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2025, 11, 3, 4, 30, tzinfo=timezone.utc)
    events = _catalog_events(_league_games()[:1], cutoff)
    events.extend((
        {
            **events[0],
            "nba_game_id": "scheduled-string-false",
            "status_text": "Scheduled",
            "status_code": 1,
            "completed": "false",
        },
        {
            **events[0],
            "nba_game_id": "final-but-postponed",
            "status_text": "Final",
            "status_code": 3,
            "postponed_status": "postponed",
            "postponement_evidence": {"reason": "weather"},
        },
    ))
    catalog = _immutable_event_catalog(events, cutoff)
    with engine.begin() as connection:
        connection.execute(CatalogPublication.__table__.insert().values(**catalog))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="event-truth-manifest",
            season="2025-26",
            cutoff=cutoff,
            collect_before=cutoff + timedelta(hours=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="event-truth-manifest",
            event_catalog_publication_id=catalog["publication_id"],
            event_catalog_checksum=catalog["checksum"],
            status="active",
            created_at=cutoff,
        ))
    game_ids = ActiveManifestLedgerGovernanceReader(engine).resolve_team_game_ids(
        "2025-26",
        cutoff,
        window="season",
        manifest_id="event-truth-manifest",
    )
    governed = frozenset().union(*game_ids.values())
    assert "scheduled-string-false" not in governed
    assert "final-but-postponed" not in governed


def test_numeric_final_collector_shape_enters_immutable_governed_set(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'numeric-final.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2025, 11, 3, 4, 30, tzinfo=timezone.utc)
    normalized = normalize_schedule_response(
        [{
            "gameId": "0022500001",
            "homeTeam_teamId": 1,
            "awayTeam_teamId": 2,
            "gameDateTimeUTC": "2025-11-02T23:30:00-05:00",
            "gameStatus": 3,
        }],
        season="2025-26",
        cutoff=cutoff,
    )
    assert normalized.payload["events"][0]["status"] == "Final"
    encoded = json.dumps(
        normalized.payload, separators=(",", ":"), sort_keys=True
    )
    checksum = hashlib.sha256(encoded.encode()).hexdigest()
    with engine.begin() as connection:
        connection.execute(CatalogPublication.__table__.insert().values(
            publication_id="numeric-final-catalog",
            season="2025-26",
            catalog_type="event",
            cutoff=cutoff,
            version="event-v1",
            checksum=checksum,
            payload=encoded,
            complete=True,
            published_at=cutoff,
            expires_at=None,
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="numeric-final-manifest",
            season="2025-26",
            cutoff=cutoff,
            collect_before=cutoff + timedelta(hours=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="numeric-final-manifest",
            event_catalog_publication_id="numeric-final-catalog",
            event_catalog_checksum=checksum,
            status="active",
            created_at=cutoff,
        ))
    game_ids = ActiveManifestLedgerGovernanceReader(engine).resolve_team_game_ids(
        "2025-26",
        cutoff,
        window="season",
        manifest_id="numeric-final-manifest",
    )
    assert frozenset().union(*game_ids.values()) == {"0022500001"}


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
    retrospective = {
        "nba_game_id": "retrospective-final",
        "season": "2025-26",
        "home_team_id": 1,
        "home_team_name": "Team 1",
        "home_team_tricode": "T01",
        "away_team_id": 30,
        "away_team_name": "Team 30",
        "away_team_tricode": "T30",
        "scheduled_at": cutoff - timedelta(days=1),
        "status_text": "Scheduled",
        "status_code": 1,
        "classification": "Regular Season",
        "first_seen_at": cutoff - timedelta(days=1),
        "last_seen_at": cutoff,
    }
    events.append(retrospective)
    with engine.begin() as connection:
        events[0]["status_code"] = None
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=cutoff, activated_at=cutoff, activated_by="test",
        ))
        connection.execute(
            CatalogPublication.__table__.insert().values(
                **_immutable_event_catalog(events, cutoff)
            )
        )
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="manifest", season="2025-26", cutoff=cutoff,
            collect_before=cutoff + timedelta(hours=1), accepted_versions="[1]",
            scopes="[\"canonical_game_ledger\"]", checksum="manifest",
            **_manifest_catalog_binding(events, cutoff),
            status="active", created_at=cutoff,
        ))
        connection.execute(EventCatalogEntry.__table__.insert(), events)

    reader = ActiveManifestLedgerGovernanceReader(
        engine, clock=lambda: cutoff - timedelta(hours=1)
    )
    governance = reader.read("2025-26", cutoff)

    assert governance.cutoff == cutoff
    assert all(event["season"] == "2025-26" for event in governance.events)
    assert all(player_game_log_season_type(event) == "Regular Season" for event in governance.events)
    assert all(is_final_event(event) for event in governance.events)
    assert not any(is_postponed_event(event) for event in governance.events)
    assert len(LedgerBackfillService._authorized_events(
        governance.events, season="2025-26", through=cutoff,
    )) == 225
    assert len(governance.expected_game_ids) == 225
    assert len(governance.team_ids) == 30
    assert all(len(game_ids) == 15 for game_ids in governance.expected_l15_game_ids.values())
    season_by_team = reader.resolve_team_game_ids(
        "2025-26", cutoff, window="season"
    )
    assert set(season_by_team) == set(governance.team_ids)
    assert all(len(game_ids) == 15 for game_ids in season_by_team.values())
    assert frozenset().union(*season_by_team.values()) == governance.expected_game_ids
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
            EventCatalogEntry.nba_game_id == retrospective["nba_game_id"],
        ).values(status_text="Final", status_code=3))
        connection.execute(update(EventCatalogEntry).where(
            EventCatalogEntry.nba_game_id == events[0]["nba_game_id"],
        ).values(postponed_status="postponed"))
    unchanged = ActiveManifestLedgerGovernanceReader(
        engine, clock=lambda: cutoff - timedelta(hours=1)
    ).read("2025-26", cutoff)
    assert unchanged.expected_game_ids == governance.expected_game_ids
    assert unchanged.expected_l15_game_ids == governance.expected_l15_game_ids
    assert retrospective["nba_game_id"] not in unchanged.expected_game_ids
    with engine.begin() as connection:
        connection.execute(
            update(CatalogPublication).values(checksum="0" * 64)
        )
    with pytest.raises(ValueError, match="immutable Event Catalog"):
        reader.read("2025-26", cutoff)
    with engine.begin() as connection:
        connection.execute(update(CollectionManifest).values(
            event_catalog_publication_id=None,
            event_catalog_checksum=None,
        ))
        connection.execute(CatalogPublication.__table__.delete())
    with pytest.raises(ValueError, match="immutable Event Catalog"):
        reader.read("2025-26", cutoff)


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


def test_compose_queued_uses_eastern_slate_date_for_dst_utc_rollover(
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
    # 23:30 EST on November 2 is November 3 in UTC.
    cutoff = datetime(2025, 11, 3, 4, 30, tzinfo=timezone.utc)
    slate_date = date(2025, 11, 2)
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
    listed_through = []
    list_games = repository.list_games

    def capture_list_games(season, *, through=None):
        listed_through.append(through)
        return list_games(season, through=through)

    repository.list_games = capture_list_games
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
        TeamMatchupSnapshotScope("2025-26", slate_date)
    )
    assert listed_through
    assert set(listed_through) == {slate_date}
    assert {item.surface for item in season.observations} == {
        "traditional",
    }
    assert {fact.base for fact in season.facts} == {
        "traditional",
    }
    assert all(fact.ledger_checksum for fact in season.facts)
    assert all(fact.game_ids for fact in season.facts)


@pytest.mark.parametrize(
    ("cutoff", "slate_date"),
    (
        (datetime(2025, 11, 3, 4, 30, tzinfo=timezone.utc), date(2025, 11, 2)),
        (datetime(2025, 3, 10, 3, 30, tzinfo=timezone.utc), date(2025, 3, 9)),
    ),
    ids=("fall-back", "spring-forward"),
)
def test_publication_materialization_uses_eastern_slate_date_for_cutoff(
    tmp_path, cutoff, slate_date,
):
    engine = create_engine(f"sqlite:///{tmp_path / f'materialization-{slate_date}.sqlite3'}")
    run_migrations(engine)
    game = replace(
        _league_games()[0],
        game_date=slate_date - timedelta(days=1),
        retrieved_at=cutoff,
        checksum=None,
    )
    game = game.with_checksum()
    repository = CanonicalGameLedgerRepository(engine)
    repository.replace_games_atomic((game,))
    expected = frozenset({game.game_id})
    expected_l15 = {
        team_id: expected
        for team_id in (game.home_team_id, game.away_team_id)
    }

    class Publication:
        def compose_inactive_ledger(self, stream_key, **kwargs):
            return SimpleNamespace(
                publication_id=f"publication:{stream_key}",
                checksum="p" * 64,
            )

    class Parity:
        def record(self, *args, **kwargs):
            return None

        def read(self, stream_key, **kwargs):
            return ()

    materialization = LedgerMaterializationService(
        repository,
        parity_repository=Parity(),
        parity_reader=Parity(),
        publication_service=Publication(),
        clock=lambda: cutoff,
    )

    result = materialization.compose(
        (game,),
        season=game.season,
        as_of=slate_date,
        cutoff=cutoff,
        expected_game_ids=expected,
        expected_l15_game_ids=expected_l15,
        team_ids=frozenset(expected_l15),
    )

    assert result.as_of == slate_date


def test_nba_only_projection_uses_eastern_slate_date_after_utc_rollover(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'nba-only-rollover.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2025, 11, 3, 4, 30, tzinfo=timezone.utc)
    slate_date = date(2025, 11, 2)
    games = _league_games()
    repository = CanonicalGameLedgerRepository(engine)
    repository.replace_games_atomic(games)
    stream_key = next(iter(sorted(NBA_PUBLICATION_STREAM_KEYS)))
    with engine.begin() as connection:
        connection.execute(CompositionJob.__table__.insert().values(
            job_id="nba-only-rollover",
            stream_key=stream_key,
            manifest_id="bound-manifest",
            season="2025-26",
            cutoff=cutoff,
            status="queued",
            attempts=0,
            created_at=cutoff,
            updated_at=cutoff,
            generation=1,
            claimed_generation=None,
        ))

    expected = frozenset(game.game_id for game in games)
    expected_l15 = {
        team_id: frozenset(
            game.game_id
            for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }

    class Governance:
        def read_for_composition(self, season, governed_cutoff, manifest_id=None):
            return LedgerGovernance(
                season,
                governed_cutoff,
                expected,
                frozenset(range(1, 31)),
                expected_l15,
                event_catalog_publication_id="event-catalog",
                event_catalog_checksum="c" * 64,
            )

    class Publication:
        def session(self):
            return sessionmaker(bind=engine, expire_on_commit=False)()

        def compose_from_observations(self, *args, **kwargs):
            return object()

    class Matchup:
        def __init__(self):
            self.as_of = None

        def refresh_publication_surfaces(self, *args, **kwargs):
            self.as_of = kwargs["as_of"]

    matchup = Matchup()
    listed_through = []
    list_games = repository.list_games

    def capture_list_games(season, *, through=None, connection=None):
        listed_through.append(through)
        return list_games(season, through=through, connection=connection)

    repository.list_games = capture_list_games
    runtime = LedgerRuntime(
        backfill=None,
        repository=repository,
        materialization=None,
        governance=Governance(),
        matchup_materialization=matchup,
        publication_service=Publication(),
        clock=lambda: cutoff + timedelta(hours=1),
    )

    assert runtime.compose_queued("2025-26") == 1
    assert listed_through == [slate_date]
    assert matchup.as_of == slate_date


def test_compose_queued_player_only_retry_does_not_issue_matchup_authority(
    tmp_path,
):
    from app.services.ledger_matchup_materialization import (
        LedgerMatchupMaterializationService,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'player-only-retry.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    games = _league_games()
    repository.replace_games_atomic(games)
    cutoff = datetime(2025, 10, 15, 5, 22, tzinfo=timezone.utc)
    team_ids = frozenset(range(1, 31))
    expected = frozenset(game.game_id for game in games)
    expected_l15 = {
        team_id: frozenset(
            game.game_id
            for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in team_ids
    }
    with engine.begin() as connection:
        connection.execute(CompositionJob.__table__.insert().values(
            job_id="player-only",
            stream_key="player_game_logs",
            manifest_id=None,
            season="2025-26",
            cutoff=cutoff,
            status="queued",
            attempts=0,
            created_at=cutoff,
            updated_at=cutoff,
        ))

    class Governance:
        def read_for_composition(self, season, governed_cutoff, manifest_id=None):
            return LedgerGovernance(
                season, governed_cutoff, expected, team_ids, expected_l15
            )

    class Parity:
        def read(self, stream_key):
            return ()

    matchup_repository = TeamMatchupRepository(engine)
    runtime = LedgerRuntime(
        backfill=None,
        repository=repository,
        materialization=LedgerMaterializationService(
            repository,
            parity_repository=LedgerParityArtifactRepository(engine),
            parity_reader=Parity(),
        ),
        governance=Governance(),
        matchup_materialization=LedgerMatchupMaterializationService(
            repository,
            matchup_repository,
            clock=lambda: cutoff + timedelta(hours=1),
        ),
        clock=lambda: cutoff + timedelta(hours=1),
    )

    assert runtime.compose_queued("2025-26") == 1
    with engine.connect() as connection:
        job = connection.execute(select(CompositionJob)).mappings().one()
    assert job["status"] == "succeeded"
    assert matchup_repository.get_snapshot(
        TeamMatchupSnapshotScope("2025-26", cutoff.date())
    ).observations == ()
    assert matchup_repository._issued_authorities == {}


def test_compose_queued_persists_incomplete_governed_l15_as_missing(tmp_path):
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
    events = _catalog_events(governed, cutoff)
    with engine.begin() as connection:
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=cutoff, activated_at=cutoff, activated_by="test",
        ))
        connection.execute(
            CatalogPublication.__table__.insert().values(
                **_immutable_event_catalog(events, cutoff)
            )
        )
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="manifest", season="2025-26", cutoff=cutoff,
            collect_before=cutoff + timedelta(hours=1), accepted_versions="[1]",
            scopes="[\"canonical_game_ledger\"]", checksum="manifest",
            **_manifest_catalog_binding(events, cutoff),
            status="active", created_at=cutoff,
        ))
        connection.execute(EventCatalogEntry.__table__.insert(), events)
        connection.execute(CompositionJob.__table__.insert().values(
            job_id="matchup", stream_key="traditional_opponent_l15",
            manifest_id="manifest", season="2025-26", cutoff=cutoff, status="queued",
            attempts=0, created_at=cutoff, updated_at=cutoff,
        ))

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
        ("traditional", "missing", "insufficient_governed_games"),
    }
    assert len(captured["expected_l15_game_ids"]) == 30
    assert {
        len(game_ids)
        for game_ids in captured["expected_l15_game_ids"].values()
    } == {14, 15}


def test_compose_queued_with_incomplete_governed_roster_persists_missing(tmp_path):
    from app.services.ledger_matchup_materialization import (
        LedgerMatchupMaterializationService,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'roster-compose.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    games = _league_games()[:2]
    repository.replace_games_atomic(games)
    cutoff = datetime(2025, 10, 15, 5, 22, tzinfo=timezone.utc)
    events = _catalog_events(games, cutoff)
    with engine.begin() as connection:
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=cutoff, activated_at=cutoff, activated_by="test",
        ))
        connection.execute(
            CatalogPublication.__table__.insert().values(
                **_immutable_event_catalog(events, cutoff)
            )
        )
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="manifest", season="2025-26", cutoff=cutoff,
            collect_before=cutoff + timedelta(hours=1), accepted_versions="[1]",
            scopes="[\"canonical_game_ledger\"]", checksum="manifest",
            **_manifest_catalog_binding(events, cutoff),
            status="active", created_at=cutoff,
        ))
        connection.execute(EventCatalogEntry.__table__.insert(), events)
        connection.execute(CompositionJob.__table__.insert(), [{
            "job_id": f"job-{stream}", "stream_key": stream, "manifest_id": "manifest",
            "season": "2025-26", "cutoff": cutoff, "status": "queued",
            "attempts": 0, "created_at": cutoff, "updated_at": cutoff,
        } for stream in LedgerCorrectionQueue.STREAMS])

    governance = ActiveManifestLedgerGovernanceReader(
        engine, clock=lambda: cutoff + timedelta(hours=1)
    ).read("2025-26", cutoff)
    assert len(governance.team_ids) < 30

    class Parity:
        def read(self, stream_key):
            return ()

    runtime = LedgerRuntime(
        backfill=None,
        repository=repository,
        materialization=LedgerMaterializationService(
            repository,
            parity_repository=LedgerParityArtifactRepository(engine),
            parity_reader=Parity(),
        ),
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

    assert runtime.compose_queued("2025-26") == 0

    for window_games in (None, 15):
        snapshot = TeamMatchupRepository(engine).get_snapshot(
            TeamMatchupSnapshotScope("2025-26", cutoff.date(), window_games)
        )
        assert snapshot.facts == ()
        assert {
            (item.surface, item.status, item.unavailable_reason)
            for item in snapshot.observations
        } == {
            ("assist_locations", "missing", "governed_team_roster_incomplete"),
            ("traditional", "missing", "governed_team_roster_incomplete"),
        }
    with engine.connect() as connection:
        jobs = {
            row["stream_key"]: row
            for row in connection.execute(select(CompositionJob.__table__)).mappings()
        }
    assert all(job["status"] == "failed" for job in jobs.values())
    assert {
        job["last_error"] for job in jobs.values()
    } == {"governed_team_roster_incomplete"}


def test_compose_queued_with_incomplete_governed_l15_persists_missing(tmp_path):
    from app.services.ledger_matchup_materialization import (
        LedgerMatchupMaterializationService,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'l15-compose.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    games = _league_games()[:150]
    repository.replace_games_atomic(games)
    cutoff = datetime(2025, 10, 15, 5, 22, tzinfo=timezone.utc)
    events = _catalog_events(games, cutoff)
    with engine.begin() as connection:
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=cutoff, activated_at=cutoff, activated_by="test",
        ))
        connection.execute(
            CatalogPublication.__table__.insert().values(
                **_immutable_event_catalog(events, cutoff)
            )
        )
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="manifest", season="2025-26", cutoff=cutoff,
            collect_before=cutoff + timedelta(hours=1), accepted_versions="[1]",
            scopes="[\"canonical_game_ledger\"]", checksum="manifest",
            **_manifest_catalog_binding(events, cutoff),
            status="active", created_at=cutoff,
        ))
        connection.execute(EventCatalogEntry.__table__.insert(), events)
        connection.execute(CompositionJob.__table__.insert(), [{
            "job_id": f"job-{stream}", "stream_key": stream, "manifest_id": "manifest",
            "season": "2025-26", "cutoff": cutoff, "status": "queued",
            "attempts": 0, "created_at": cutoff, "updated_at": cutoff,
        } for stream in LedgerCorrectionQueue.STREAMS])

    class Parity:
        def read(self, stream_key):
            return ()

    runtime = LedgerRuntime(
        backfill=None,
        repository=repository,
        materialization=LedgerMaterializationService(
            repository,
            parity_repository=LedgerParityArtifactRepository(engine),
            parity_reader=Parity(),
        ),
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

    assert runtime.compose_queued("2025-26") == 4

    season = TeamMatchupRepository(engine).get_snapshot(
        TeamMatchupSnapshotScope("2025-26", cutoff.date())
    )
    assert {item.surface for item in season.observations} == {
        "traditional",
        "assist_locations",
    }
    assert all(item.status == "available" for item in season.observations)
    l15 = TeamMatchupRepository(engine).get_snapshot(
        TeamMatchupSnapshotScope("2025-26", cutoff.date(), 15)
    )
    assert l15.facts == ()
    assert {
        (item.surface, item.status, item.unavailable_reason)
        for item in l15.observations
    } == {
        ("assist_locations", "missing", "insufficient_governed_games"),
        ("traditional", "missing", "insufficient_governed_games"),
    }
    with engine.connect() as connection:
        jobs = {
            row["stream_key"]: row
            for row in connection.execute(select(CompositionJob.__table__)).mappings()
        }
    assert jobs["player_game_logs"]["status"] == "succeeded"
    assert jobs["traditional_opponent_season"]["status"] == "succeeded"
    assert jobs["player_per36"]["status"] == "succeeded"
    assert jobs["assist_locations_season"]["status"] == "succeeded"
    assert jobs["traditional_opponent_l15"]["status"] == "failed"
    assert jobs["traditional_opponent_l15"]["last_error"] == "insufficient_governed_games"
    assert jobs["assist_locations_l15"]["status"] == "failed"
    assert jobs["assist_locations_l15"]["last_error"] == "insufficient_governed_games"


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
    events = [{
        "nba_game_id": "game-1", "season": "2025-26",
        "home_team_id": 1, "home_team_name": "Team 1",
        "home_team_tricode": "T01",
        "away_team_id": 2, "away_team_name": "Team 2",
        "away_team_tricode": "T02",
        "scheduled_at": now - timedelta(days=1), "status_text": "Final",
        "status_code": 3, "classification": "Regular Season",
        "first_seen_at": now - timedelta(days=1), "last_seen_at": now,
    }]
    with engine.begin() as connection:
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=now, activated_at=now, activated_by="test",
        ))
        connection.execute(
            CatalogPublication.__table__.insert().values(
                **_immutable_event_catalog(events, now)
            )
        )
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="manifest", season="2025-26", cutoff=now,
            collect_before=now + timedelta(hours=1), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum="manifest",
            **_manifest_catalog_binding(events, now),
            status="active", created_at=now,
        ))
        connection.execute(EventCatalogEntry.__table__.insert(), events)

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
