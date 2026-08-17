"""NBA-owned team matchup publications compose into the persisted read model."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from app.services.database_first_activation import (
    DatabaseFirstPublicationReader,
    PublicationRead,
    PublicationReadSnapshot,
    PublicationTeamWindowRow,
)
from app.services.collection_control import PublicationService
from app.services.ledger_matchup_materialization import (
    LedgerMatchupMaterializationService,
)
from app.services.team_matchup_query import TeamMatchupQueryService
from app.services.team_matchup_repository import (
    TeamMatchupFact,
    TeamMatchupRepository,
    TeamMatchupObservation,
    TeamMatchupSnapshotScope,
)
from tests.services.test_ledger_derivations import _league_games
from tests.services.test_ledger_matchup_materialization import _governance


AS_OF = date(2025, 10, 15)
RETRIEVED_AT = datetime(2025, 10, 16, 10, tzinfo=timezone.utc)


def _engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'nba-publications.sqlite3'}")
    run_migrations(engine)
    return engine


def _rows(metric_key: str | tuple[str, ...]) -> tuple[PublicationTeamWindowRow, ...]:
    metric_keys = (metric_key,) if isinstance(metric_key, str) else metric_key
    return tuple(
        PublicationTeamWindowRow(
            team_id=team_id,
            team_tricode=f"T{team_id:02d}",
            game_ids=(f"game-{team_id}",),
            game_count=1,
            per48={
                key: float(team_id + index)
                for index, key in enumerate(
                    metric_keys if team_id % 2 else reversed(metric_keys)
                )
            },
            league_average={key: 15.5 for key in metric_keys},
            population_sigma={key: 8.655 for key in metric_keys},
            competition_rank={key: team_id for key in metric_keys},
        )
        for team_id in range(1, 31)
    )


def _reader(
    *,
    unavailable: frozenset[str] = frozenset(),
    metric_keys_by_stream: dict[str, str | tuple[str, ...]] | None = None,
    cutoff_by_stream: dict[str, str] | None = None,
    freshness_by_stream: dict[str, str] | None = None,
):
    metrics = {
        "synergy_play_types_opponent_season": "Transition_PTS",
        "synergy_play_types_opponent_l15": "Transition_PTS",
        "grouped_shot_types_opponent_season": "catch_and_shoot_FGA",
        "grouped_shot_types_opponent_l15": "catch_and_shoot_FGA",
        "exact_shot_zones_opponent_season": "Restricted Area_FGM",
        "exact_shot_zones_opponent_l15": "Restricted Area_FGM",
    }
    reads = {}
    for stream_key, metric_key in metrics.items():
        if stream_key in unavailable:
            reads[stream_key] = PublicationRead(
                stream_key=stream_key,
                publication_id=None,
                season="2025-26",
                cutoff=None,
                version=None,
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
            decoded=_rows((metric_keys_by_stream or {}).get(stream_key, metric_key)),
        )
    for stream_key in (
        "traditional_opponent_season",
        "assist_locations_season",
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
    games = _league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(engine)
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
                    for team_id in range(1, 31)
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
    assert fact.game_ids == ("game-1",)
    shot_type = next(item for item in snapshot.facts if item.base == "shot_types")
    assert (shot_type.slice_key, shot_type.stat_key) == (
        "catch_and_shoot",
        "FGA",
    )
    observations = {item.surface: item for item in snapshot.observations}
    assert observations["shot_zones"].status == "available"
    assert observations["shot_zones"].publication == fact.publication
    assert observations["traditional"].status == "available"

    window = TeamMatchupQueryService(repository).get_window(snapshot.scope)
    assert any(metric.base == "shot_zones" for metric in window.league_metrics)
    assert any(
        (metric.base, metric.slice_key, metric.stat_key)
        == ("shot_types", "catch_and_shoot", "FGA")
        for metric in window.league_metrics
    )

    publication_window = TeamMatchupQueryService(
        repository,
        publication_reader=_reader(),
    ).get_window(snapshot.scope)
    assert any(
        (metric.base, metric.slice_key, metric.stat_key)
        == ("shot_types", "catch_and_shoot", "FGA")
        for metric in publication_window.league_metrics
    )


def test_publication_per48_values_and_metric_key_order_are_preserved(tmp_path):
    engine = _engine(tmp_path)
    games = _league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(engine)
    service = LedgerMatchupMaterializationService(
        ledger,
        repository,
        publication_reader=_reader(
            metric_keys_by_stream={
                "grouped_shot_types_opponent_season": (
                    "catch_and_shoot_FGA",
                    "catch_and_shoot_FGM",
                )
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
        if item.base == "shot_types" and item.stat_key == "FGA"
    )
    assert fact.raw_value == 1.0
    assert fact.denominator_value == 48.0
    window = TeamMatchupQueryService(repository).get_window(snapshot.scope)
    metric = next(
        item
        for item in window.team_metrics[1]
        if item.base == "shot_types" and item.stat_key == "FGA"
    )
    assert metric.allowed_per_48 == 1.0
    assert {
        item.stat_key for item in window.league_metrics if item.base == "shot_types"
    } == {"FGA", "FGM"}


def test_nba_unavailable_surfaces_do_not_retain_pbp_fallback(tmp_path):
    engine = _engine(tmp_path)
    games = _league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(engine)
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
                    for team_id in range(1, 31)
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
    games = _league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(engine)
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
    repository = TeamMatchupRepository(engine)
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


def test_synergy_last_15_is_explicitly_unsupported(tmp_path):
    engine = _engine(tmp_path)
    games = _league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)
    repository = TeamMatchupRepository(engine)
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


def test_publication_lineage_is_not_rejected_by_the_legacy_write_fence(tmp_path):
    engine = _engine(tmp_path)
    games = _league_games()
    ledger = CanonicalGameLedgerRepository(engine)
    ledger.replace_games_atomic(games)

    class Fence:
        def __init__(self):
            self.calls = []

        def assert_writable(self, stream_key, *, connection=None):
            self.calls.append(stream_key)
            if stream_key in {
                "synergy_play_types_opponent_season",
                "synergy_play_types_opponent_l15",
                "grouped_shot_types_opponent_season",
                "grouped_shot_types_opponent_l15",
                "exact_shot_zones_opponent_season",
                "exact_shot_zones_opponent_l15",
            }:
                raise AssertionError("publication-backed writes must bypass legacy fence")

    fence = Fence()
    repository = TeamMatchupRepository(engine, write_fence=fence)
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
    assert set(fence.calls) == {
        "traditional_opponent",
        "traditional_opponent_season",
        "traditional_opponent_l15",
        "assist_locations",
        "assist_locations_season",
        "assist_locations_l15",
    }
