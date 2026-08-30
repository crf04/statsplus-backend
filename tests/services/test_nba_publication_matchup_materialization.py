"""NBA-owned team matchup publications compose into the persisted read model."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from statistics import fmean, pstdev
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError

from app.migrations import run_migrations
from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from app.services.canonical_game_ledger import raw_rows_from_facts
from app.services.database_first_activation import (
    DatabaseFirstActivationService,
    DatabaseFirstPublicationReader,
    PublicationRead,
    PublicationReadSnapshot,
    PublicationTeamWindowRow,
    LegacyWriteFence,
)
from app.services.database_first_rehearsal import HistoricalRehearsalRunner
from app.services.collection_control import PublicationService
from app.services.collection_control import CollectionOperationsService, ControlPlaneError
from app.services.collection_control import STREAM_REQUIRED_SLICES
from app.domain.team_matchup_taxonomy import (
    SHOT_TYPE_DISPLAY_TO_STORED,
    SHOT_ZONE_SLICES,
)
from app.models.collection_control import (
    ActiveSeason,
    CatalogPublication,
    CollectionManifest,
    PublicationPointer,
    PublicationVersion,
)
from app.models.event_catalog import EventCatalogEntry
from app.services.ledger_matchup_materialization import (
    LedgerMatchupMaterializationService,
)
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.team_matchup_query import TeamMatchupQueryService
from app.services.team_matchup_repository import (
    create_publication_write_capability,
    TeamMatchupFact,
    TeamMatchupRepository,
    TeamMatchupObservation,
    TeamMatchupSnapshotScope,
)
from app.services.team_matchup_publications import (
    NBA_PUBLICATION_STREAMS,
    NBA_PUBLICATION_TAXONOMY,
    PublicationLineage,
    PublicationGovernanceUnavailable,
    PublicationValidationError,
    publication_metric_identity,
    publication_cutoff_reason,
    resolve_governed_team_game_ids,
    validate_publication_rows,
)
from tests.services.test_ledger_derivations import _league_games
from tests.services.test_ledger_matchup_materialization import _governance


AS_OF = date(2025, 10, 15)
RETRIEVED_AT = datetime(2025, 10, 16, 10, tzinfo=timezone.utc)
CANONICAL_TEAM_IDS = tuple(sorted(NBA_TEAM_ID_TO_TRICODE))
CANONICAL_BY_FIXTURE_ID = dict(zip(range(1, 31), CANONICAL_TEAM_IDS))
DEFAULT_MANIFEST_ID = "nba-publication-manifest"
DEFAULT_EVENT_CATALOG_ID = "nba-publication-event-catalog"


def _canonical_league_games():
    """Use the reviewed NBA identity map for publication tests."""

    canonical = []
    for game in _league_games():
        team_facts = tuple(
            replace(
                fact,
                team_id=CANONICAL_BY_FIXTURE_ID[fact.team_id],
                team_tricode=NBA_TEAM_ID_TO_TRICODE[
                    CANONICAL_BY_FIXTURE_ID[fact.team_id]
                ],
                opponent_team_id=CANONICAL_BY_FIXTURE_ID[fact.opponent_team_id],
                opponent_team_tricode=NBA_TEAM_ID_TO_TRICODE[
                    CANONICAL_BY_FIXTURE_ID[fact.opponent_team_id]
                ],
            )
            for fact in game.team_facts
        )
        player_facts = tuple(
            replace(
                fact,
                team_id=CANONICAL_BY_FIXTURE_ID[fact.team_id],
                team_tricode=NBA_TEAM_ID_TO_TRICODE[
                    CANONICAL_BY_FIXTURE_ID[fact.team_id]
                ],
            )
            for fact in game.player_facts
        )
        transformed = replace(
            game,
            home_team_id=CANONICAL_BY_FIXTURE_ID[game.home_team_id],
            home_team_tricode=NBA_TEAM_ID_TO_TRICODE[
                CANONICAL_BY_FIXTURE_ID[game.home_team_id]
            ],
            away_team_id=CANONICAL_BY_FIXTURE_ID[game.away_team_id],
            away_team_tricode=NBA_TEAM_ID_TO_TRICODE[
                CANONICAL_BY_FIXTURE_ID[game.away_team_id]
            ],
            team_facts=team_facts,
            player_facts=player_facts,
            participant_ids_by_team=tuple(
                (
                    CANONICAL_BY_FIXTURE_ID[team_id],
                    player_ids,
                )
                for team_id, player_ids in game.participant_ids_by_team
            ),
            raw_rows=(),
        )
        canonical.append(
            replace(transformed, raw_rows=raw_rows_from_facts(transformed)).with_checksum()
        )
    return tuple(canonical)


def _season_game_ids_by_team():
    games = _canonical_league_games()
    return {
        team_id: tuple(
            game.game_id
            for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in CANONICAL_TEAM_IDS
    }


def _engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'nba-publications.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2025, 10, 15, tzinfo=timezone.utc)
    with engine.begin() as connection:
        _, governed_l15, _ = _governance(_canonical_league_games())
        governed_season = _season_game_ids_by_team()
        events = [{
            "nba_game_id": game.game_id,
            "home_team_id": game.home_team_id,
            "away_team_id": game.away_team_id,
            "phase": "Regular Season",
            "status": "Final",
            "status_code": 3,
            "scheduled_at": datetime.combine(
                game.game_date, datetime.min.time(), timezone.utc
            ).isoformat(),
        } for game in _canonical_league_games()]
        catalog_payload = json.dumps(
            {"events": events}, separators=(",", ":"), sort_keys=True
        )
        catalog_checksum = hashlib.sha256(catalog_payload.encode()).hexdigest()
        connection.execute(CatalogPublication.__table__.insert().values(
            publication_id=DEFAULT_EVENT_CATALOG_ID,
            season="2025-26",
            catalog_type="event",
            cutoff=cutoff,
            version="event-v1",
            checksum=catalog_checksum,
            payload=catalog_payload,
            complete=True,
            published_at=cutoff - timedelta(minutes=1),
            expires_at=None,
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id=DEFAULT_MANIFEST_ID,
            season="2025-26",
            cutoff=cutoff,
            collect_before=cutoff + timedelta(days=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="nba-publication-manifest-checksum",
            event_catalog_publication_id=DEFAULT_EVENT_CATALOG_ID,
            event_catalog_checksum=catalog_checksum,
            status="active",
            created_at=cutoff,
        ))
        for base, template in NBA_PUBLICATION_STREAMS.items():
            for window in ("season", "l15"):
                stream_key = template.format(window=window)
                metric_keys = tuple(sorted(NBA_PUBLICATION_TAXONOMY[base]))
                payload = {
                    "rows": [
                        {
                            "team_id": team_id,
                            "team_tricode": NBA_TEAM_ID_TO_TRICODE[team_id],
                            "game_ids": list(
                                governed_l15[team_id]
                                if window == "l15"
                                else governed_season[team_id]
                            ),
                            "game_count": (
                                len(governed_l15[team_id])
                                if window == "l15"
                                else len(governed_season[team_id])
                            ),
                            "per48": {key: float(team_id) for key in metric_keys},
                            "league_average": {
                                key: 15.5 for key in metric_keys
                            },
                            "population_sigma": {
                                key: 8.655 for key in metric_keys
                            },
                            "competition_rank": {
                                key: team_id for key in metric_keys
                            },
                        }
                        for team_id in CANONICAL_TEAM_IDS
                    ]
                }
                encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
                publication_id = f"publication-{stream_key}"
                connection.execute(PublicationVersion.__table__.insert().values(
                    publication_id=publication_id,
                    stream_key=stream_key,
                    season="2025-26",
                    cutoff=cutoff,
                    version=2,
                    status="active",
                    checksum=hashlib.sha256(encoded.encode()).hexdigest(),
                    payload=encoded,
                    manifest_id=DEFAULT_MANIFEST_ID,
                    event_catalog_publication_id=DEFAULT_EVENT_CATALOG_ID,
                    event_catalog_checksum=catalog_checksum,
                    created_at=cutoff,
                    fence=1,
                ))
                connection.execute(PublicationPointer.__table__.insert().values(
                    stream_key=stream_key,
                    active_publication_id=publication_id,
                    previous_publication_id=None,
                    fence=1,
                    updated_at=cutoff,
                ))
    return engine


def _publication_authority_values(
    *,
    manifest_id=DEFAULT_MANIFEST_ID,
    catalog_id=DEFAULT_EVENT_CATALOG_ID,
):
    events = [{
        "nba_game_id": game.game_id,
        "home_team_id": game.home_team_id,
        "away_team_id": game.away_team_id,
        "phase": "Regular Season",
        "status": "Final",
        "status_code": 3,
        "scheduled_at": datetime.combine(
            game.game_date, datetime.min.time(), timezone.utc
        ).isoformat(),
    } for game in _canonical_league_games()]
    checksum = hashlib.sha256(json.dumps(
        {"events": events}, separators=(",", ":"), sort_keys=True
    ).encode()).hexdigest()
    return {
        "manifest_id": manifest_id,
        "event_catalog_publication_id": catalog_id,
        "event_catalog_checksum": checksum,
    }


def _advance_test_publication(engine, stream_key, payload, publication_id):
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    checksum = hashlib.sha256(encoded.encode()).hexdigest()
    with engine.begin() as connection:
        pointer = connection.execute(
            PublicationPointer.__table__.select().where(
                PublicationPointer.stream_key == stream_key
            )
        ).mappings().one()
        prior_id = pointer["active_publication_id"]
        connection.execute(
            PublicationVersion.__table__.update().where(
                PublicationVersion.publication_id == prior_id
            ).values(status="superseded")
        )
        connection.execute(PublicationVersion.__table__.insert().values(
            **_publication_authority_values(),
            publication_id=publication_id,
            stream_key=stream_key,
            season="2025-26",
            cutoff=datetime(2025, 10, 15, tzinfo=timezone.utc),
            version=99,
            status="active",
            checksum=checksum,
            payload=encoded,
            created_at=RETRIEVED_AT,
            fence=pointer["fence"] + 1,
        ))
        connection.execute(
            PublicationPointer.__table__.update().where(
                PublicationPointer.stream_key == stream_key
            ).values(
                previous_publication_id=prior_id,
                active_publication_id=publication_id,
                fence=pointer["fence"] + 1,
            )
        )
    return SimpleNamespace(
        publication_id=publication_id, payload=encoded,
        version=99, checksum=checksum,
    )


def _insert_publication_authority(
    connection,
    *,
    cutoff,
    manifest_id,
    catalog_id,
    unplayed_games: int = 0,
):
    games = _canonical_league_games()
    unplayed = {
        game.game_id for game in games[len(games) - unplayed_games:]
    } if unplayed_games else frozenset()
    events = [{
        "nba_game_id": game.game_id,
        "home_team_id": game.home_team_id,
        "away_team_id": game.away_team_id,
        "phase": "Regular Season",
        "status": "Scheduled" if game.game_id in unplayed else "Final",
        "status_code": 1 if game.game_id in unplayed else 3,
        "scheduled_at": datetime.combine(
            game.game_date, datetime.min.time(), timezone.utc
        ).isoformat(),
    } for game in games]
    payload = json.dumps(
        {"events": events}, separators=(",", ":"), sort_keys=True
    )
    checksum = hashlib.sha256(payload.encode()).hexdigest()
    connection.execute(CatalogPublication.__table__.insert().values(
        publication_id=catalog_id,
        season="2025-26",
        catalog_type="event",
        cutoff=cutoff,
        version=f"event-{catalog_id}",
        checksum=checksum,
        payload=payload,
        complete=True,
        published_at=cutoff - timedelta(minutes=1),
        expires_at=None,
    ))
    connection.execute(CollectionManifest.__table__.insert().values(
        manifest_id=manifest_id,
        season="2025-26",
        cutoff=cutoff,
        collect_before=cutoff + timedelta(days=1),
        accepted_versions="[1]",
        scopes='["canonical_game_ledger"]',
        checksum=f"manifest-{manifest_id}",
        event_catalog_publication_id=catalog_id,
        event_catalog_checksum=checksum,
        status="superseded",
        created_at=cutoff,
    ))
    return {
        "manifest_id": manifest_id,
        "event_catalog_publication_id": catalog_id,
        "event_catalog_checksum": checksum,
    }


def _candidate_payload(expected_l15, *, mode="valid"):
    rows = []
    for team_id in CANONICAL_TEAM_IDS:
        game_ids = list(expected_l15[team_id])
        row = {
            "team_id": team_id,
            "team_tricode": NBA_TEAM_ID_TO_TRICODE[team_id],
            "game_ids": game_ids,
            "game_count": len(game_ids),
            "per48": {
                key: 1.0 for key in sorted(NBA_PUBLICATION_TAXONOMY["shot_zones"])
            },
            "league_average": {
                key: 1.0 for key in sorted(NBA_PUBLICATION_TAXONOMY["shot_zones"])
            },
            "population_sigma": {
                key: 1.0 for key in sorted(NBA_PUBLICATION_TAXONOMY["shot_zones"])
            },
            "competition_rank": {
                key: 1 for key in sorted(NBA_PUBLICATION_TAXONOMY["shot_zones"])
            },
        }
        rows.append(row)
    if mode == "one_game_one_team":
        rows = rows[:1]
    elif mode == "wrong_ids":
        rows[0]["team_id"] = 999
    elif mode == "duplicate_ids":
        rows.append(dict(rows[0]))
    elif mode == "missing_metric":
        rows[0]["per48"].pop(next(iter(rows[0]["per48"])))
    elif mode == "wrong_game_set":
        rows[0]["game_ids"] = [f"wrong-{index}" for index in range(15)]
    elif mode == "duplicate_game_ids":
        rows[0]["game_ids"][-1] = rows[0]["game_ids"][0]
    elif mode == "missing_game_id":
        rows[0]["game_ids"].pop()
        rows[0]["game_count"] -= 1
    elif mode == "extra_game_id":
        rows[0]["game_ids"].append("extra-game")
        rows[0]["game_count"] += 1
    return {"rows": rows}


def _per48_only_payload(expected_game_ids):
    payload = _candidate_payload(expected_game_ids)
    for row in payload["rows"]:
        row.pop("league_average")
        row.pop("population_sigma")
        row.pop("competition_rank")
    return payload


class _WindowGovernanceResolver:
    def __init__(self, *, season, l15):
        self.expected = {"season": season, "l15": l15}

    def resolve_team_game_ids(
        self,
        season,
        cutoff,
        *,
        window,
        manifest_id=None,
        event_catalog_publication_id=None,
        event_catalog_checksum=None,
    ):
        return self.expected[window]


def test_legacy_resolver_cannot_discard_requested_publication_authority():
    expected = {
        team_id: frozenset({f"game-{team_id}"})
        for team_id in CANONICAL_TEAM_IDS
    }
    class LegacyResolver:
        def resolve_team_game_ids(self, season, cutoff, *, window):
            return expected

    resolver = LegacyResolver()

    with pytest.raises(ValueError, match="publication_governance_unavailable"):
        resolve_governed_team_game_ids(
            resolver,
            "2025-26",
            datetime(2025, 10, 15, tzinfo=timezone.utc),
            window="season",
            manifest_id="invented-manifest",
            event_catalog_publication_id="invented-catalog",
            event_catalog_checksum="invented-checksum",
        )

    assert resolve_governed_team_game_ids(
        resolver,
        "2025-26",
        datetime(2025, 10, 15, tzinfo=timezone.utc),
        window="season",
    ) == expected


def test_collection_registry_and_publication_contract_share_canonical_taxonomy():
    assert STREAM_REQUIRED_SLICES["grouped_shot_types"] == frozenset(
        SHOT_TYPE_DISPLAY_TO_STORED
    )
    assert STREAM_REQUIRED_SLICES["exact_shot_zones"] == frozenset(SHOT_ZONE_SLICES)
    assert set(NBA_PUBLICATION_TAXONOMY["shot_types"]) == {
        f"{slice_key}_{stat_key}"
        for slice_key in SHOT_TYPE_DISPLAY_TO_STORED.values()
        for stat_key in ("FG2M", "FG2A", "FG3M", "FG3A")
    }


def _operator_activation_fixture(tmp_path, *, mode="valid"):
    engine = _engine(tmp_path)
    publications = PublicationService(engine)
    stream_key = "exact_shot_zones_opponent_l15"
    publications.register_stream(
        stream_key,
        provider="nba",
        owner="residential_collector",
        required_observations=(),
        publication_strategy="snapshot_replace",
        supported_windows=("l15",),
        enabled=False,
    )
    cutoff = datetime(2025, 10, 15, tzinfo=timezone.utc)
    expected = {
        team_id: frozenset(f"governed-{team_id}-{index}" for index in range(15))
        for team_id in CANONICAL_TEAM_IDS
    }
    publications.l15_expectation_resolver = _WindowGovernanceResolver(
        season=expected,
        l15=expected,
    )
    payload = _candidate_payload(expected, mode=mode)
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    candidate_id = f"candidate-{mode}"
    with engine.begin() as connection:
        connection.execute(PublicationVersion.__table__.insert().values(
            **_publication_authority_values(),
            publication_id=candidate_id,
            stream_key=stream_key,
            season="2025-26",
            cutoff=cutoff,
            version=1,
            status="candidate",
            checksum=hashlib.sha256(encoded.encode()).hexdigest(),
            payload=encoded,
            created_at=cutoff,
            fence=0,
        ))
    operations = CollectionOperationsService(
        engine,
        publication_service=publications,
        l15_expectation_resolver=_WindowGovernanceResolver(
            season=expected,
            l15=expected,
        ),
        clock=lambda: RETRIEVED_AT,
    )
    return engine, operations, stream_key, candidate_id, cutoff


@pytest.mark.parametrize(
    "mode",
    ("one_game_one_team", "wrong_ids", "duplicate_ids"),
)
def test_operator_l15_activation_rejects_noncanonical_or_incomplete_candidate(
    tmp_path, mode
):
    _, operations, stream_key, candidate_id, cutoff = _operator_activation_fixture(
        tmp_path, mode=mode
    )

    with pytest.raises(ControlPlaneError, match="publication_candidate_invalid"):
        operations.activate_stream(
            stream_key,
            actor="operator",
            reason="reject invalid candidate",
            season="2025-26",
            cutoff=cutoff,
            candidate_publication_id=candidate_id,
        )


def test_operator_l15_activation_accepts_exact_governed_unique_ids(tmp_path):
    engine, operations, stream_key, candidate_id, cutoff = _operator_activation_fixture(
        tmp_path
    )

    result = operations.activate_stream(
        stream_key,
        actor="operator",
        reason="activate reviewed candidate",
        season="2025-26",
        cutoff=cutoff,
        candidate_publication_id=candidate_id,
    )

    assert result.resource.enabled is True
    with engine.connect() as connection:
        active_id = connection.execute(
            PublicationVersion.__table__.select().where(
                PublicationVersion.publication_id == candidate_id
            )
        ).scalar_one_or_none()
    assert active_id is not None


def test_governed_activation_never_trusts_caller_expected_game_ids(tmp_path):
    engine, operations, stream_key, candidate_id, cutoff = (
        _operator_activation_fixture(tmp_path)
    )
    operations.publication_service.l15_expectation_resolver = None
    with engine.connect() as connection:
        payload = json.loads(connection.execute(
            select(PublicationVersion.payload).where(
                PublicationVersion.publication_id == candidate_id
            )
        ).scalar_one())
    self_asserted = {
        int(row["team_id"]): frozenset(row["game_ids"])
        for row in payload["rows"]
    }

    with pytest.raises(
        ControlPlaneError, match="publication_governance_unavailable"
    ):
        operations.publication_service.activate_stream(
            stream_key,
            actor="operator",
            reason="reject self asserted governance",
            season="2025-26",
            cutoff=cutoff,
            candidate_publication_id=candidate_id,
            expected_game_ids_by_team=self_asserted,
        )

    facade = DatabaseFirstActivationService(engine)
    with pytest.raises(
        ControlPlaneError, match="publication_governance_unavailable"
    ):
        facade.activate(
            stream_key,
            actor="operator",
            reason="reject missing production resolver",
            season="2025-26",
            cutoff=cutoff,
            candidate_publication_id=candidate_id,
            expected_game_ids_by_team=self_asserted,
        )


@pytest.mark.parametrize(
    "mode",
    ("duplicate_game_ids", "missing_game_id", "extra_game_id", "wrong_game_set"),
)
def test_season_activation_rejects_noncanonical_game_identity_and_keeps_pointer(
    tmp_path, mode
):
    engine = _engine(tmp_path)
    season_ids = {
        team_id: frozenset(game_ids)
        for team_id, game_ids in _season_game_ids_by_team().items()
    }
    _, l15_ids, _ = _governance(_canonical_league_games())
    publications = PublicationService(
        engine,
        l15_expectation_resolver=_WindowGovernanceResolver(
            season=season_ids, l15=l15_ids
        ),
    )
    stream_key = "exact_shot_zones_opponent_season"
    publications.register_stream(
        stream_key,
        provider="nba",
        owner="residential_collector",
        required_observations=(),
        publication_strategy="snapshot_replace",
        supported_windows=("season",),
        enabled=False,
    )
    cutoff = datetime(2025, 10, 15, tzinfo=timezone.utc)
    payload = _candidate_payload(season_ids, mode=mode)
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    candidate_id = f"season-candidate-{mode}"
    with engine.begin() as connection:
        before = connection.execute(
            PublicationPointer.__table__.select().where(
                PublicationPointer.stream_key == stream_key
            )
        ).mappings().one()
        connection.execute(PublicationVersion.__table__.insert().values(
            **_publication_authority_values(),
            publication_id=candidate_id,
            stream_key=stream_key,
            season="2025-26",
            cutoff=cutoff,
            version=3,
            status="candidate",
            checksum=hashlib.sha256(encoded.encode()).hexdigest(),
            payload=encoded,
            created_at=cutoff,
            fence=0,
        ))

    with pytest.raises(ControlPlaneError, match="publication_candidate_invalid"):
        publications.activate_stream(
            stream_key,
            actor="operator",
            reason="reject invalid season identity",
            season="2025-26",
            cutoff=cutoff,
            candidate_publication_id=candidate_id,
        )

    with engine.connect() as connection:
        after = connection.execute(
            PublicationPointer.__table__.select().where(
                PublicationPointer.stream_key == stream_key
            )
        ).mappings().one()
    assert after["active_publication_id"] == before["active_publication_id"]
    assert after["fence"] == before["fence"]


def test_activation_and_query_use_immutable_catalog_not_retrospective_live_final(
    tmp_path,
):
    engine = _engine(tmp_path)
    cutoff = datetime(2025, 10, 15, tzinfo=timezone.utc)
    games = _canonical_league_games()
    events = [
        {
            "nba_game_id": game.game_id,
            "season": "2025-26",
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
            "first_seen_at": cutoff,
            "last_seen_at": cutoff,
        }
        for game in games
    ]
    retrospective_id = "retrospective-final"
    retrospective = {
        "nba_game_id": retrospective_id,
        "season": "2025-26",
        "home_team_id": CANONICAL_TEAM_IDS[0],
        "home_team_name": "Retrospective Home",
        "home_team_tricode": NBA_TEAM_ID_TO_TRICODE[CANONICAL_TEAM_IDS[0]],
        "away_team_id": CANONICAL_TEAM_IDS[-1],
        "away_team_name": "Retrospective Away",
        "away_team_tricode": NBA_TEAM_ID_TO_TRICODE[CANONICAL_TEAM_IDS[-1]],
        "scheduled_at": cutoff - timedelta(days=1),
        "status_text": "Scheduled",
        "status_code": 1,
        "classification": "Regular Season",
        "first_seen_at": cutoff,
        "last_seen_at": cutoff,
    }
    events.append(retrospective)
    catalog_payload = {
        "events": [
            {
                "nba_game_id": event["nba_game_id"],
                "home_team_id": event["home_team_id"],
                "away_team_id": event["away_team_id"],
                "phase": event["classification"],
                "status": event["status_text"],
                "status_code": event["status_code"],
                "scheduled_at": event["scheduled_at"].isoformat(),
            }
            for event in events
        ]
    }
    encoded_catalog = json.dumps(
        catalog_payload, separators=(",", ":"), sort_keys=True
    )
    with engine.begin() as connection:
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26",
            phase="Regular Season",
            status="active",
            cutoff=cutoff,
            activated_at=cutoff,
            activated_by="test",
        ))
        connection.execute(CatalogPublication.__table__.insert().values(
            publication_id="immutable-event-catalog",
            season="2025-26",
            catalog_type="event",
            cutoff=cutoff,
            version="event-v1",
            checksum=hashlib.sha256(encoded_catalog.encode()).hexdigest(),
            payload=encoded_catalog,
            complete=True,
            published_at=cutoff - timedelta(minutes=1),
            expires_at=None,
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="immutable-manifest",
            season="2025-26",
            cutoff=cutoff,
            collect_before=cutoff + timedelta(hours=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="immutable-manifest",
            event_catalog_publication_id="immutable-event-catalog",
            event_catalog_checksum=hashlib.sha256(
                encoded_catalog.encode()
            ).hexdigest(),
            status="active",
            created_at=cutoff,
        ))
        connection.execute(EventCatalogEntry.__table__.insert(), events)
    resolver = ActiveManifestLedgerGovernanceReader(engine)
    before = resolver.resolve_team_game_ids("2025-26", cutoff, window="season")
    assert retrospective_id not in frozenset().union(*before.values())
    with engine.begin() as connection:
        connection.execute(
            EventCatalogEntry.__table__.update().where(
                EventCatalogEntry.nba_game_id == retrospective_id
            ).values(status_text="Final", status_code=3)
        )
    assert resolver.resolve_team_game_ids(
        "2025-26", cutoff, window="season"
    ) == before

    wrong = {team_id: tuple(game_ids) for team_id, game_ids in before.items()}
    wrong[CANONICAL_TEAM_IDS[0]] = (
        *wrong[CANONICAL_TEAM_IDS[0]], retrospective_id
    )
    stream_key = "exact_shot_zones_opponent_season"
    publications = PublicationService(
        engine, l15_expectation_resolver=resolver
    )
    publications.register_stream(
        stream_key,
        provider="nba",
        owner="residential_collector",
        required_observations=(),
        publication_strategy="snapshot_replace",
        supported_windows=("season",),
        enabled=False,
    )
    payload = _candidate_payload(wrong)
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    candidate_id = "retrospective-season-candidate"
    with engine.begin() as connection:
        pointer_before = connection.execute(
            PublicationPointer.__table__.select().where(
                PublicationPointer.stream_key == stream_key
            )
        ).mappings().one()
        connection.execute(PublicationVersion.__table__.insert().values(
            manifest_id="immutable-manifest",
            event_catalog_publication_id="immutable-event-catalog",
            event_catalog_checksum=hashlib.sha256(
                encoded_catalog.encode()
            ).hexdigest(),
            publication_id=candidate_id,
            stream_key=stream_key,
            season="2025-26",
            cutoff=cutoff,
            version=3,
            status="candidate",
            checksum=hashlib.sha256(encoded.encode()).hexdigest(),
            payload=encoded,
            created_at=cutoff,
            fence=0,
        ))
    with pytest.raises(ControlPlaneError, match="publication_candidate_invalid"):
        publications.activate_stream(
            stream_key,
            actor="operator",
            reason="reject retrospective final",
            season="2025-26",
            cutoff=cutoff,
            candidate_publication_id=candidate_id,
        )
    with engine.connect() as connection:
        pointer_after = connection.execute(
            PublicationPointer.__table__.select().where(
                PublicationPointer.stream_key == stream_key
            )
        ).mappings().one()
    assert pointer_after["active_publication_id"] == pointer_before[
        "active_publication_id"
    ]

    query = TeamMatchupQueryService(
        TeamMatchupRepository(engine),
        publication_reader=_reader(
            game_ids_by_stream={stream_key: wrong},
        ),
        l15_expectation_resolver=resolver,
    ).get_window(TeamMatchupSnapshotScope("2025-26", AS_OF))
    observation = next(
        item for item in query.observations if item.surface == "shot_zones"
    )
    assert observation.status == "unavailable"
    assert observation.unavailable_reason == "publication_game_set_mismatch"


def test_same_cutoff_republication_cannot_reinterpret_bound_candidate(tmp_path):
    engine = _engine(tmp_path)
    cutoff = datetime(2025, 10, 15, tzinfo=timezone.utc)
    resolver = ActiveManifestLedgerGovernanceReader(engine)
    expected_a = resolver.resolve_team_game_ids(
        "2025-26",
        cutoff,
        window="season",
        manifest_id=DEFAULT_MANIFEST_ID,
    )
    stream_key = "exact_shot_zones_opponent_season"
    candidate_payload = _candidate_payload(expected_a)
    encoded_candidate = json.dumps(
        candidate_payload, separators=(",", ":"), sort_keys=True
    )

    with engine.begin() as connection:
        catalog_a = connection.execute(
            CatalogPublication.__table__.select().where(
                CatalogPublication.publication_id == DEFAULT_EVENT_CATALOG_ID
            )
        ).mappings().one()
        document_b = json.loads(catalog_a["payload"])
        excluded_game_id = document_b["events"][0]["nba_game_id"]
        document_b["events"][0]["status"] = "Scheduled"
        document_b["events"][0]["status_code"] = 1
        encoded_b = json.dumps(document_b, separators=(",", ":"), sort_keys=True)
        checksum_b = hashlib.sha256(encoded_b.encode()).hexdigest()
        connection.execute(CatalogPublication.__table__.insert().values(
            publication_id="same-cutoff-catalog-b",
            season="2025-26",
            catalog_type="event",
            cutoff=cutoff,
            version="event-v2",
            checksum=checksum_b,
            payload=encoded_b,
            complete=True,
            published_at=cutoff + timedelta(microseconds=1),
            expires_at=None,
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="same-cutoff-manifest-b",
            season="2025-26",
            cutoff=cutoff,
            collect_before=cutoff + timedelta(days=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="same-cutoff-manifest-b",
            event_catalog_publication_id="same-cutoff-catalog-b",
            event_catalog_checksum=checksum_b,
            status="active",
            created_at=cutoff + timedelta(microseconds=1),
        ))
        connection.execute(PublicationVersion.__table__.insert().values(
            **_publication_authority_values(),
            publication_id="same-cutoff-candidate-a",
            stream_key=stream_key,
            season="2025-26",
            cutoff=cutoff,
            version=3,
            status="candidate",
            checksum=hashlib.sha256(encoded_candidate.encode()).hexdigest(),
            payload=encoded_candidate,
            created_at=cutoff + timedelta(minutes=1),
            fence=0,
        ))

    expected_b = resolver.resolve_team_game_ids(
        "2025-26",
        cutoff,
        window="season",
        manifest_id="same-cutoff-manifest-b",
    )
    assert excluded_game_id in frozenset().union(*expected_a.values())
    assert excluded_game_id not in frozenset().union(*expected_b.values())

    publications = PublicationService(
        engine, l15_expectation_resolver=resolver
    )
    publications.register_stream(
        stream_key,
        provider="nba",
        owner="residential_collector",
        required_observations=(),
        publication_strategy="snapshot_replace",
        supported_windows=("season",),
        enabled=False,
    )
    publications.activate_stream(
        stream_key,
        actor="operator",
        reason="activate exact authority A",
        season="2025-26",
        cutoff=cutoff,
        candidate_publication_id="same-cutoff-candidate-a",
        require_candidate=True,
    )
    read = DatabaseFirstPublicationReader(engine).snapshot(
        (stream_key,), season="2025-26"
    ).reads[stream_key]
    assert read.available
    assert read.publication_id == "same-cutoff-candidate-a"
    assert read.manifest_id == DEFAULT_MANIFEST_ID

    query = TeamMatchupQueryService(
        TeamMatchupRepository(engine),
        publication_reader=DatabaseFirstPublicationReader(engine),
        l15_expectation_resolver=resolver,
    ).get_window(TeamMatchupSnapshotScope("2025-26", AS_OF))
    observation = next(
        item for item in query.observations if item.surface == "shot_zones"
    )
    assert observation.status == "available"
    assert observation.publication is not None
    assert observation.publication.publication_id == "same-cutoff-candidate-a"


def _rows(
    metric_key: str | tuple[str, ...],
    game_ids_by_team: dict[int, tuple[str, ...]] | None = None,
) -> tuple[PublicationTeamWindowRow, ...]:
    metric_keys = (metric_key,) if isinstance(metric_key, str) else metric_key
    return tuple(
        PublicationTeamWindowRow(
            team_id=team_id,
            team_tricode=NBA_TEAM_ID_TO_TRICODE[team_id],
            game_ids=(game_ids_by_team or {}).get(team_id, (f"game-{team_id}",)),
            game_count=len((game_ids_by_team or {}).get(team_id, (f"game-{team_id}",))),
            per48={
                key: float(team_id)
                for key in (metric_keys if team_id % 2 else reversed(metric_keys))
            },
            league_average={key: 15.5 for key in metric_keys},
            population_sigma={key: 8.655 for key in metric_keys},
            competition_rank={key: team_id for key in metric_keys},
        )
        for team_id in CANONICAL_TEAM_IDS
    )


def _reader(
    *,
    unavailable: frozenset[str] = frozenset(),
    metric_keys_by_stream: dict[str, str | tuple[str, ...]] | None = None,
    cutoff_by_stream: dict[str, str] | None = None,
    freshness_by_stream: dict[str, str] | None = None,
    game_ids_by_stream: dict[str, dict[int, tuple[str, ...]]] | None = None,
    contradict_statistics: bool = False,
    unavailable_lineage_by_stream: dict[str, tuple[str, str, str]] | None = None,
    governance_available: bool = True,
):
    governed_season = _season_game_ids_by_team()
    stream_bases = {
        template.format(window=window): base
        for base, template in NBA_PUBLICATION_STREAMS.items()
        for window in ("season", "l15")
    }
    reads = {}
    for stream_key, base in stream_bases.items():
        if stream_key in unavailable:
            unavailable_lineage = (unavailable_lineage_by_stream or {}).get(
                stream_key
            )
            reads[stream_key] = PublicationRead(
                stream_key=stream_key,
                publication_id=(
                    unavailable_lineage[0] if unavailable_lineage else None
                ),
                season=(unavailable_lineage[1] if unavailable_lineage else "2025-26"),
                cutoff=(unavailable_lineage[2] if unavailable_lineage else None),
                version=2 if unavailable_lineage else None,
                status="unavailable",
                freshness="unavailable",
                age_seconds=None,
                payload=None,
                retrieved_at=RETRIEVED_AT,
                legacy_fallback_allowed=True,
                unavailable_reason=(
                    "provider_window_unsupported"
                    if stream_key.endswith("_l15")
                    and stream_key.startswith("synergy_")
                    else "publication_missing"
                ),
            )
            continue
        decoded = _rows(
            (metric_keys_by_stream or {}).get(
                stream_key, tuple(sorted(NBA_PUBLICATION_TAXONOMY[base]))
            ),
            (game_ids_by_stream or {}).get(
                stream_key,
                governed_season if stream_key.endswith("_season") else None,
            ),
        )
        if contradict_statistics:
            decoded = tuple(
                replace(
                    row,
                    league_average={key: 999.0 for key in row.per48},
                    population_sigma={key: 999.0 for key in row.per48},
                    competition_rank={key: 99 for key in row.per48},
                )
                for row in decoded
            )
        reads[stream_key] = PublicationRead(
            stream_key=stream_key,
            publication_id=f"publication-{stream_key}",
            season="2025-26",
            cutoff=(cutoff_by_stream or {}).get(
                stream_key, "2025-10-15T00:00:00+00:00"
            ),
            version=2,
            status="active",
            freshness=(freshness_by_stream or {}).get(stream_key, "stale"),
            age_seconds=90000,
            payload={"rows": []},
            retrieved_at=RETRIEVED_AT,
            decoded=decoded,
        )
    for stream_key in (
        "traditional_opponent_season",
        "assist_locations_season",
        "traditional_opponent_l15",
        "assist_locations_l15",
    ):
        reads[stream_key] = PublicationRead(
            stream_key=stream_key,
            publication_id=None,
            season="2025-26",
            cutoff=None,
            version=None,
            status="inactive",
            freshness="legacy_fallback",
            age_seconds=None,
            payload=None,
            legacy_fallback_allowed=True,
        )

    class Reader:
        def resolve_team_game_ids(self, season, cutoff, *, window):
            if not governance_available:
                raise PublicationGovernanceUnavailable()
            cutoff_text = cutoff.isoformat()
            for stream_key, read in reads.items():
                if (
                    stream_key.endswith(f"_{window}")
                    and read.season == season
                    and read.cutoff == cutoff_text
                    and read.decoded
                ):
                    return {
                        row.team_id: frozenset(row.game_ids)
                        for row in read.decoded
                    }
            raise PublicationGovernanceUnavailable()

        def snapshot(self, stream_keys, *, season=None, require_active=True):
            selected = {key: reads[key] for key in stream_keys}
            return PublicationReadSnapshot(
                season=season,
                reads=selected,
                generation=tuple(
                    (key, read.publication_id, read.fence, read.version)
                    for key, read in sorted(selected.items())
                ),
            )

        def read_many(self, stream_keys, *, season=None, require_active=True):
            return self.snapshot(
                stream_keys, season=season, require_active=require_active
            ).reads

    return Reader()


def test_nba_publications_persist_independent_facts_with_lineage(tmp_path):
    engine = _engine(tmp_path)
    games = _canonical_league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    repository.replace_snapshots(
        (
            (
                TeamMatchupSnapshotScope("2025-26", AS_OF),
                [
                    TeamMatchupFact(
                        team_id=team_id,
                        base="shot_zones",
                        slice_key="Restricted Area",
                        stat_key="FGM",
                        raw_value=77,
                        denominator_value=48,
                        denominator_unit="minutes",
                        provider="pbp_stats",
                    )
                    for team_id in CANONICAL_TEAM_IDS
                ],
                [TeamMatchupObservation("shot_zones", "available")],
            ),
        ),
        retrieved_at=RETRIEVED_AT,
    )
    service = LedgerMatchupMaterializationService(
        ledger,
        repository,
        publication_reader=_reader(),
        clock=lambda: RETRIEVED_AT,
    )
    expected_game_ids, expected_l15_game_ids, team_ids = _governance(games)

    service.materialize(
        "2025-26",
        as_of=AS_OF,
        expected_game_ids=expected_game_ids,
        expected_l15_game_ids=expected_l15_game_ids,
        team_ids=team_ids,
    )

    snapshot = repository.get_snapshot(TeamMatchupSnapshotScope("2025-26", AS_OF))
    fact = next(item for item in snapshot.facts if item.base == "shot_zones")
    assert fact.provider == "nba_publication"
    assert fact.publication.publication_id == "publication-exact_shot_zones_opponent_season"
    assert fact.publication.cutoff == "2025-10-15T00:00:00+00:00"
    assert fact.publication.freshness == "stale"
    assert fact.game_ids == _season_game_ids_by_team()[CANONICAL_TEAM_IDS[0]]
    shot_type = next(item for item in snapshot.facts if item.base == "shot_types")
    assert (shot_type.slice_key, shot_type.stat_key) == (
        "catch_and_shoot",
        "FG2A",
    )
    observations = {item.surface: item for item in snapshot.observations}
    assert observations["shot_zones"].status == "available"
    assert observations["shot_zones"].publication == fact.publication
    assert observations["traditional"].status == "available"

    window = TeamMatchupQueryService(repository).get_window(snapshot.scope)
    assert any(metric.base == "shot_zones" for metric in window.league_metrics)
    assert any(
        (metric.base, metric.slice_key, metric.stat_key)
        == ("shot_types", "catch_and_shoot", "FG2A")
        for metric in window.league_metrics
    )

    publication_window = TeamMatchupQueryService(
        repository,
        publication_reader=_reader(),
    ).get_window(snapshot.scope)
    assert any(
        (metric.base, metric.slice_key, metric.stat_key)
        == ("shot_types", "catch_and_shoot", "FG2A")
        for metric in publication_window.league_metrics
    )


def test_publication_per48_values_and_metric_key_order_are_preserved(tmp_path):
    engine = _engine(tmp_path)
    games = _canonical_league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    service = LedgerMatchupMaterializationService(
        ledger,
        repository,
        publication_reader=_reader(
            metric_keys_by_stream={
                "grouped_shot_types_opponent_season": tuple(
                    reversed(sorted(NBA_PUBLICATION_TAXONOMY["shot_types"]))
                ),
            }
        ),
        clock=lambda: RETRIEVED_AT,
    )
    expected_game_ids, expected_l15_game_ids, team_ids = _governance(games)
    service.materialize(
        "2025-26",
        as_of=AS_OF,
        expected_game_ids=expected_game_ids,
        expected_l15_game_ids=expected_l15_game_ids,
        team_ids=team_ids,
    )

    snapshot = repository.get_snapshot(TeamMatchupSnapshotScope("2025-26", AS_OF))
    fact = next(
        item
        for item in snapshot.facts
        if item.base == "shot_types" and item.stat_key == "FG2A"
    )
    assert fact.raw_value == float(CANONICAL_TEAM_IDS[0])
    assert fact.denominator_value == 48.0
    window = TeamMatchupQueryService(repository).get_window(snapshot.scope)
    metric = next(
        item
        for item in window.team_metrics[CANONICAL_TEAM_IDS[0]]
        if item.base == "shot_types" and item.stat_key == "FG2A"
    )
    assert metric.allowed_per_48 == float(CANONICAL_TEAM_IDS[0])
    assert {
        item.stat_key for item in window.league_metrics if item.base == "shot_types"
    } == {"FG2A", "FG2M", "FG3A", "FG3M"}


def test_l15_publication_requires_each_team_exact_governed_game_set(tmp_path):
    engine = _engine(tmp_path)
    games = _canonical_league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    expected_game_ids, expected_l15_game_ids, team_ids = _governance(games)
    l15_game_ids = {
        team_id: tuple(sorted(game_ids))
        for team_id, game_ids in expected_l15_game_ids.items()
    }
    reader = _reader(
        game_ids_by_stream={
            stream_key: l15_game_ids
            for stream_key in (
                "synergy_play_types_opponent_l15",
                "grouped_shot_types_opponent_l15",
                "exact_shot_zones_opponent_l15",
            )
        }
    )
    service = LedgerMatchupMaterializationService(
        ledger, repository, publication_reader=reader, clock=lambda: RETRIEVED_AT
    )
    service.materialize(
        "2025-26",
        as_of=AS_OF,
        expected_game_ids=expected_game_ids,
        expected_l15_game_ids=expected_l15_game_ids,
        team_ids=team_ids,
    )
    snapshot = repository.get_snapshot(
        TeamMatchupSnapshotScope("2025-26", AS_OF, 15)
    )
    shot_zones = next(item for item in snapshot.observations if item.surface == "shot_zones")
    assert shot_zones.status == "available"
    assert any(fact.base == "shot_zones" for fact in snapshot.facts)

    wrong_game_ids = dict(l15_game_ids)
    wrong_game_ids[CANONICAL_TEAM_IDS[0]] = ("wrong-game",)
    wrong_reader = _reader(
        game_ids_by_stream={
            stream_key: wrong_game_ids
            for stream_key in (
                "synergy_play_types_opponent_l15",
                "grouped_shot_types_opponent_l15",
                "exact_shot_zones_opponent_l15",
            )
        }
    )
    wrong_path = tmp_path / "wrong"
    wrong_path.mkdir()
    wrong_engine = _engine(wrong_path)
    wrong_ledger = CanonicalGameLedgerRepository(wrong_engine)
    wrong_ledger.replace_games_atomic(games)
    wrong_repository = TeamMatchupRepository(
        wrong_engine,
        publication_write_capability=create_publication_write_capability(wrong_engine),
    )
    wrong_service = LedgerMatchupMaterializationService(
        wrong_ledger,
        wrong_repository,
        publication_reader=wrong_reader,
        clock=lambda: RETRIEVED_AT,
    )
    wrong_service.materialize(
        "2025-26",
        as_of=AS_OF,
        expected_game_ids=expected_game_ids,
        expected_l15_game_ids=expected_l15_game_ids,
        team_ids=team_ids,
    )
    wrong_snapshot = wrong_repository.get_snapshot(
        TeamMatchupSnapshotScope("2025-26", AS_OF, 15)
    )
    wrong_observation = next(
        item for item in wrong_snapshot.observations if item.surface == "shot_zones"
    )
    assert wrong_observation.status == "unavailable"
    assert wrong_observation.unavailable_reason == "publication_game_set_mismatch"
    assert not any(fact.base == "shot_zones" for fact in wrong_snapshot.facts)


def test_materialization_resolves_l15_governance_per_publication_cutoff(tmp_path):
    engine = _engine(tmp_path)
    games = _canonical_league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    expected_game_ids, current_l15, team_ids = _governance(games)
    prior_l15 = {
        team_id: frozenset(f"prior-{team_id}-{index}" for index in range(15))
        for team_id in CANONICAL_TEAM_IDS
    }
    cutoffs = {
        "synergy_play_types_opponent_l15": "2025-10-14T00:00:00+00:00",
        "grouped_shot_types_opponent_l15": "2025-10-15T00:00:00+00:00",
        "exact_shot_zones_opponent_l15": "2025-10-14T00:00:00+00:00",
    }
    governed = {date(2025, 10, 14): prior_l15, AS_OF: current_l15}
    reader = _reader(
        cutoff_by_stream=cutoffs,
        game_ids_by_stream={
            stream_key: {
                team_id: tuple(game_ids)
                for team_id, game_ids in governed[
                    date.fromisoformat(cutoff[:10])
                ].items()
            }
            for stream_key, cutoff in cutoffs.items()
        },
    )
    with engine.begin() as connection:
        prior_authority = _insert_publication_authority(
            connection,
            cutoff=datetime(2025, 10, 14, tzinfo=timezone.utc),
            manifest_id="prior-cutoff-manifest",
            catalog_id="prior-cutoff-catalog",
        )
        for stream_key, cutoff in cutoffs.items():
            publication_id = f"publication-{stream_key}"
            encoded = connection.execute(
                PublicationVersion.__table__.select().where(
                    PublicationVersion.publication_id == publication_id
                )
            ).mappings().one()["payload"]
            payload = json.loads(encoded)
            game_ids = governed[date.fromisoformat(cutoff[:10])]
            for row in payload["rows"]:
                row["game_ids"] = list(game_ids[row["team_id"]])
                row["game_count"] = len(row["game_ids"])
            encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            connection.execute(
                PublicationVersion.__table__.update().where(
                    PublicationVersion.publication_id == publication_id
                ).values(
                    cutoff=datetime.fromisoformat(cutoff),
                    payload=encoded,
                    checksum=hashlib.sha256(encoded.encode()).hexdigest(),
                    **(
                        prior_authority
                        if cutoff.startswith("2025-10-14")
                        else _publication_authority_values()
                    ),
                )
            )
    calls = []

    def resolve(season, cutoff):
        calls.append((season, cutoff))
        return governed[cutoff.date()]

    service = LedgerMatchupMaterializationService(
        ledger,
        repository,
        publication_reader=reader,
        l15_expectation_resolver=resolve,
        clock=lambda: RETRIEVED_AT,
    )
    service.materialize(
        "2025-26",
        as_of=AS_OF,
        expected_game_ids=expected_game_ids,
        expected_l15_game_ids=current_l15,
        team_ids=team_ids,
    )

    snapshot = repository.get_snapshot(
        TeamMatchupSnapshotScope("2025-26", AS_OF, 15)
    )
    assert {
        observation.surface
        for observation in snapshot.observations
        if observation.status == "available"
    } >= {"play_types", "shot_types", "shot_zones", "traditional"}
    assert {cutoff.date() for _, cutoff in calls} == {date(2025, 10, 14), AS_OF}


def test_prior_season_unavailable_nba_lineage_does_not_rollback_ledger_facts(
    tmp_path,
):
    engine = _engine(tmp_path)
    games = _canonical_league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    publications = PublicationService(engine)
    for template in NBA_PUBLICATION_STREAMS.values():
        for window in ("season", "l15"):
            publications.register_stream(
                template.format(window=window),
                provider="nba",
                owner="residential_collector",
                required_observations=(),
                publication_strategy="snapshot_replace",
                supported_windows=(window,),
                enabled=not (
                    template.startswith("synergy_") and window == "l15"
                ),
            )
    for stream_key in (
        "traditional_opponent",
        "traditional_opponent_season",
        "traditional_opponent_l15",
        "assist_locations",
        "assist_locations_season",
        "assist_locations_l15",
    ):
        publications.register_stream(
            stream_key,
            provider="pbp_stats",
            owner="ledger",
            required_observations=(),
            publication_strategy="snapshot_replace",
            enabled=False,
        )
    repository = TeamMatchupRepository(
        engine,
        write_fence=LegacyWriteFence(engine),
        publication_write_capability=create_publication_write_capability(engine),
    )
    unavailable = frozenset(
        template.format(window=window)
        for template in NBA_PUBLICATION_STREAMS.values()
        for window in ("season", "l15")
    )
    reader = _reader(
        unavailable=unavailable,
        unavailable_lineage_by_stream={
            stream_key: (
                f"prior-{stream_key}",
                "2024-25",
                "2025-04-15T00:00:00+00:00",
            )
            for stream_key in unavailable
        },
    )
    expected_game_ids, expected_l15_game_ids, team_ids = _governance(games)

    LedgerMatchupMaterializationService(
        ledger,
        repository,
        publication_reader=reader,
        clock=lambda: RETRIEVED_AT,
    ).materialize(
        "2025-26",
        as_of=AS_OF,
        expected_game_ids=expected_game_ids,
        expected_l15_game_ids=expected_l15_game_ids,
        team_ids=team_ids,
    )

    snapshot = repository.get_snapshot(
        TeamMatchupSnapshotScope("2025-26", AS_OF)
    )
    assert any(fact.base == "traditional" for fact in snapshot.facts)
    assert all(
        observation.status == "unavailable"
        for observation in snapshot.observations
        if observation.surface in NBA_PUBLICATION_STREAMS
    )


@pytest.mark.parametrize(
    ("publication_cutoff", "expected_status", "expected_reason"),
    (
        ("2025-10-16T02:00:00+00:00", "available", None),
        (
            "2025-10-16T04:00:00+00:00",
            "unavailable",
            "publication_cutoff_after_as_of",
        ),
    ),
)
def test_publication_cutoff_uses_eastern_slate_day_in_materialization_and_fence(
    tmp_path,
    publication_cutoff,
    expected_status,
    expected_reason,
):
    engine = _engine(tmp_path)
    games = _canonical_league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    stream_key = "exact_shot_zones_opponent_season"
    with engine.begin() as connection:
        authority = _insert_publication_authority(
            connection,
            cutoff=datetime.fromisoformat(publication_cutoff),
            manifest_id=f"cutoff-manifest-{expected_status}",
            catalog_id=f"cutoff-catalog-{expected_status}",
        )
        connection.execute(
            PublicationVersion.__table__.update().where(
                PublicationVersion.publication_id == f"publication-{stream_key}"
            ).values(
                cutoff=datetime.fromisoformat(publication_cutoff),
                **authority,
            )
        )
    expected_game_ids, expected_l15_game_ids, team_ids = _governance(games)

    LedgerMatchupMaterializationService(
        ledger,
        repository,
        publication_reader=_reader(
            cutoff_by_stream={stream_key: publication_cutoff},
        ),
        clock=lambda: RETRIEVED_AT,
    ).materialize(
        "2025-26",
        as_of=AS_OF,
        expected_game_ids=expected_game_ids,
        expected_l15_game_ids=expected_l15_game_ids,
        team_ids=team_ids,
    )

    snapshot = repository.get_snapshot(
        TeamMatchupSnapshotScope("2025-26", AS_OF)
    )
    observations = {item.surface: item for item in snapshot.observations}
    assert observations["shot_zones"].status == expected_status
    assert observations["shot_zones"].unavailable_reason == expected_reason
    assert any(fact.base == "shot_zones" for fact in snapshot.facts) is (
        expected_status == "available"
    )
    assert observations["shot_types"].status == "available"
    assert any(fact.base == "traditional" for fact in snapshot.facts)
    assert any(fact.base == "shot_types" for fact in snapshot.facts)


def test_publication_cutoff_uses_dst_aware_eastern_midnight_boundary():
    def read_at(cutoff):
        return PublicationRead(
            stream_key="exact_shot_zones_opponent_season",
            publication_id="publication",
            season="2025-26",
            cutoff=cutoff,
            version=1,
            status="active",
            freshness="fresh",
            age_seconds=0,
            payload={"rows": []},
        )

    fall_back_day = date(2025, 11, 2)
    assert publication_cutoff_reason(
        read_at("2025-11-03T04:59:59+00:00"), fall_back_day
    ) is None
    assert publication_cutoff_reason(
        read_at("2025-11-03T05:00:00+00:00"), fall_back_day
    ) == "publication_cutoff_after_as_of"


def test_materialization_rejects_fabricated_season_game_set_independently(
    tmp_path,
):
    engine = _engine(tmp_path)
    games = _canonical_league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    season_ids = {
        team_id: frozenset(game_ids)
        for team_id, game_ids in _season_game_ids_by_team().items()
    }
    expected_game_ids, l15_ids, team_ids = _governance(games)
    wrong = dict(_season_game_ids_by_team())
    wrong[CANONICAL_TEAM_IDS[0]] = tuple(
        f"fabricated-{index}" for index in range(15)
    )
    stream_key = "exact_shot_zones_opponent_season"

    LedgerMatchupMaterializationService(
        ledger,
        repository,
        publication_reader=_reader(
            game_ids_by_stream={stream_key: wrong},
        ),
        l15_expectation_resolver=_WindowGovernanceResolver(
            season=season_ids, l15=l15_ids
        ),
        clock=lambda: RETRIEVED_AT,
    ).materialize(
        "2025-26",
        as_of=AS_OF,
        expected_game_ids=expected_game_ids,
        expected_l15_game_ids=l15_ids,
        team_ids=team_ids,
    )

    snapshot = repository.get_snapshot(
        TeamMatchupSnapshotScope("2025-26", AS_OF)
    )
    observations = {item.surface: item for item in snapshot.observations}
    assert observations["shot_zones"].status == "unavailable"
    assert observations["shot_zones"].unavailable_reason == (
        "publication_game_set_mismatch"
    )
    assert observations["shot_types"].status == "available"
    assert any(fact.base == "traditional" for fact in snapshot.facts)
    assert not any(fact.base == "shot_zones" for fact in snapshot.facts)


@pytest.mark.parametrize(
    "metric_keys",
    [
        tuple(sorted(NBA_PUBLICATION_TAXONOMY["shot_types"] - {"pullups_FG2A"})),
        tuple(sorted(NBA_PUBLICATION_TAXONOMY["shot_types"] | {"uncontracted_PTS"})),
    ],
    ids=("missing_registered_metric", "extra_uncontracted_metric"),
)
def test_publication_requires_exact_registered_metric_taxonomy(tmp_path, metric_keys):
    engine = _engine(tmp_path)
    games = _canonical_league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    service = LedgerMatchupMaterializationService(
        ledger,
        repository,
        publication_reader=_reader(
            metric_keys_by_stream={
                "grouped_shot_types_opponent_season": metric_keys,
            }
        ),
        clock=lambda: RETRIEVED_AT,
    )
    expected_game_ids, expected_l15_game_ids, team_ids = _governance(games)
    service.materialize(
        "2025-26",
        as_of=AS_OF,
        expected_game_ids=expected_game_ids,
        expected_l15_game_ids=expected_l15_game_ids,
        team_ids=team_ids,
    )
    snapshot = repository.get_snapshot(TeamMatchupSnapshotScope("2025-26", AS_OF))
    observation = next(item for item in snapshot.observations if item.surface == "shot_types")
    assert observation.status == "unavailable"
    assert observation.unavailable_reason == "publication_metric_taxonomy_mismatch"
    assert not any(fact.base == "shot_types" for fact in snapshot.facts)


def test_publication_rejects_normalized_alias_for_registered_key(tmp_path):
    keys = set(NBA_PUBLICATION_TAXONOMY["shot_types"])
    keys.remove("catch_and_shoot_FG2A")
    keys.add("Catch and Shoot_FG2A")
    engine = _engine(tmp_path)
    games = _canonical_league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    service = LedgerMatchupMaterializationService(
        ledger, repository,
        publication_reader=_reader(metric_keys_by_stream={
            "grouped_shot_types_opponent_season": tuple(keys),
        }),
        clock=lambda: RETRIEVED_AT,
    )
    expected_game_ids, expected_l15_game_ids, team_ids = _governance(games)
    service.materialize(
        "2025-26", as_of=AS_OF, expected_game_ids=expected_game_ids,
        expected_l15_game_ids=expected_l15_game_ids, team_ids=team_ids,
    )
    snapshot = repository.get_snapshot(TeamMatchupSnapshotScope("2025-26", AS_OF))
    observation = next(item for item in snapshot.observations if item.surface == "shot_types")
    assert observation.status == "unavailable"
    assert observation.unavailable_reason == "publication_metric_taxonomy_mismatch"


def test_injected_governed_roster_cannot_replace_canonical_nba_identity_set():
    rows = _rows(tuple(sorted(NBA_PUBLICATION_TAXONOMY["shot_zones"])))

    with pytest.raises(PublicationValidationError, match="publication_surface_incomplete"):
        validate_publication_rows(
            "shot_zones",
            rows,
            expected_team_ids={CANONICAL_TEAM_IDS[0]},
        )


def test_publication_requires_canonical_tricode_for_each_nba_id():
    rows = list(_rows(tuple(sorted(NBA_PUBLICATION_TAXONOMY["shot_zones"]))))
    rows[0] = replace(rows[0], team_tricode="T01")

    with pytest.raises(PublicationValidationError, match="publication_team_identity_mismatch"):
        validate_publication_rows("shot_zones", tuple(rows))


def test_nba_unavailable_surfaces_do_not_retain_pbp_fallback(tmp_path):
    engine = _engine(tmp_path)
    games = _canonical_league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    repository.replace_snapshots(
        (
            (
                TeamMatchupSnapshotScope("2025-26", AS_OF),
                [
                    TeamMatchupFact(
                        team_id=team_id,
                        base="shot_zones",
                        slice_key="Restricted Area",
                        stat_key="FGM",
                        raw_value=77,
                        denominator_value=48,
                        denominator_unit="minutes",
                        provider="pbp_stats",
                    )
                    for team_id in CANONICAL_TEAM_IDS
                ],
                [TeamMatchupObservation("shot_zones", "available")],
            ),
        ),
        retrieved_at=RETRIEVED_AT,
    )
    service = LedgerMatchupMaterializationService(
        ledger,
        repository,
        publication_reader=_reader(
            unavailable=frozenset({"exact_shot_zones_opponent_season"})
        ),
        clock=lambda: RETRIEVED_AT,
    )
    expected_game_ids, expected_l15_game_ids, team_ids = _governance(games)
    service.materialize(
        "2025-26",
        as_of=AS_OF,
        expected_game_ids=expected_game_ids,
        expected_l15_game_ids=expected_l15_game_ids,
        team_ids=team_ids,
    )

    # A legacy row must never backfill an unavailable NBA-owned surface.
    query = TeamMatchupQueryService(repository, publication_reader=_reader(
        unavailable=frozenset({"exact_shot_zones_opponent_season"})
    ))
    window = query.get_window(TeamMatchupSnapshotScope("2025-26", AS_OF))
    assert not any(metric.base == "shot_zones" for metric in window.league_metrics)
    assert any(metric.base == "traditional" for metric in window.league_metrics)
    observation = next(
        item for item in window.observations if item.surface == "shot_zones"
    )
    assert observation.status == "unavailable"
    assert observation.unavailable_reason == "publication_missing"
    persisted_window = TeamMatchupQueryService(repository).get_window(
        TeamMatchupSnapshotScope("2025-26", AS_OF)
    )
    assert not any(
        metric.base == "shot_zones" for metric in persisted_window.league_metrics
    )


def test_publication_surfaces_keep_independent_cutoffs_and_freshness(tmp_path):
    engine = _engine(tmp_path)
    games = _canonical_league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    reader = _reader(
        cutoff_by_stream={
            "grouped_shot_types_opponent_season": "2025-10-14T00:00:00+00:00",
            "exact_shot_zones_opponent_season": "2025-10-15T00:00:00+00:00",
        },
        freshness_by_stream={
            "grouped_shot_types_opponent_season": "fresh",
            "exact_shot_zones_opponent_season": "stale",
        },
    )
    with engine.begin() as connection:
        prior_authority = _insert_publication_authority(
            connection,
            cutoff=datetime(2025, 10, 14, tzinfo=timezone.utc),
            manifest_id="independent-cutoff-manifest",
            catalog_id="independent-cutoff-catalog",
        )
        connection.execute(
            PublicationVersion.__table__.update().where(
                PublicationVersion.publication_id
                == "publication-grouped_shot_types_opponent_season"
            ).values(
                cutoff=datetime(2025, 10, 14, tzinfo=timezone.utc),
                **prior_authority,
            )
        )
    service = LedgerMatchupMaterializationService(
        ledger, repository, publication_reader=reader, clock=lambda: RETRIEVED_AT
    )
    expected_game_ids, expected_l15_game_ids, team_ids = _governance(games)
    service.materialize(
        "2025-26",
        as_of=AS_OF,
        expected_game_ids=expected_game_ids,
        expected_l15_game_ids=expected_l15_game_ids,
        team_ids=team_ids,
    )

    observations = {
        item.surface: item
        for item in repository.get_snapshot(
            TeamMatchupSnapshotScope("2025-26", AS_OF)
        ).observations
    }
    assert observations["shot_types"].publication.cutoff == (
        "2025-10-14T00:00:00+00:00"
    )
    assert observations["shot_types"].publication.freshness == "fresh"
    assert observations["shot_zones"].publication.cutoff == (
        "2025-10-15T00:00:00+00:00"
    )
    assert observations["shot_zones"].publication.freshness == "stale"
    assert observations["traditional"].ledger_checksum is not None


def test_nba_publication_reads_remain_available_when_ledger_surface_is_missing(tmp_path):
    engine = _engine(tmp_path)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    scope = TeamMatchupSnapshotScope("2025-26", AS_OF)
    repository.replace_snapshots(
        (
            (
                scope,
                (),
                (
                    TeamMatchupObservation(
                        "traditional",
                        "missing",
                        unavailable_reason="ledger_window_incomplete",
                    ),
                ),
            ),
        ),
        retrieved_at=RETRIEVED_AT,
    )

    window = TeamMatchupQueryService(
        repository, publication_reader=_reader()
    ).get_window(scope)
    assert any(metric.base == "shot_zones" for metric in window.league_metrics)
    assert not any(metric.base == "traditional" for metric in window.league_metrics)
    assert next(
        item for item in window.observations if item.surface == "traditional"
    ).status == "missing"


def test_direct_l15_query_uses_governed_nba_publication_without_ledger_window(tmp_path):
    engine = _engine(tmp_path)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    expected = {
        team_id: frozenset(f"governed-{team_id}-{index}" for index in range(15))
        for team_id in CANONICAL_TEAM_IDS
    }
    reader = _reader(
        game_ids_by_stream={
            stream_key: {
                team_id: tuple(game_ids)
                for team_id, game_ids in expected.items()
            }
            for stream_key in (
                "synergy_play_types_opponent_l15",
                "grouped_shot_types_opponent_l15",
                "exact_shot_zones_opponent_l15",
            )
        }
    )
    window = TeamMatchupQueryService(
        repository,
        publication_reader=reader,
        l15_expectation_resolver=lambda season, cutoff: expected,
    ).get_window(TeamMatchupSnapshotScope("2025-26", AS_OF, 15))

    assert any(metric.base == "shot_zones" for metric in window.league_metrics)
    assert any(metric.base == "shot_types" for metric in window.league_metrics)
    assert not any(metric.base == "traditional" for metric in window.league_metrics)


def test_latest_l15_query_uses_governance_without_a_legacy_snapshot(tmp_path):
    engine = _engine(tmp_path)
    repository = TeamMatchupRepository(engine)
    expected = {
        team_id: frozenset(f"governed-{team_id}-{index}" for index in range(15))
        for team_id in CANONICAL_TEAM_IDS
    }
    reader = _reader(
        game_ids_by_stream={
            stream_key: {
                team_id: tuple(game_ids)
                for team_id, game_ids in expected.items()
            }
            for stream_key in (
                "synergy_play_types_opponent_l15",
                "grouped_shot_types_opponent_l15",
                "exact_shot_zones_opponent_l15",
            )
        }
    )
    window = TeamMatchupQueryService(
        repository,
        clock=lambda: datetime(2025, 10, 16, 12, tzinfo=timezone.utc),
        publication_reader=reader,
        l15_expectation_resolver=lambda season, cutoff: expected,
    ).get_latest_window("2025-26", window_games=15)

    assert window is not None
    assert any(metric.base == "shot_zones" for metric in window.league_metrics)
    assert next(
        item for item in window.observations if item.surface == "shot_zones"
    ).status == "available"


def test_l15_query_resolves_each_publication_at_its_immutable_cutoff(tmp_path):
    engine = _engine(tmp_path)
    repository = TeamMatchupRepository(engine)
    cutoffs = {
        "synergy_play_types_opponent_l15": "2025-10-14T00:00:00+00:00",
        "grouped_shot_types_opponent_l15": "2025-10-15T00:00:00+00:00",
        "exact_shot_zones_opponent_l15": "2025-10-14T00:00:00+00:00",
    }
    governed_by_date = {
        day: {
            team_id: frozenset(
                f"{day}-game-{team_id}-{index}" for index in range(15)
            )
            for team_id in CANONICAL_TEAM_IDS
        }
        for day in ("2025-10-14", "2025-10-15")
    }
    reader = _reader(
        cutoff_by_stream=cutoffs,
        game_ids_by_stream={
            stream_key: {
                team_id: tuple(game_ids)
                for team_id, game_ids in governed_by_date[cutoff[:10]].items()
            }
            for stream_key, cutoff in cutoffs.items()
        },
    )
    calls = []

    def resolve(season, cutoff):
        calls.append((season, cutoff.isoformat()))
        return governed_by_date[cutoff.date().isoformat()]

    window = TeamMatchupQueryService(
        repository,
        publication_reader=reader,
        l15_expectation_resolver=resolve,
        clock=lambda: datetime(2025, 10, 16, 12, tzinfo=timezone.utc),
    ).get_latest_window("2025-26", window_games=15)

    assert {
        observation.surface
        for observation in window.observations
        if observation.status == "available"
    } >= {"play_types", "shot_types", "shot_zones"}
    assert {cutoff[:10] for _, cutoff in calls} == {"2025-10-14", "2025-10-15"}


@pytest.mark.parametrize(
    "mode",
    ("one_game_one_team", "missing_metric", "wrong_game_set"),
)
def test_compose_rejects_invalid_nba_payload_without_advancing_pointer(
    tmp_path, mode
):
    engine = _engine(tmp_path)
    expected = {
        team_id: frozenset(f"governed-{team_id}-{index}" for index in range(15))
        for team_id in CANONICAL_TEAM_IDS
    }
    publications = PublicationService(
        engine,
        l15_expectation_resolver=_WindowGovernanceResolver(
            season=expected,
            l15=expected,
        ),
    )
    stream_key = "exact_shot_zones_opponent_l15"
    publications.register_stream(
        stream_key,
        provider="nba",
        owner="residential_collector",
        required_observations=(),
        publication_strategy="snapshot_replace",
        supported_windows=("l15",),
        enabled=True,
    )
    with engine.connect() as connection:
        before = connection.execute(
            PublicationPointer.__table__.select().where(
                PublicationPointer.stream_key == stream_key
            )
        ).mappings().one()

    with pytest.raises(ControlPlaneError, match="publication_candidate_invalid"):
        publications.compose(
            stream_key,
            season="2025-26",
            cutoff=datetime(2025, 10, 15, tzinfo=timezone.utc),
            payload=_candidate_payload(expected, mode=mode),
            expected_fence=before["fence"],
        )

    with engine.connect() as connection:
        after = connection.execute(
            PublicationPointer.__table__.select().where(
                PublicationPointer.stream_key == stream_key
            )
        ).mappings().one()
    assert after["active_publication_id"] == before["active_publication_id"]
    assert after["fence"] == before["fence"]


def test_compose_rejects_fabricated_season_game_ids_without_advancing_pointer(
    tmp_path,
):
    engine = _engine(tmp_path)
    season_ids = {
        team_id: frozenset(game_ids)
        for team_id, game_ids in _season_game_ids_by_team().items()
    }
    _, l15_ids, _ = _governance(_canonical_league_games())
    publications = PublicationService(
        engine,
        l15_expectation_resolver=_WindowGovernanceResolver(
            season=season_ids, l15=l15_ids
        ),
    )
    stream_key = "exact_shot_zones_opponent_season"
    publications.register_stream(
        stream_key,
        provider="nba",
        owner="residential_collector",
        required_observations=(),
        publication_strategy="snapshot_replace",
        supported_windows=("season",),
        enabled=True,
    )
    with engine.connect() as connection:
        before = connection.execute(
            PublicationPointer.__table__.select().where(
                PublicationPointer.stream_key == stream_key
            )
        ).mappings().one()

    with pytest.raises(ControlPlaneError, match="publication_candidate_invalid"):
        publications.compose(
            stream_key,
            season="2025-26",
            cutoff=datetime(2025, 10, 15, tzinfo=timezone.utc),
            payload=_candidate_payload(season_ids, mode="wrong_game_set"),
            expected_fence=before["fence"],
        )

    with engine.connect() as connection:
        after = connection.execute(
            PublicationPointer.__table__.select().where(
                PublicationPointer.stream_key == stream_key
            )
        ).mappings().one()
    assert after["active_publication_id"] == before["active_publication_id"]
    assert after["fence"] == before["fence"]


def test_per48_only_publication_composes_activates_reads_and_materializes(
    tmp_path,
):
    engine = _engine(tmp_path)
    games = _canonical_league_games()
    season_ids = {
        team_id: frozenset(game_ids)
        for team_id, game_ids in _season_game_ids_by_team().items()
    }
    expected_game_ids, l15_ids, team_ids = _governance(games)
    resolver = _WindowGovernanceResolver(season=season_ids, l15=l15_ids)
    publications = PublicationService(
        engine,
        l15_expectation_resolver=resolver,
    )

    l15_stream = "exact_shot_zones_opponent_l15"
    publications.register_stream(
        l15_stream,
        provider="nba",
        owner="residential_collector",
        required_observations=(),
        publication_strategy="snapshot_replace",
        supported_windows=("l15",),
        enabled=True,
    )
    with engine.connect() as connection:
        l15_pointer = connection.execute(
            PublicationPointer.__table__.select().where(
                PublicationPointer.stream_key == l15_stream
            )
        ).mappings().one()
    with pytest.raises(ControlPlaneError, match="publication_candidate_invalid"):
        publications.compose(
            l15_stream,
            season="2025-26",
            cutoff=datetime(2025, 10, 15, tzinfo=timezone.utc),
            payload=_per48_only_payload(l15_ids),
            expected_fence=l15_pointer["fence"],
        )

    season_stream = "exact_shot_zones_opponent_season"
    publications.register_stream(
        season_stream,
        provider="nba",
        owner="residential_collector",
        required_observations=(),
        publication_strategy="snapshot_replace",
        supported_windows=("season",),
        enabled=False,
    )
    season_payload = _per48_only_payload(season_ids)
    encoded = json.dumps(season_payload, separators=(",", ":"), sort_keys=True)
    candidate_id = "per48-only-season-candidate"
    with engine.begin() as connection:
        connection.execute(PublicationVersion.__table__.insert().values(
            **_publication_authority_values(),
            publication_id=candidate_id,
            stream_key=season_stream,
            season="2025-26",
            cutoff=datetime(2025, 10, 15, tzinfo=timezone.utc),
            version=3,
            status="candidate",
            checksum=hashlib.sha256(encoded.encode()).hexdigest(),
            payload=encoded,
            created_at=RETRIEVED_AT,
            fence=0,
        ))
    publications.activate_stream(
        season_stream,
        reason="activate per48-only contract",
        season="2025-26",
        cutoff=datetime(2025, 10, 15, tzinfo=timezone.utc),
        candidate_publication_id=candidate_id,
    )

    reader = DatabaseFirstPublicationReader(engine, clock=lambda: RETRIEVED_AT)
    read = reader.read(season_stream, season="2025-26")
    assert read.available
    assert len(read.decoded) == 30
    assert all(
        not row.league_average
        and not row.population_sigma
        and not row.competition_rank
        for row in read.decoded
    )

    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(
        engine,
        publication_write_capability=create_publication_write_capability(engine),
    )
    LedgerMatchupMaterializationService(
        ledger,
        repository,
        publication_reader=reader,
        l15_expectation_resolver=resolver,
        clock=lambda: RETRIEVED_AT,
    ).materialize(
        "2025-26",
        as_of=AS_OF,
        expected_game_ids=expected_game_ids,
        expected_l15_game_ids=l15_ids,
        team_ids=team_ids,
    )
    snapshot = repository.get_snapshot(
        TeamMatchupSnapshotScope("2025-26", AS_OF)
    )
    assert any(fact.base == "shot_zones" for fact in snapshot.facts)
    assert next(
        item for item in snapshot.observations if item.surface == "shot_zones"
    ).status == "available"


def test_invalid_active_publication_retains_computed_age(tmp_path):
    engine = _engine(tmp_path)
    publications = PublicationService(engine)
    stream_key = "exact_shot_zones_opponent_season"
    publications.register_stream(
        stream_key,
        provider="nba",
        owner="residential_collector",
        required_observations=(),
        publication_strategy="snapshot_replace",
        enabled=True,
    )
    with engine.begin() as connection:
        connection.execute(
            PublicationVersion.__table__.update().where(
                PublicationVersion.publication_id == f"publication-{stream_key}"
            ).values(
                payload="{}",
                checksum=hashlib.sha256(b"{}").hexdigest(),
            )
        )

    read = DatabaseFirstPublicationReader(
        engine, clock=lambda: RETRIEVED_AT
    ).read(stream_key, season="2025-26")

    assert read.status == "unavailable"
    assert read.unavailable_reason == "publication_payload_invalid"
    assert read.age_seconds == 122400


def test_checksum_mismatch_fails_closed_for_read_activation_rollback_and_rehearsal(
    tmp_path,
):
    engine = _engine(tmp_path)
    _, l15_ids, _ = _governance(_canonical_league_games())
    publications = PublicationService(
        engine,
        l15_expectation_resolver=_WindowGovernanceResolver(
            season=l15_ids,
            l15=l15_ids,
        ),
    )
    stream_key = "exact_shot_zones_opponent_l15"
    publications.register_stream(
        stream_key,
        provider="nba",
        owner="residential_collector",
        required_observations=(),
        publication_strategy="snapshot_replace",
        supported_windows=("l15",),
        enabled=True,
    )
    current = _advance_test_publication(
        engine, stream_key, _per48_only_payload(l15_ids),
        "checksum-current",
    )
    with engine.begin() as connection:
        prior_id = connection.execute(
            select(PublicationPointer.previous_publication_id).where(
                PublicationPointer.stream_key == stream_key
            )
        ).scalar_one()
        prior_payload = connection.execute(
            select(PublicationVersion.payload).where(
                PublicationVersion.publication_id == prior_id
            )
        ).scalar_one()
        connection.execute(
            PublicationVersion.__table__.update().where(
                PublicationVersion.publication_id == prior_id
            ).values(payload=prior_payload.replace("1.0", "1.25", 1))
        )
    with pytest.raises(ControlPlaneError, match="publication_checksum_mismatch"):
        publications.rollback(stream_key, reason="reject corrupt prior")
    with engine.connect() as connection:
        assert connection.execute(
            select(PublicationPointer.active_publication_id).where(
                PublicationPointer.stream_key == stream_key
            )
        ).scalar_one() == current.publication_id

    candidate_payload = json.loads(current.payload)
    candidate_payload["rows"][0]["per48"][
        next(iter(candidate_payload["rows"][0]["per48"]))
    ] = 2.0
    candidate_encoded = json.dumps(
        candidate_payload, separators=(",", ":"), sort_keys=True
    )
    candidate_id = "checksum-mismatch-candidate"
    with engine.begin() as connection:
        connection.execute(PublicationVersion.__table__.insert().values(
            **_publication_authority_values(),
            publication_id=candidate_id,
            stream_key=stream_key,
            season="2025-26",
            cutoff=datetime(2025, 10, 15, tzinfo=timezone.utc),
            version=current.version + 1,
            status="candidate",
            checksum=current.checksum,
            payload=candidate_encoded,
            created_at=RETRIEVED_AT,
            fence=0,
        ))
    with pytest.raises(ControlPlaneError, match="publication_checksum_mismatch"):
        publications.activate_stream(
            stream_key,
            reason="reject stale checksum",
            season="2025-26",
            cutoff=datetime(2025, 10, 15, tzinfo=timezone.utc),
            candidate_publication_id=candidate_id,
        )
    with engine.connect() as connection:
        assert connection.execute(
            select(PublicationPointer.active_publication_id).where(
                PublicationPointer.stream_key == stream_key
            )
        ).scalar_one() == current.publication_id

    with pytest.raises(ValueError, match="rehearsal publication checksum mismatch"):
        HistoricalRehearsalRunner(
            engine, environment="unit"
        )._load_isolated_publications(
            {stream_key: candidate_id},
            season="2025-26",
            cutoff=date(2025, 10, 14),
        )

    with engine.begin() as connection:
        current_payload = connection.execute(
            select(PublicationVersion.payload).where(
                PublicationVersion.publication_id == current.publication_id
            )
        ).scalar_one()
        mutated = json.loads(current_payload)
        mutated["rows"][0]["per48"][
            next(iter(mutated["rows"][0]["per48"]))
        ] = 3.0
        connection.execute(
            PublicationVersion.__table__.update().where(
                PublicationVersion.publication_id == current.publication_id
            ).values(payload=json.dumps(
                mutated, separators=(",", ":"), sort_keys=True
            ))
        )
    read = DatabaseFirstPublicationReader(
        engine, clock=lambda: RETRIEVED_AT
    ).read(stream_key, season="2025-26")
    assert read.status == "unavailable"
    assert read.unavailable_reason == "publication_checksum_mismatch"
    assert read.age_seconds is not None


def test_rehearsal_and_rollback_require_exact_publication_authority(tmp_path):
    engine = _engine(tmp_path)
    _, l15_ids, _ = _governance(_canonical_league_games())
    resolver = ActiveManifestLedgerGovernanceReader(engine)
    publications = PublicationService(
        engine,
        l15_expectation_resolver=resolver,
    )
    stream_key = "exact_shot_zones_opponent_l15"
    publications.register_stream(
        stream_key,
        provider="nba",
        owner="residential_collector",
        required_observations=(),
        publication_strategy="snapshot_replace",
        supported_windows=("l15",),
        enabled=True,
    )
    current = _advance_test_publication(
        engine, stream_key, _per48_only_payload(l15_ids),
        "authority-current",
    )
    with engine.connect() as connection:
        prior_id = connection.execute(
            select(PublicationPointer.previous_publication_id).where(
                PublicationPointer.stream_key == stream_key
            )
        ).scalar_one()

    with engine.begin() as connection:
        connection.execute(
            PublicationVersion.__table__.update().where(
                PublicationVersion.publication_id == current.publication_id
            ).values(manifest_id=None)
        )
    with pytest.raises(ValueError, match="rehearsal publication authority mismatch"):
        HistoricalRehearsalRunner(
            engine, environment="unit"
        )._load_isolated_publications(
            {stream_key: current.publication_id},
            season="2025-26",
            cutoff=date(2025, 10, 14),
        )

    with engine.begin() as connection:
        connection.execute(
            PublicationVersion.__table__.update().where(
                PublicationVersion.publication_id == current.publication_id
            ).values(**_publication_authority_values())
        )
        connection.execute(
            PublicationVersion.__table__.update().where(
                PublicationVersion.publication_id == prior_id
            ).values(event_catalog_checksum="mismatched-authority")
        )
    with pytest.raises(
        ControlPlaneError, match="publication_governance_unavailable"
    ):
        publications.rollback(stream_key, reason="reject invalid authority")
    with engine.connect() as connection:
        assert connection.execute(
            select(PublicationPointer.active_publication_id).where(
                PublicationPointer.stream_key == stream_key
            )
        ).scalar_one() == current.publication_id


@pytest.mark.parametrize(
    "manifest_values",
    (
        {"scopes": '["synergy"]'},
        {"accepted_versions": "[2]"},
    ),
)
def test_publication_authority_requires_ledger_scope_and_schema_version(
    tmp_path,
    manifest_values,
):
    engine = _engine(tmp_path)
    stream_key = "exact_shot_zones_opponent_season"
    PublicationService(engine).register_stream(
        stream_key,
        provider="nba",
        owner="residential_collector",
        required_observations=(),
        publication_strategy="snapshot_replace",
        supported_windows=("season",),
        enabled=True,
    )
    publication_id = f"publication-{stream_key}"
    with engine.begin() as connection:
        connection.execute(
            CollectionManifest.__table__.update().where(
                CollectionManifest.manifest_id == DEFAULT_MANIFEST_ID
            ).values(**manifest_values)
        )

    read = DatabaseFirstPublicationReader(engine).read(
        stream_key, season="2025-26"
    )
    assert read.status == "unavailable"
    assert read.unavailable_reason == "publication_authority_invalid"
    with pytest.raises(ValueError, match="rehearsal publication authority mismatch"):
        HistoricalRehearsalRunner(
            engine, environment="unit"
        )._load_isolated_publications(
            {stream_key: publication_id},
            season="2025-26",
            cutoff=date(2025, 10, 14),
        )

    capability = create_publication_write_capability(engine)
    with engine.connect() as connection:
        version = connection.execute(
            PublicationVersion.__table__.select().where(
                PublicationVersion.publication_id == publication_id
            )
        ).mappings().one()
        with pytest.raises(ValueError, match="publication_write_context_invalid"):
            capability._verify_authority_binding(connection, version)


def test_rehearsal_date_uses_eastern_slate_day_for_utc_evening(tmp_path):
    engine = _engine(tmp_path)
    stream_key = "rehearsal-date-probe"
    cutoff = datetime(2025, 11, 2, 3, 30, tzinfo=timezone.utc)
    publications = PublicationService(engine)
    publications.register_stream(
        stream_key,
        provider="derived",
        owner="railway",
        required_observations=(),
        publication_strategy="snapshot_replace",
        supported_windows=("season",),
        enabled=True,
    )
    publication = publications.compose(
        stream_key,
        season="2025-26",
        cutoff=cutoff,
        payload={"value": 1},
    )

    loaded = HistoricalRehearsalRunner(
        engine, environment="unit"
    )._load_isolated_publications(
        {stream_key: publication.publication_id},
        season="2025-26",
        cutoff=date(2025, 11, 1),
    )
    assert loaded[stream_key].publication_id == publication.publication_id


def test_direct_publication_query_derives_statistics_from_per48_rows(tmp_path):
    engine = _engine(tmp_path)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    window = TeamMatchupQueryService(
        repository,
        publication_reader=_reader(contradict_statistics=True),
    ).get_window(TeamMatchupSnapshotScope("2025-26", AS_OF))

    metric = next(
        item
        for item in window.league_metrics
        if (item.base, item.slice_key, item.stat_key)
        == ("shot_zones", "Restricted Area", "FGM")
    )
    expected_average = fmean(CANONICAL_TEAM_IDS)
    expected_sigma = pstdev(CANONICAL_TEAM_IDS)
    assert metric.average_allowed_per_48 == expected_average
    assert metric.sigma == expected_sigma
    team_metric = next(
        item
        for item in window.team_metrics[CANONICAL_TEAM_IDS[0]]
        if (item.base, item.slice_key, item.stat_key)
        == ("shot_zones", "Restricted Area", "FGM")
    )
    assert team_metric.rank == 1
    assert team_metric.sigma_deviation == (
        (CANONICAL_TEAM_IDS[0] - expected_average) / expected_sigma
    )


SEASON_COMPLETE_CUTOFF = "2025-10-16T04:00:00+00:00"


def _season_complete_query(
    tmp_path, *, window_games=None, unplayed_games=0, surface="shot_zones"
):
    """Query one surface whose snapshot was cut after the requested date."""

    engine = _engine(tmp_path)
    with engine.begin() as connection:
        _insert_publication_authority(
            connection,
            cutoff=datetime.fromisoformat(SEASON_COMPLETE_CUTOFF),
            manifest_id="season-complete-manifest",
            catalog_id="season-complete-catalog",
            unplayed_games=unplayed_games,
        )
    stream_key = {
        "shot_zones": (
            "exact_shot_zones_opponent_l15"
            if window_games is not None
            else "exact_shot_zones_opponent_season"
        ),
        "traditional": (
            "traditional_opponent_l15"
            if window_games is not None
            else "traditional_opponent_season"
        ),
    }[surface]
    window = TeamMatchupQueryService(
        TeamMatchupRepository(engine),
        publication_reader=_reader(
            cutoff_by_stream={stream_key: SEASON_COMPLETE_CUTOFF},
            freshness_by_stream={stream_key: "fresh"},
        ),
        l15_expectation_resolver=ActiveManifestLedgerGovernanceReader(engine),
    ).get_window(
        TeamMatchupSnapshotScope("2025-26", AS_OF, window_games=window_games)
    )
    return stream_key, next(
        item for item in window.observations if item.surface == surface
    ), window


def test_completed_season_exemption_covers_every_governed_base():
    # A ledger-derived season aggregate is one sum over the same finished set
    # of games, so it is as date-independent as the NBA-owned snapshots; the
    # window, not the owner, decides.
    from app.services.team_matchup_publications import (
        season_complete_snapshot_accepted,
    )

    for base in ("traditional", "assist_locations", "play_types", "shot_zones"):
        assert season_complete_snapshot_accepted(
            None, base=base, window="season", season_is_complete=True
        )
        assert not season_complete_snapshot_accepted(
            None, base=base, window="l15", season_is_complete=True
        )
        assert not season_complete_snapshot_accepted(
            None, base=base, window="season", season_is_complete=False
        )


def test_completed_season_serves_a_later_season_snapshot_with_its_reason(
    tmp_path,
):
    stream_key, observation, window = _season_complete_query(tmp_path)

    assert observation.status == "available"
    assert observation.unavailable_reason is None
    assert observation.publication == PublicationLineage(
        publication_id=f"publication-{stream_key}",
        cutoff=SEASON_COMPLETE_CUTOFF,
        freshness="fresh",
        version=2,
        reason="season_complete_snapshot",
    )
    assert any(metric.base == "shot_zones" for metric in window.league_metrics)
    # Surfaces read at a cutoff on or before the requested date are ordinary
    # reads and must not claim the completed-season exemption.
    shot_types = next(
        item for item in window.observations if item.surface == "shot_types"
    )
    assert shot_types.status == "available"
    assert shot_types.publication.reason is None


def test_a_strict_as_of_read_refuses_the_completed_season_exemption(tmp_path):
    """Scoring cannot borrow a snapshot cut after the date it asked about.

    The completed-season exemption is sound for hindsight display: the finished
    season's aggregate is the same for every date in it. That is exactly why it
    is unsound as a Matchup Score input for a game inside that season, because
    the aggregate contains the focal game.
    """

    engine = _engine(tmp_path)
    with engine.begin() as connection:
        _insert_publication_authority(
            connection,
            cutoff=datetime.fromisoformat(SEASON_COMPLETE_CUTOFF),
            manifest_id="season-complete-manifest",
            catalog_id="season-complete-catalog",
            unplayed_games=0,
        )
    stream_key = "exact_shot_zones_opponent_season"
    service = TeamMatchupQueryService(
        TeamMatchupRepository(engine),
        publication_reader=_reader(
            cutoff_by_stream={stream_key: SEASON_COMPLETE_CUTOFF},
            freshness_by_stream={stream_key: "fresh"},
        ),
        l15_expectation_resolver=ActiveManifestLedgerGovernanceReader(engine),
        clock=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    display = service.get_latest_window("2025-26", as_of=AS_OF)
    scoring = service.get_latest_window("2025-26", as_of=AS_OF, strict_as_of=True)

    display_observation = next(
        item for item in display.observations if item.surface == "shot_zones"
    )
    assert display_observation.status == "available"
    assert display_observation.publication.reason == "season_complete_snapshot"

    scoring_observation = next(
        item for item in scoring.observations if item.surface == "shot_zones"
    )
    assert scoring_observation.status == "unavailable"
    assert scoring_observation.unavailable_reason == "publication_cutoff_after_as_of"
    assert not any(
        metric.base == "shot_zones" for metric in scoring.league_metrics
    )


def test_a_later_season_snapshot_without_bound_governance_stays_withheld(
    tmp_path,
):
    """No governance at the snapshot's own cutoff proves nothing about it."""

    engine = _engine(tmp_path)
    stream_key = "exact_shot_zones_opponent_season"
    window = TeamMatchupQueryService(
        TeamMatchupRepository(engine),
        publication_reader=_reader(
            cutoff_by_stream={stream_key: SEASON_COMPLETE_CUTOFF},
            freshness_by_stream={stream_key: "fresh"},
        ),
        l15_expectation_resolver=ActiveManifestLedgerGovernanceReader(engine),
    ).get_window(TeamMatchupSnapshotScope("2025-26", AS_OF))

    observation = next(
        item for item in window.observations if item.surface == "shot_zones"
    )
    assert observation.status == "unavailable"
    assert observation.unavailable_reason == "publication_cutoff_after_as_of"


def test_completed_season_still_withholds_a_later_last_15_snapshot(tmp_path):
    _stream_key, observation, window = _season_complete_query(
        tmp_path, window_games=15
    )

    assert observation.status == "unavailable"
    assert observation.unavailable_reason == "publication_cutoff_after_as_of"
    assert observation.publication.reason is None
    assert not any(
        metric.base == "shot_zones" for metric in window.league_metrics
    )


def test_incomplete_season_still_withholds_a_later_season_snapshot(tmp_path):
    _stream_key, observation, window = _season_complete_query(
        tmp_path, unplayed_games=1
    )

    assert observation.status == "unavailable"
    assert observation.unavailable_reason == "publication_cutoff_after_as_of"
    assert observation.publication.reason is None
    assert not any(
        metric.base == "shot_zones" for metric in window.league_metrics
    )


def test_direct_publication_query_preserves_future_cutoff_failure_provenance(
    tmp_path,
):
    engine = _engine(tmp_path)
    repository = TeamMatchupRepository(engine)
    stream_key = "exact_shot_zones_opponent_season"
    future_cutoff = "2025-10-16T04:00:00+00:00"
    window = TeamMatchupQueryService(
        repository,
        publication_reader=_reader(
            cutoff_by_stream={stream_key: future_cutoff},
            freshness_by_stream={stream_key: "fresh"},
        ),
    ).get_window(TeamMatchupSnapshotScope("2025-26", AS_OF))

    observation = next(
        item for item in window.observations if item.surface == "shot_zones"
    )
    assert observation.status == "unavailable"
    assert observation.unavailable_reason == "publication_cutoff_after_as_of"
    assert observation.publication == PublicationLineage(
        publication_id=f"publication-{stream_key}",
        cutoff=future_cutoff,
        freshness="fresh",
        version=2,
    )
    assert not any(
        metric.base == "shot_zones" for metric in window.league_metrics
    )


def test_direct_query_rejects_fabricated_season_game_set_with_lineage(tmp_path):
    engine = _engine(tmp_path)
    repository = TeamMatchupRepository(engine)
    season_ids = {
        team_id: frozenset(game_ids)
        for team_id, game_ids in _season_game_ids_by_team().items()
    }
    _, l15_ids, _ = _governance(_canonical_league_games())
    wrong = dict(_season_game_ids_by_team())
    wrong[CANONICAL_TEAM_IDS[0]] = tuple(
        f"fabricated-{index}" for index in range(15)
    )
    stream_key = "exact_shot_zones_opponent_season"
    window = TeamMatchupQueryService(
        repository,
        publication_reader=_reader(
            game_ids_by_stream={stream_key: wrong},
        ),
        l15_expectation_resolver=_WindowGovernanceResolver(
            season=season_ids, l15=l15_ids
        ),
    ).get_window(TeamMatchupSnapshotScope("2025-26", AS_OF))

    observation = next(
        item for item in window.observations if item.surface == "shot_zones"
    )
    assert observation.status == "unavailable"
    assert observation.unavailable_reason == "publication_game_set_mismatch"
    assert observation.publication.publication_id == f"publication-{stream_key}"
    assert not any(
        metric.base == "shot_zones" for metric in window.league_metrics
    )


def test_l15_query_without_governance_fails_closed(tmp_path):
    engine = _engine(tmp_path)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    window = TeamMatchupQueryService(
        repository,
        publication_reader=_reader(governance_available=False),
    ).get_window(TeamMatchupSnapshotScope("2025-26", AS_OF, 15))

    assert not any(metric.base in {"play_types", "shot_types", "shot_zones"}
                   for metric in window.league_metrics)
    publication_observations = {
        item.surface: item
        for item in window.observations
        if item.surface in {"play_types", "shot_types", "shot_zones"}
    }
    assert set(publication_observations) == {"play_types", "shot_types", "shot_zones"}
    assert all(
        item.status == "unavailable"
        and item.unavailable_reason == "publication_governance_unavailable"
        for item in publication_observations.values()
    )


@pytest.mark.parametrize(
    "error",
    (
        TypeError("resolver implementation defect"),
        RuntimeError("resolver runtime failure"),
        OperationalError("governance query", {}, RuntimeError("database down")),
    ),
)
def test_query_propagates_unexpected_governance_failures(tmp_path, error):
    engine = _engine(tmp_path)
    repository = TeamMatchupRepository(engine)

    class BrokenResolver:
        def resolve_team_game_ids(self, *args, **kwargs):
            raise error

    query = TeamMatchupQueryService(
        repository,
        publication_reader=_reader(),
        l15_expectation_resolver=BrokenResolver(),
    )
    with pytest.raises(type(error)):
        query.get_window(TeamMatchupSnapshotScope("2025-26", AS_OF))


def test_l15_query_preserves_publication_game_set_failure_and_lineage(tmp_path):
    engine = _engine(tmp_path)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    expected = {
        team_id: frozenset(f"governed-{team_id}-{index}" for index in range(15))
        for team_id in CANONICAL_TEAM_IDS
    }
    wrong = dict(expected)
    wrong[CANONICAL_TEAM_IDS[0]] = frozenset(
        f"wrong-{index}" for index in range(15)
    )
    reader = _reader(
        game_ids_by_stream={
            stream_key: {
                team_id: tuple(game_ids)
                for team_id, game_ids in wrong.items()
            }
            for stream_key in (
                "synergy_play_types_opponent_l15",
                "grouped_shot_types_opponent_l15",
                "exact_shot_zones_opponent_l15",
            )
        }
    )
    window = TeamMatchupQueryService(
        repository,
        publication_reader=reader,
        l15_expectation_resolver=lambda season, cutoff: expected,
    ).get_window(TeamMatchupSnapshotScope("2025-26", AS_OF, 15))

    observation = next(
        item for item in window.observations if item.surface == "shot_zones"
    )
    assert observation.status == "unavailable"
    assert observation.unavailable_reason == "publication_game_set_mismatch"
    assert observation.publication.publication_id == (
        "publication-exact_shot_zones_opponent_l15"
    )
    assert not any(metric.base == "shot_zones" for metric in window.league_metrics)


def test_synergy_last_15_is_explicitly_unsupported(tmp_path):
    engine = _engine(tmp_path)
    games = _canonical_league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(
        engine, publication_write_capability=create_publication_write_capability(engine)
    )
    service = LedgerMatchupMaterializationService(
        ledger,
        repository,
        publication_reader=_reader(
            unavailable=frozenset({"synergy_play_types_opponent_l15"})
        ),
        clock=lambda: RETRIEVED_AT,
    )
    expected_game_ids, expected_l15_game_ids, team_ids = _governance(games)
    service.materialize(
        "2025-26",
        as_of=AS_OF,
        expected_game_ids=expected_game_ids,
        expected_l15_game_ids=expected_l15_game_ids,
        team_ids=team_ids,
    )

    snapshot = repository.get_snapshot(
        TeamMatchupSnapshotScope("2025-26", AS_OF, 15)
    )
    observation = next(
        item for item in snapshot.observations if item.surface == "play_types"
    )
    assert observation.status == "unavailable"
    assert observation.unavailable_reason == "provider_window_unsupported"
    assert not any(fact.base == "play_types" for fact in snapshot.facts)


def test_materialization_propagates_unexpected_governance_failure_but_degrades_expected(
    tmp_path,
):
    engine = _engine(tmp_path)
    games = _canonical_league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(
        engine,
        publication_write_capability=create_publication_write_capability(engine),
    )
    expected_game_ids, expected_l15_game_ids, team_ids = _governance(games)
    initial = LedgerMatchupMaterializationService(
        ledger, repository, publication_reader=_reader(), clock=lambda: RETRIEVED_AT
    )
    initial.materialize(
        "2025-26", as_of=AS_OF, expected_game_ids=expected_game_ids,
        expected_l15_game_ids=expected_l15_game_ids, team_ids=team_ids,
    )
    prior = repository.get_snapshot(
        TeamMatchupSnapshotScope("2025-26", AS_OF)
    )

    class InfrastructureFailure:
        def resolve_team_game_ids(self, *args, **kwargs):
            raise RuntimeError("governance database unavailable")

    broken = LedgerMatchupMaterializationService(
        ledger,
        repository,
        publication_reader=_reader(),
        l15_expectation_resolver=InfrastructureFailure(),
        clock=lambda: RETRIEVED_AT + timedelta(minutes=1),
    )
    with pytest.raises(RuntimeError, match="governance database unavailable"):
        broken.materialize(
            "2025-26", as_of=AS_OF, expected_game_ids=expected_game_ids,
            expected_l15_game_ids=expected_l15_game_ids, team_ids=team_ids,
        )
    assert repository.get_snapshot(
        TeamMatchupSnapshotScope("2025-26", AS_OF)
    ) == prior

    class ExpectedUnavailable:
        def resolve_team_game_ids(self, *args, **kwargs):
            raise PublicationGovernanceUnavailable()

    degraded = LedgerMatchupMaterializationService(
        ledger,
        repository,
        publication_reader=_reader(),
        l15_expectation_resolver=ExpectedUnavailable(),
        clock=lambda: RETRIEVED_AT + timedelta(minutes=2),
    )
    degraded.materialize(
        "2025-26", as_of=AS_OF, expected_game_ids=expected_game_ids,
        expected_l15_game_ids=expected_l15_game_ids, team_ids=team_ids,
    )
    publication_observations = {
        item.surface: item
        for item in repository.get_snapshot(
            TeamMatchupSnapshotScope("2025-26", AS_OF)
        ).observations
        if item.surface in {"play_types", "shot_types", "shot_zones"}
    }
    assert all(
        item.status == "unavailable"
        and item.unavailable_reason == "publication_governance_unavailable"
        for item in publication_observations.values()
    )


def test_database_reader_never_marks_nba_surface_as_legacy_fallback(tmp_path):
    engine = _engine(tmp_path)
    publications = PublicationService(engine, clock=lambda: RETRIEVED_AT)
    publications.register_stream(
        "exact_shot_zones_opponent_season",
        provider="nba_stats",
        owner="residential",
        required_observations=(),
        publication_strategy="replace",
        enabled=False,
    )

    read = DatabaseFirstPublicationReader(
        engine, clock=lambda: RETRIEVED_AT
    ).read("exact_shot_zones_opponent_season", season="2025-26")
    assert read.status == "unavailable"
    assert read.unavailable_reason == "publication_inactive"
    assert not read.legacy_fallback_allowed


def test_publication_lineage_cannot_bypass_the_legacy_write_fence(tmp_path):
    engine = _engine(tmp_path)

    class Fence:
        def __init__(self):
            self.calls = []

        def assert_writable(self, stream_key, *, connection=None):
            self.calls.append(stream_key)
            raise ValueError("legacy_write_fenced")

    fence = Fence()
    repository = TeamMatchupRepository(engine, write_fence=fence)
    lineage = PublicationLineage(
        publication_id="untrusted-publication",
        cutoff=AS_OF.isoformat(),
        freshness="fresh",
        version=1,
    )
    with pytest.raises(ValueError, match="legacy_write_fenced"):
        repository.replace_snapshots(
            (
                (
                    TeamMatchupSnapshotScope("2025-26", AS_OF),
                    [
                        TeamMatchupFact(
                            team_id=team_id,
                            base="shot_zones",
                            slice_key="Restricted Area",
                            stat_key="FGM",
                            raw_value=1,
                            denominator_value=48,
                            denominator_unit="minutes",
                            provider="nba_publication",
                            publication=lineage,
                        )
                        for team_id in CANONICAL_TEAM_IDS
                    ],
                    [TeamMatchupObservation(
                        "shot_zones", "available", publication=lineage
                    )],
                ),
            ),
            retrieved_at=RETRIEVED_AT,
        )
    assert fence.calls == ["exact_shot_zones_opponent_season"]


def test_governed_publication_write_requires_and_checks_its_capability(tmp_path):
    engine, operations, stream_key, candidate_id, cutoff = _operator_activation_fixture(
        tmp_path
    )
    operations.activate_stream(
        stream_key,
        actor="operator",
        reason="activate reviewed candidate",
        season="2025-26",
        cutoff=cutoff,
        candidate_publication_id=candidate_id,
    )

    class Fence:
        def assert_writable(self, stream_key, *, connection=None):
            raise ValueError("legacy_write_fenced")

    lineage = PublicationLineage(candidate_id, cutoff.isoformat(), "fresh", 1)
    governed_game_ids = {
        team_id: tuple(
            f"governed-{team_id}-{index}" for index in range(15)
        )
        for team_id in CANONICAL_TEAM_IDS
    }
    snapshots = (
        (
            TeamMatchupSnapshotScope("2025-26", AS_OF, 15),
            [
                TeamMatchupFact(
                    team_id=team_id,
                    base="shot_zones",
                    slice_key=publication_metric_identity("shot_zones", metric_key)[0],
                    stat_key=publication_metric_identity("shot_zones", metric_key)[1],
                    raw_value=1,
                    denominator_value=48,
                    denominator_unit="minutes",
                    provider="nba_publication",
                    game_ids=governed_game_ids[team_id],
                    publication=lineage,
                )
                for team_id in CANONICAL_TEAM_IDS
                for metric_key in sorted(NBA_PUBLICATION_TAXONOMY["shot_zones"])
            ],
            [TeamMatchupObservation(
                "shot_zones",
                "available",
                game_ids=tuple(sorted({
                    game_id
                    for game_ids in governed_game_ids.values()
                    for game_id in game_ids
                })),
                publication=lineage,
            )],
        ),
    )
    unguarded_repository = TeamMatchupRepository(engine, write_fence=Fence())
    with pytest.raises(ValueError, match="publication_write_capability_required"):
        unguarded_repository.replace_governed_publication_snapshots(
            snapshots,
            retrieved_at=RETRIEVED_AT,
        )

    read_only_repository = TeamMatchupRepository(engine)
    with pytest.raises(ValueError, match="publication_write_capability_required"):
        read_only_repository.replace_governed_publication_snapshots(
            snapshots,
            retrieved_at=RETRIEVED_AT,
        )

    repository = TeamMatchupRepository(
        engine,
        write_fence=Fence(),
        publication_write_capability=create_publication_write_capability(engine),
    )
    authorization_queries = []

    def count_authorization_queries(
        connection, cursor, statement, parameters, context, executemany
    ):
        lowered = statement.lower()
        if "publication_versions" in lowered or "publication_pointers" in lowered:
            authorization_queries.append(statement)

    event.listen(engine, "before_cursor_execute", count_authorization_queries)
    try:
        repository.replace_governed_publication_snapshots(
            snapshots,
            retrieved_at=RETRIEVED_AT,
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_authorization_queries)
    assert len(authorization_queries) == 2
    assert repository.get_snapshot(
        TeamMatchupSnapshotScope("2025-26", AS_OF, 15)
    ).facts

    scope, facts, observations = snapshots[0]
    with engine.begin() as connection:
        encoded = connection.execute(
            select(PublicationVersion.payload).where(
                PublicationVersion.publication_id == candidate_id
            )
        ).scalar_one()
        payload = json.loads(encoded)
        for row in payload["rows"]:
            row["per48"] = {key: 0.007 for key in row["per48"]}
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        connection.execute(
            PublicationVersion.__table__.update().where(
                PublicationVersion.publication_id == candidate_id
            ).values(
                payload=encoded,
                checksum=hashlib.sha256(encoded.encode()).hexdigest(),
            )
        )
    transported = tuple(replace(fact, raw_value=0.007) for fact in facts)
    repository.replace_governed_publication_snapshots(
        ((scope, transported, observations),),
        retrieved_at=RETRIEVED_AT,
    )

    alternate_id = "alternate-valid-publication"
    with engine.begin() as connection:
        connection.execute(PublicationVersion.__table__.insert().values(
            **_publication_authority_values(),
            publication_id=alternate_id,
            stream_key=stream_key,
            season="2025-26",
            cutoff=cutoff,
            version=2,
            status="candidate",
            checksum=hashlib.sha256(encoded.encode()).hexdigest(),
            payload=encoded,
            created_at=cutoff,
            fence=0,
        ))
    alternate_lineage = PublicationLineage(
        alternate_id, cutoff.isoformat(), "fresh", 2
    )
    mixed = (
        replace(transported[0], publication=alternate_lineage),
        *transported[1:],
    )
    with pytest.raises(ValueError, match="publication_write_context_invalid"):
        repository.replace_governed_publication_snapshots(
            ((scope, mixed, observations),),
            retrieved_at=RETRIEVED_AT,
        )

    invented = (replace(transported[0], raw_value=999), *transported[1:])
    with pytest.raises(ValueError, match="publication_write_context_invalid"):
        repository.replace_governed_publication_snapshots(
            ((scope, invented, observations),),
            retrieved_at=RETRIEVED_AT,
        )
