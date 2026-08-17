"""Window-aware team matchup persistence and query contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
import pandas as pd
from sqlalchemy import create_engine, delete, inspect, update
from sqlalchemy.exc import IntegrityError

from app.migrations import run_migrations
from app.models.team_matchup import (
    TeamMatchupFactRow,
    TeamMatchupSurfaceObservationRow,
)
from app.services.team_matchup_query import TeamMatchupQueryService
from app.services.team_matchup_repository import (
    TeamMatchupFact,
    TeamMatchupObservation,
    TeamMatchupRepository,
    TeamMatchupSnapshotScope,
)
from app.services.team_matchup_refresh import (
    TeamMatchupRefreshService,
    TeamWindowBoundaryResolver,
)
from app.utils.telemetry import ProviderResponseError


BOS = 1610612738
NYK = 1610612752


class FakeEventCatalog:
    def __init__(self, events):
        self.events = events
        self.calls = 0

    def get_events(self, season):
        assert season == "2024-25"
        self.calls += 1
        return list(self.events)


def _event(
    game_number: int,
    played_on: date,
    *,
    home_team_id: int = BOS,
    away_team_id: int = NYK,
    status_code: int = 3,
    classification: str = "Regular Season",
    postponed: bool = False,
):
    return {
        "nba_game_id": f"002240{game_number:04d}",
        "season": "2024-25",
        "scheduled_at": datetime.combine(played_on, datetime.min.time(), timezone.utc)
        .replace(hour=12)
        .isoformat(),
        "home_team_id": home_team_id,
        "away_team_id": away_team_id,
        "status_code": status_code,
        "status_text": "Final" if status_code == 3 else "Scheduled",
        "classification": classification,
        "postponed_status": "Postponed" if postponed else None,
        "postponement_evidence": {"reason": "arena"} if postponed else None,
        "is_postponed": postponed,
    }


class _FakeMatchupNBA:
    def __init__(self, team_ids, *, rolling_games=15):
        self.team_ids = tuple(team_ids)
        self.rolling_games = rolling_games
        self.calls = []

    def _teams(self, team_id):
        return [team_id] if team_id is not None else self.team_ids

    def fetch_opponent_team_stats(self, date_from, **kwargs):
        self.calls.append(("traditional", date_from, kwargs))
        games = self.rolling_games if kwargs["last_n_games"] else 82
        return pd.DataFrame(
            [
                {
                    "TEAM_ID": team_id,
                    "TEAM_NAME": f"Team {team_id}",
                    "GP": games,
                    "MIN": games * 48,
                    "OPP_REB": 200 + index,
                    "OPP_TOV": 100 + index,
                    "OPP_STL": 50 + index,
                    "OPP_BLK": 25 + index,
                }
                for index, team_id in enumerate(self._teams(kwargs["team_id"]))
            ]
        )

    def fetch_opponent_shot_chart(self, general_range, date_from, **kwargs):
        self.calls.append(("shot_types", general_range, date_from, kwargs))
        games = self.rolling_games if kwargs["last_n_games"] else 82
        return pd.DataFrame(
            [
                {
                    "TEAM_ID": team_id,
                    "TEAM_NAME": f"Team {team_id}",
                    "GP": games,
                    "FG2M": 200 + index,
                    "FG2A": 400 + index,
                    "FG3M": 100 + index,
                    "FG3A": 300 + index,
                }
                for index, team_id in enumerate(self._teams(kwargs["team_id"]))
            ]
        )

    def fetch_opponent_shooting_zone(self, date_from, **kwargs):
        self.calls.append(("shot_zones", date_from, kwargs))
        return pd.DataFrame(
            [
                {
                    "TEAM_ID": team_id,
                    "TEAM_NAME": f"Team {team_id}",
                    "Restricted Area_OPP_FGM": 200 + index,
                    "Restricted Area_OPP_FGA": 350 + index,
                    "Backcourt_OPP_FGM": 1,
                    "Backcourt_OPP_FGA": 2,
                }
                for index, team_id in enumerate(self._teams(kwargs["team_id"]))
            ]
        )

    def fetch_synergy_play_types(self, play_type, **kwargs):
        self.calls.append(("play_types", play_type, kwargs))
        return pd.DataFrame(
            [
                {
                    "TEAM_ID": team_id,
                    "TEAM_NAME": f"Team {team_id}",
                    "GP": 82,
                    "PTS": 400 + index,
                    "POSS": 200 + index,
                }
                for index, team_id in enumerate(self.team_ids)
            ]
        )


class _FakeMatchupPBP:
    def __init__(self, team_ids):
        self.team_ids = tuple(team_ids)
        self.calls = []

    def fetch_totals_frame(self, data_type, **kwargs):
        assert data_type == "opponent"
        self.calls.append(kwargs)
        team_ids = self.team_ids if kwargs["team_id"] is None else [kwargs["team_id"]]
        games = 82 if kwargs["team_id"] is None else 15
        return pd.DataFrame(
            [
                {
                    "TeamId": team_id,
                    "Name": f"Team {team_id}",
                    "GamesPlayed": games,
                    "SecondsPlayed": games * 48 * 60,
                    "Assists": 1000 + index,
                    "Arc3Assists": 200 + index,
                    "Corner3Assists": 100 + index,
                    "AtRimAssists": 300 + index,
                    "ShortMidRangeAssists": 250 + index,
                    "LongMidRangeAssists": 150 + index,
                }
                for index, team_id in enumerate(team_ids)
            ]
        )


def _fixture_team_ids():
    return [
        row["team_id"]
        for row in json.loads(
            (
                Path(__file__).parents[1]
                / "fixtures"
                / "team_matchups"
                / "thirty_teams.json"
            ).read_text()
        )
    ]


def _complete_traditional_facts(value=10):
    return [
        TeamMatchupFact(
            team_id=team_id,
            base="traditional",
            slice_key="OPP_TOV",
            stat_key="OPP_TOV",
            raw_value=value,
            denominator_value=48,
            denominator_unit="minutes",
            provider="nba_stats",
        )
        for team_id in _fixture_team_ids()
    ]


def _publish_snapshot_batch(
    repository,
    scope,
    *,
    facts,
    observations,
    retrieved_at,
):
    repository.replace_snapshots(
        ((scope, facts, observations),),
        retrieved_at=retrieved_at,
    )


class _CountingTeamMatchupRepository(TeamMatchupRepository):
    def __init__(self, engine):
        super().__init__(engine)
        self.snapshot_calls = []

    def get_snapshot(self, scope):
        self.snapshot_calls.append(scope)
        return super().get_snapshot(scope)


def _one_game_for_every_team(played_on: date) -> list[dict]:
    team_ids = _fixture_team_ids()
    return [
        _event(
            pair_index // 2 + 1,
            played_on,
            home_team_id=team_ids[pair_index],
            away_team_id=team_ids[pair_index + 1],
        )
        for pair_index in range(0, 30, 2)
    ]


def test_matchup_repository_refuses_the_read_only_demo_database():
    with pytest.raises(ValueError, match="demo database"):
        TeamMatchupRepository(create_engine("sqlite:///nba_play_types.db"))


@pytest.mark.parametrize(
    ("collaborator", "message"),
    [
        ("repository", "repository"),
        ("event_catalog", "event_catalog"),
        ("nba_stats_provider", "nba_stats_provider"),
        ("pbp_stats_provider", "pbp_stats_provider"),
    ],
)
def test_refresh_service_constructor_validates_each_collaborator_independently(
    tmp_path,
    collaborator,
    message,
):
    team_ids = _fixture_team_ids()
    repository = TeamMatchupRepository(
        create_engine(f"sqlite:///{tmp_path / 'constructor.sqlite3'}")
    )
    catalog = FakeEventCatalog(_one_game_for_every_team(date(2025, 4, 15)))
    nba = _FakeMatchupNBA(team_ids)
    pbp = _FakeMatchupPBP(team_ids)
    arguments = {
        "repository": repository,
        "event_catalog": catalog,
        "nba_stats_provider": nba,
        "pbp_stats_provider": pbp,
    }
    arguments[collaborator] = object()

    with pytest.raises(TypeError, match=message):
        TeamMatchupRefreshService(**arguments)


def test_refresh_service_constructor_requires_named_collaborators(tmp_path):
    team_ids = _fixture_team_ids()
    repository = TeamMatchupRepository(
        create_engine(f"sqlite:///{tmp_path / 'constructor.sqlite3'}")
    )
    catalog = FakeEventCatalog(_one_game_for_every_team(date(2025, 4, 15)))
    nba = _FakeMatchupNBA(team_ids)
    pbp = _FakeMatchupPBP(team_ids)

    with pytest.raises(TypeError):
        TeamMatchupRefreshService(repository, catalog, nba, pbp)


def test_team_last_15_boundary_uses_only_completed_governed_games():
    as_of = date(2025, 4, 15)
    governed = [
        _event(index, date(2025, 3, 1) + timedelta(days=index - 1))
        for index in range(1, 16)
    ]
    excluded = [
        _event(101, date(2025, 4, 12), postponed=True),
        _event(102, date(2025, 4, 13), classification="Preseason"),
        _event(103, date(2025, 4, 14), classification="All-Star"),
        _event(104, date(2025, 4, 15), status_code=1),
        _event(105, date(2025, 4, 16)),
    ]

    boundaries = TeamWindowBoundaryResolver().last_n(
        governed + excluded, as_of=as_of, window_games=15
    )

    assert boundaries[BOS].from_date == date(2025, 3, 1)
    assert boundaries[BOS].to_date == as_of
    assert boundaries[BOS].season_type == "Regular Season"
    assert boundaries[BOS].game_ids == tuple(
        f"002240{index:04d}" for index in range(15, 0, -1)
    )


def test_team_last_15_boundary_keeps_cross_phase_games_but_marks_them_unrepresentable():
    events = [
        _event(index, date(2025, 4, index), classification="Regular Season")
        for index in range(1, 3)
    ]
    for index in range(3, 16):
        event = _event(index, date(2025, 4, index), classification="Playoffs")
        event["nba_game_id"] = f"004240{index:04d}"
        events.append(event)

    boundary = TeamWindowBoundaryResolver().last_n(
        events, as_of=date(2025, 4, 15), window_games=15
    )[BOS]

    assert len(boundary.game_ids) == 15
    assert boundary.season_type is None


def test_refresh_rejects_a_future_as_of_before_provider_or_storage_work(tmp_path):
    team_ids = _fixture_team_ids()
    events = [
        _event(
            pair_index + 1,
            date(2025, 4, 15),
            home_team_id=team_ids[pair_index],
            away_team_id=team_ids[pair_index + 1],
        )
        for pair_index in range(0, 30, 2)
    ]
    engine = create_engine(f"sqlite:///{tmp_path / 'future.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    nba = _FakeMatchupNBA(team_ids)
    pbp = _FakeMatchupPBP(team_ids)
    catalog = FakeEventCatalog(events)
    service = TeamMatchupRefreshService(
        repository=repository,
        event_catalog=catalog,
        nba_stats_provider=nba,
        pbp_stats_provider=pbp,
        clock=lambda: datetime(2025, 4, 15, 16, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="future as_of"):
        service.refresh("2024-25", as_of=date(2025, 4, 16))

    assert nba.calls == []
    assert pbp.calls == []
    assert repository.get_latest_scope("2024-25") is None


@pytest.mark.parametrize(
    "provider_row",
    (
        {"TEAM_ID": BOS, "GP": 14},
        {"TEAM_ID": BOS, "GP": 15, "GAME_IDS": ["stale-game"]},
    ),
)
def test_provider_aggregate_must_prove_immutable_window_identity(provider_row):
    with pytest.raises(ValueError, match="provider"):
        TeamMatchupRefreshService._verify_aggregate_window(
            pd.DataFrame([provider_row]),
            expected_game_counts={BOS: 15},
            expected_game_ids_by_team={BOS: tuple(f"game-{index}" for index in range(15))},
        )


def test_repository_rejects_a_future_scope_without_polluting_latest(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'future-write.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    current_scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)
    _publish_snapshot_batch(
        repository,
        current_scope,
        facts=_complete_traditional_facts(),
        observations=[TeamMatchupObservation("traditional", "available")],
        retrieved_at=datetime(2025, 4, 15, 16, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="future as_of"):
        _publish_snapshot_batch(
            repository,
            TeamMatchupSnapshotScope("2024-25", date(2025, 4, 16), 15),
            facts=[],
            observations=[
                TeamMatchupObservation("traditional", "missing", "bad_clock")
            ],
            retrieved_at=datetime(2025, 4, 15, 16, tzinfo=timezone.utc),
        )

    assert (
        repository.get_latest_scope(
            "2024-25", window_games=15, as_of=date(2025, 4, 15)
        )
        == current_scope
    )


def test_refresh_records_an_incomplete_governed_roster_without_failing_nightly(
    tmp_path,
):
    events = [_event(1, date(2025, 4, 15))]
    engine = create_engine(f"sqlite:///{tmp_path / 'roster-missing.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    nba = _FakeMatchupNBA((BOS, NYK))
    pbp = _FakeMatchupPBP((BOS, NYK))
    catalog = FakeEventCatalog(events)
    service = TeamMatchupRefreshService(
        repository=repository,
        event_catalog=catalog,
        nba_stats_provider=nba,
        pbp_stats_provider=pbp,
        clock=lambda: datetime(2025, 4, 15, 16, tzinfo=timezone.utc),
    )

    service.refresh("2024-25", as_of=date(2025, 4, 15))

    assert catalog.calls == 1
    for window_games in (None, 15):
        snapshot = repository.get_snapshot(
            TeamMatchupSnapshotScope(
                "2024-25", date(2025, 4, 15), window_games
            )
        )
        assert snapshot.facts == ()
        assert {
            (item.status, item.unavailable_reason)
            for item in snapshot.observations
        } == {("missing", "governed_team_roster_incomplete")}
    assert nba.calls == []
    assert pbp.calls == []


def test_migration_012_stores_window_ready_facts_and_surface_observations(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'matchups.sqlite3'}")

    result = run_migrations(engine)

    assert "012_create_team_matchup_facts" in result.applied
    assert {
        "team_matchup_facts",
        "team_matchup_surface_observations",
    }.issubset(inspect(engine).get_table_names())
    fact_columns = {
        column["name"] for column in inspect(engine).get_columns("team_matchup_facts")
    }
    assert {
        "id",
        "season",
        "as_of_date",
        "window_kind",
        "window_games",
        "team_id",
        "base",
        "slice_key",
        "stat_key",
        "raw_value",
        "denominator_value",
        "denominator_unit",
        "provider",
        "window_start_date",
        "window_end_date",
        "retrieved_at",
    } <= fact_columns
    # Migration 034 and the correction-propagation upgrade add lineage to the
    # migration-012 read model.  Keep this assertion additive so future
    # schema-compatible columns do not make the migration smoke test brittle.
    assert {
        "game_ids",
        "ledger_checksum",
        "source_observation_ids",
        "game_set_checksum",
        "cutoff",
        "recomposition_reason",
        "publication_id",
        "publication_cutoff",
        "publication_freshness",
        "publication_version",
    } <= fact_columns


def test_stored_raw_facts_produce_deterministic_30_team_league_metrics(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'matchups.sqlite3'}")
    run_migrations(engine)
    fixture_path = (
        Path(__file__).parents[1] / "fixtures" / "team_matchups" / "thirty_teams.json"
    )
    rows = json.loads(fixture_path.read_text())
    scope = TeamMatchupSnapshotScope(
        season="2024-25", as_of=date(2025, 4, 15), window_games=15
    )
    facts = [
        TeamMatchupFact(
            team_id=row["team_id"],
            base="traditional",
            slice_key="OPP_TOV",
            stat_key="OPP_TOV",
            raw_value=row["allowed"] * 100,
            denominator_value=4800,
            denominator_unit="minutes",
            provider="nba_stats",
        )
        for row in rows
    ]
    repository = TeamMatchupRepository(engine)
    retrieved_at = datetime(2025, 4, 16, 10, tzinfo=timezone.utc)
    _publish_snapshot_batch(
        repository,
        scope,
        facts=facts,
        observations=[
            TeamMatchupObservation(surface="traditional", status="available")
        ],
        retrieved_at=retrieved_at,
    )

    window = TeamMatchupQueryService(repository).get_window(scope)

    league = window.league_metrics[0]
    assert league.team_count == 30
    assert league.average_allowed_per_48 == pytest.approx(15.5)
    assert league.sigma == pytest.approx(8.6554414484)
    best = window.team_metrics[1610612737][0]
    assert best.allowed_per_48 == pytest.approx(1)
    assert best.percent_vs_league_average == pytest.approx(-93.5483870968)
    assert best.sigma_deviation == pytest.approx(-1.6752467319)
    assert best.rank == 1
    assert window.observations[0].retrieved_at == retrieved_at


def test_zero_league_average_has_an_honest_unavailable_percent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'zero-average.sqlite3'}")
    run_migrations(engine)
    rows = json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures"
            / "team_matchups"
            / "thirty_teams.json"
        ).read_text()
    )
    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)
    repository = TeamMatchupRepository(engine)
    _publish_snapshot_batch(
        repository,
        scope,
        facts=[
            TeamMatchupFact(
                team_id=row["team_id"],
                base="traditional",
                slice_key="OPP_BLK",
                stat_key="OPP_BLK",
                raw_value=0,
                denominator_value=480,
                denominator_unit="minutes",
                provider="nba_stats",
            )
            for row in rows
        ],
        observations=[TeamMatchupObservation("traditional", "available")],
        retrieved_at=datetime(2025, 4, 16, 10, tzinfo=timezone.utc),
    )

    window = TeamMatchupQueryService(repository).get_window(scope)

    assert window.league_metrics[0].average_allowed_per_48 == 0
    assert window.league_metrics[0].sigma == 0
    assert all(
        metric.percent_vs_league_average is None
        and metric.sigma_deviation == 0
        for metrics in window.team_metrics.values()
        for metric in metrics
    )


def test_matchup_facts_downgrade_game_denominators_mislabeled_as_per_48(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'games-denominator.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15))

    facts = _complete_traditional_facts()
    facts[0] = TeamMatchupFact(
        team_id=facts[0].team_id,
        base="traditional",
        slice_key="OPP_TOV",
        stat_key="OPP_TOV",
        raw_value=100,
        denominator_value=10,
        denominator_unit="games",
        provider="nba_stats",
    )

    _publish_snapshot_batch(
        repository,
        scope,
        facts=facts,
        observations=[TeamMatchupObservation("traditional", "available")],
        retrieved_at=datetime(2025, 4, 16, 10, tzinfo=timezone.utc),
    )

    snapshot = repository.get_snapshot(scope)
    assert snapshot.facts == ()
    assert snapshot.observations[0].status == "unavailable"
    assert snapshot.observations[0].unavailable_reason == "provider_invalid_numeric"


def test_repository_downgrades_a_partial_available_surface_before_write(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'partial-write.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)

    _publish_snapshot_batch(
        repository,
        scope,
        facts=[
            TeamMatchupFact(
                team_id=team_id,
                base="traditional",
                slice_key="OPP_TOV",
                stat_key="OPP_TOV",
                raw_value=10,
                denominator_value=48,
                denominator_unit="minutes",
                provider="nba_stats",
            )
            for team_id in _fixture_team_ids()[:29]
        ],
        observations=[TeamMatchupObservation("traditional", "available")],
        retrieved_at=datetime(2025, 4, 16, 10, tzinfo=timezone.utc),
    )

    snapshot = repository.get_snapshot(scope)
    assert snapshot.facts == ()
    assert snapshot.observations[0].status == "unavailable"
    assert snapshot.observations[0].unavailable_reason == "surface_incomplete"


def test_repository_rejects_non_finite_surface_without_replacing_valid_facts(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'non-finite.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)
    observation = TeamMatchupObservation("traditional", "available")
    valid_facts = _complete_traditional_facts(value=10)
    _publish_snapshot_batch(
        repository,
        scope,
        facts=valid_facts,
        observations=[observation],
        retrieved_at=datetime(2025, 4, 16, 10, tzinfo=timezone.utc),
    )
    invalid_facts = list(_complete_traditional_facts(value=20))
    invalid_facts[0] = replace(invalid_facts[0], raw_value=float("nan"))

    _publish_snapshot_batch(
        repository,
        scope,
        facts=invalid_facts,
        observations=[observation],
        retrieved_at=datetime(2025, 4, 16, 11, tzinfo=timezone.utc),
    )

    snapshot = repository.get_snapshot(scope)
    assert {fact.raw_value for fact in snapshot.facts} == {10}
    assert snapshot.observations[0].status == "unavailable"
    assert snapshot.observations[0].unavailable_reason == "provider_invalid_numeric"


def test_query_degrades_only_a_legacy_incomplete_surface(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-partial.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)
    assist_facts = [
        TeamMatchupFact(
            team_id=team_id,
            base="assist_locations",
            slice_key="Assists",
            stat_key="Assists",
            raw_value=20,
            denominator_value=48,
            denominator_unit="minutes",
            provider="pbp_stats",
        )
        for team_id in _fixture_team_ids()
    ]
    _publish_snapshot_batch(
        repository,
        scope,
        facts=[*_complete_traditional_facts(), *assist_facts],
        observations=[
            TeamMatchupObservation("traditional", "available"),
            TeamMatchupObservation("assist_locations", "available"),
        ],
        retrieved_at=datetime(2025, 4, 16, 10, tzinfo=timezone.utc),
    )
    with engine.begin() as connection:
        connection.execute(
            delete(TeamMatchupFactRow.__table__).where(
                TeamMatchupFactRow.__table__.c.base == "traditional",
                TeamMatchupFactRow.__table__.c.team_id == _fixture_team_ids()[0],
            )
        )
        connection.execute(
            delete(TeamMatchupSurfaceObservationRow.__table__).where(
                TeamMatchupSurfaceObservationRow.__table__.c.surface
                == "traditional"
            )
        )

    window = TeamMatchupQueryService(repository).get_window(scope)

    assert {metric.base for metric in window.league_metrics} == {
        "assist_locations"
    }
    observations = {item.surface: item for item in window.observations}
    assert observations["assist_locations"].status == "available"
    assert observations["traditional"].status == "unavailable"
    assert (
        observations["traditional"].unavailable_reason
        == "legacy_surface_incomplete"
    )


def test_query_keeps_invalid_numeric_as_the_causal_surface_reason(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-invalid-numeric.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)
    assist_facts = [
        TeamMatchupFact(
            team_id=team_id,
            base="assist_locations",
            slice_key="Assists",
            stat_key="Assists",
            raw_value=20,
            denominator_value=48,
            denominator_unit="minutes",
            provider="pbp_stats",
        )
        for team_id in _fixture_team_ids()
    ]
    _publish_snapshot_batch(
        repository,
        scope,
        facts=[*_complete_traditional_facts(), *assist_facts],
        observations=[
            TeamMatchupObservation("traditional", "available"),
            TeamMatchupObservation("assist_locations", "available"),
        ],
        retrieved_at=datetime(2025, 4, 16, 10, tzinfo=timezone.utc),
    )
    with engine.begin() as connection:
        connection.execute(
            update(TeamMatchupFactRow.__table__)
            .where(
                TeamMatchupFactRow.__table__.c.base == "traditional",
                TeamMatchupFactRow.__table__.c.team_id == _fixture_team_ids()[0],
            )
            .values(denominator_value=0)
        )

    window = TeamMatchupQueryService(repository).get_window(scope)

    assert {metric.base for metric in window.league_metrics} == {
        "assist_locations"
    }
    observations = {item.surface: item for item in window.observations}
    assert observations["assist_locations"].status == "available"
    assert observations["traditional"].status == "unavailable"
    assert (
        observations["traditional"].unavailable_reason
        == "provider_invalid_numeric"
    )


def test_refresh_collects_exact_supported_windows_and_marks_synergy_unsupported(
    tmp_path,
):
    teams_fixture = json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures"
            / "team_matchups"
            / "thirty_teams.json"
        ).read_text()
    )
    team_ids = [row["team_id"] for row in teams_fixture]
    pbp_fixture = json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures"
            / "team_matchups"
            / "pbp_bos_bounds.json"
        ).read_text()
    )
    events = []
    for game_day in range(15):
        for pair_index in range(0, 30, 2):
            event = _event(
                game_day * 15 + pair_index // 2 + 1,
                date(2025, 3, 1) + timedelta(days=game_day),
                home_team_id=team_ids[pair_index],
                away_team_id=team_ids[pair_index + 1],
            )
            event["home_team_name"] = f"Team {team_ids[pair_index]}"
            event["away_team_name"] = f"Team {team_ids[pair_index + 1]}"
            events.append(event)
    events.extend(
        (
            _event(
                901,
                date(2025, 2, 1),
                home_team_id=999_001,
                away_team_id=999_002,
                classification="Preseason",
            ),
            _event(
                902,
                date(2025, 2, 16),
                home_team_id=999_003,
                away_team_id=999_004,
                classification="All-Star",
            ),
        )
    )

    class FakeNBAStats:
        def __init__(self):
            self.calls = []

        def _teams(self, team_id):
            return [team_id] if team_id is not None else team_ids

        def fetch_opponent_team_stats(self, date_from, **kwargs):
            self.calls.append(("traditional", date_from, kwargs))
            games = kwargs["last_n_games"] or 82
            return pd.DataFrame(
                [
                    {
                        "TEAM_ID": team_id,
                        "TEAM_NAME": f"Team {team_id}",
                        "GP": games,
                        "MIN": games * 48,
                        "OPP_REB": 200 + index,
                        "OPP_TOV": 100 + index,
                        "OPP_STL": 50 + index,
                        "OPP_BLK": 25 + index,
                    }
                    for index, team_id in enumerate(self._teams(kwargs["team_id"]))
                ]
            )

        def fetch_opponent_shot_chart(self, general_range, date_from, **kwargs):
            self.calls.append(("shot_types", general_range, date_from, kwargs))
            games = kwargs["last_n_games"] or 82
            return pd.DataFrame(
                [
                    {
                        "TEAM_ID": team_id,
                        "TEAM_NAME": f"Team {team_id}",
                        "GP": games,
                        "FG2M": 200 + index,
                        "FG2A": 400 + index,
                        "FG3M": 100 + index,
                        "FG3A": 300 + index,
                    }
                    for index, team_id in enumerate(self._teams(kwargs["team_id"]))
                ]
            )

        def fetch_opponent_shooting_zone(self, date_from, **kwargs):
            self.calls.append(("shot_zones", date_from, kwargs))
            games = kwargs["last_n_games"] or 82
            return pd.DataFrame(
                [
                    {
                        "TEAM_ID": team_id,
                        "TEAM_NAME": f"Team {team_id}",
                        "GP": games,
                        "Restricted Area_OPP_FGM": 200 + index,
                        "Restricted Area_OPP_FGA": 350 + index,
                        "Backcourt_OPP_FGM": 1,
                        "Backcourt_OPP_FGA": 2,
                    }
                    for index, team_id in enumerate(self._teams(kwargs["team_id"]))
                ]
            )

        def fetch_synergy_play_types(self, play_type, **kwargs):
            self.calls.append(("play_types", play_type, kwargs))
            return pd.DataFrame(
                [
                    {
                        "TEAM_ID": team_id,
                        "TEAM_NAME": f"Team {team_id}",
                        "GP": 82,
                        "PTS": 400 + index,
                        "POSS": 200 + index,
                    }
                    for index, team_id in enumerate(team_ids)
                ]
            )

    class FakePBPStats:
        def __init__(self):
            self.calls = []

        def fetch_totals_frame(self, data_type, **kwargs):
            assert data_type == "opponent"
            self.calls.append(kwargs)
            if kwargs["team_id"] is None:
                rows = []
                for index, team_id in enumerate(team_ids):
                    row = {
                        "TeamId": team_id,
                        "Name": f"Team {team_id}",
                        "SecondsPlayed": 82 * 48 * 60,
                        "Assists": 1000 + index,
                        "Arc3Assists": 200 + index,
                        "Corner3Assists": 100 + index,
                        "AtRimAssists": 300 + index,
                        "ShortMidRangeAssists": 250 + index,
                        "LongMidRangeAssists": 150 + index,
                    }
                    if team_id == BOS:
                        row.update(pbp_fixture["season"])
                    rows.append(row)
                return pd.DataFrame(rows)
            row = {
                "TeamId": kwargs["team_id"],
                "Name": f"Team {kwargs['team_id']}",
                "GamesPlayed": 15,
                "SecondsPlayed": 15 * 48 * 60,
                "Assists": 300,
                "Arc3Assists": 60,
                "Corner3Assists": 30,
                "AtRimAssists": 90,
                "ShortMidRangeAssists": 75,
                "LongMidRangeAssists": 45,
            }
            return pd.DataFrame([row])

    engine = create_engine(f"sqlite:///{tmp_path / 'refresh.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    nba = FakeNBAStats()
    pbp = FakePBPStats()
    catalog = FakeEventCatalog(events)
    retrieved_at = datetime(2025, 4, 15, 10, tzinfo=timezone.utc)
    service = TeamMatchupRefreshService(
        repository=repository,
        event_catalog=catalog,
        nba_stats_provider=nba,
        pbp_stats_provider=pbp,
        clock=lambda: retrieved_at,
    )

    service.refresh("2024-25", as_of=date(2025, 4, 15))

    assert catalog.calls == 1
    rolling_calls = [call for call in nba.calls if call[-1].get("team_id") is not None]
    assert rolling_calls
    assert all(call[-1]["last_n_games"] == 15 for call in rolling_calls)
    assert all(call[-2] == "03/01/2025" for call in rolling_calls)
    assert all(call[-1]["date_to"] == "04/15/2025" for call in rolling_calls)
    assert all(call[-1]["season_type"] == "Regular Season" for call in rolling_calls)
    bos_pbp_call = next(call for call in pbp.calls if call["team_id"] == BOS)
    assert bos_pbp_call["from_date"] == "2025-03-01"
    assert bos_pbp_call["to_date"] == "2025-04-15"
    assert bos_pbp_call["season_type"] == "Regular Season"

    season = repository.get_snapshot(
        TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15))
    )
    last_15 = repository.get_snapshot(
        TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)
    )
    season_bos_assists = next(
        fact
        for fact in season.facts
        if fact.team_id == BOS
        and fact.base == "assist_locations"
        and fact.stat_key == "Assists"
    )
    last_15_bos_assists = next(
        fact
        for fact in last_15.facts
        if fact.team_id == BOS
        and fact.base == "assist_locations"
        and fact.stat_key == "Assists"
    )
    assert season_bos_assists.raw_value == 1968
    assert last_15_bos_assists.raw_value == 300
    assert {
        fact.stat_key
        for fact in season.facts
        if fact.base == "traditional"
    } == {"OPP_REB", "OPP_TOV", "OPP_STL", "OPP_BLK"}
    assert {
        fact.stat_key
        for fact in last_15.facts
        if fact.base == "traditional"
    } == {"OPP_REB", "OPP_TOV", "OPP_STL", "OPP_BLK"}
    bos_transition = {
        fact.stat_key: fact.raw_value
        for fact in season.facts
        if fact.team_id == BOS
        and fact.base == "play_types"
        and fact.slice_key == "Transition"
    }
    assert bos_transition == {"POSS": 201, "PTS": 401}
    season_window = TeamMatchupQueryService(repository).get_window(season.scope)
    assert {
        metric.stat_key
        for metric in season_window.league_metrics
        if metric.base == "play_types" and metric.slice_key == "Transition"
    } == {"POSS", "PTS"}
    assert all(
        fact.denominator_unit == "minutes"
        for fact in last_15.facts
        if fact.provider == "nba_stats"
    )
    assert not any(
        fact.base == "shot_zones" and "Backcourt" in fact.slice_key
        for fact in (*season.facts, *last_15.facts)
    )
    assert not any(
        call[0] == "play_types" and call[-1].get("last_n_games") == 15
        for call in nba.calls
    )
    play_type_observation = next(
        observation
        for observation in last_15.observations
        if observation.surface == "play_types"
    )
    assert play_type_observation.status == "unavailable"
    assert play_type_observation.unavailable_reason == "provider_window_unsupported"
    assert not any(fact.base == "play_types" for fact in last_15.facts)


def test_refresh_persists_production_shape_opponent_shot_locations(tmp_path):
    class MultiIndexShotZonesNBA(_FakeMatchupNBA):
        def fetch_opponent_shooting_zone(self, date_from, **kwargs):
            self.calls.append(("shot_zones", date_from, kwargs))
            columns = pd.MultiIndex.from_tuples(
                [
                    ("", "TEAM_ID"),
                    ("", "TEAM_NAME"),
                    ("Restricted Area", "OPP_FGM"),
                    ("Restricted Area", "OPP_FGA"),
                    ("Restricted Area", "OPP_FG_PCT"),
                    ("Backcourt", "OPP_FGM"),
                    ("Backcourt", "OPP_FGA"),
                ]
            )
            return pd.DataFrame(
                [
                    [
                        team_id,
                        f"Team {team_id}",
                        200 if team_id == BOS else 300 + index,
                        350 if team_id == BOS else 450 + index,
                        0.571,
                        1,
                        2,
                    ]
                    for index, team_id in enumerate(self._teams(kwargs["team_id"]))
                ],
                columns=columns,
            )

    team_ids = _fixture_team_ids()
    as_of = date(2025, 4, 15)
    engine = create_engine(f"sqlite:///{tmp_path / 'multi-index-zones.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    service = TeamMatchupRefreshService(
        repository=repository,
        event_catalog=FakeEventCatalog(_one_game_for_every_team(as_of)),
        nba_stats_provider=MultiIndexShotZonesNBA(team_ids),
        pbp_stats_provider=_FakeMatchupPBP(team_ids),
        clock=lambda: datetime(2025, 4, 15, 10, tzinfo=timezone.utc),
    )

    service.refresh("2024-25", as_of=as_of)

    snapshot = repository.get_snapshot(TeamMatchupSnapshotScope("2024-25", as_of))
    zone_observation = next(
        item for item in snapshot.observations if item.surface == "shot_zones"
    )
    bos_zones = {
        (fact.slice_key, fact.stat_key): fact.raw_value
        for fact in snapshot.facts
        if fact.team_id == BOS and fact.base == "shot_zones"
    }
    assert zone_observation.status == "available"
    assert bos_zones == {
        ("Restricted Area", "FGA"): 350,
        ("Restricted Area", "FGM"): 200,
    }
    assert not any(
        fact.base == "shot_zones"
        and (fact.slice_key == "Backcourt" or fact.stat_key == "FG_PCT")
        for fact in snapshot.facts
    )


def test_refresh_publishes_season_and_honest_missing_last_15_early_in_season(
    tmp_path,
):
    team_ids = [
        row["team_id"]
        for row in json.loads(
            (
                Path(__file__).parents[1]
                / "fixtures"
                / "team_matchups"
                / "thirty_teams.json"
            ).read_text()
        )
    ]
    events = [
        _event(
            pair_index + 1,
            date(2024, 10, 22),
            home_team_id=team_ids[pair_index],
            away_team_id=team_ids[pair_index + 1],
        )
        for pair_index in range(0, 30, 2)
    ]
    engine = create_engine(f"sqlite:///{tmp_path / 'early.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    nba = _FakeMatchupNBA(team_ids)
    pbp = _FakeMatchupPBP(team_ids)
    service = TeamMatchupRefreshService(
        repository=repository,
        event_catalog=FakeEventCatalog(events),
        nba_stats_provider=nba,
        pbp_stats_provider=pbp,
        clock=lambda: datetime(2024, 10, 23, 10, tzinfo=timezone.utc),
    )

    service.refresh("2024-25", as_of=date(2024, 10, 22))

    season = repository.get_snapshot(
        TeamMatchupSnapshotScope("2024-25", date(2024, 10, 22))
    )
    last_15 = repository.get_snapshot(
        TeamMatchupSnapshotScope("2024-25", date(2024, 10, 22), 15)
    )
    assert season.facts
    season_play_types = next(
        item for item in season.observations if item.surface == "play_types"
    )
    assert season_play_types.status == "unavailable"
    assert season_play_types.unavailable_reason == "provider_unbounded_as_of"
    assert not any(fact.base == "play_types" for fact in season.facts)
    assert not any(call[0] == "play_types" for call in nba.calls)
    assert last_15.facts == ()
    assert {(item.status, item.unavailable_reason) for item in last_15.observations} == {
        ("missing", "insufficient_governed_games")
    }
    assert not any(call[-1].get("last_n_games") == 15 for call in nba.calls)
    assert not any(call["team_id"] is not None for call in pbp.calls)


def test_refresh_records_malformed_nba_surface_and_preserves_prior_facts(tmp_path):
    class MalformedZonesNBA(_FakeMatchupNBA):
        def fetch_opponent_shooting_zone(self, date_from, **kwargs):
            raise ProviderResponseError("bad shooting-zone schema")

    team_ids = _fixture_team_ids()
    observed_at = datetime(2025, 4, 15, 15, tzinfo=timezone.utc)
    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15))
    prior_zone_facts = [
        TeamMatchupFact(
            team_id=team_id,
            base="shot_zones",
            slice_key="Restricted Area",
            stat_key="FGM",
            raw_value=77,
            denominator_value=48,
            denominator_unit="minutes",
            provider="nba_stats",
        )
        for team_id in team_ids
    ]
    engine = create_engine(f"sqlite:///{tmp_path / 'malformed-nba.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    _publish_snapshot_batch(
        repository,
        scope,
        facts=prior_zone_facts,
        observations=[TeamMatchupObservation("shot_zones", "available")],
        retrieved_at=observed_at - timedelta(hours=1),
    )
    service = TeamMatchupRefreshService(
        repository=repository,
        event_catalog=FakeEventCatalog(_one_game_for_every_team(scope.as_of)),
        nba_stats_provider=MalformedZonesNBA(team_ids),
        pbp_stats_provider=_FakeMatchupPBP(team_ids),
        clock=lambda: observed_at,
    )

    service.refresh("2024-25", as_of=scope.as_of)

    snapshot = repository.get_snapshot(scope)
    observations = {item.surface: item for item in snapshot.observations}
    assert observations["shot_zones"].status == "unavailable"
    assert (
        observations["shot_zones"].unavailable_reason == "provider_malformed_response"
    )
    assert observations["traditional"].status == "available"
    assert {fact.raw_value for fact in snapshot.facts if fact.base == "shot_zones"} == {
        77
    }


def test_empty_season_traditional_surface_is_incomplete_and_preserves_prior_facts(
    tmp_path,
):
    class EmptyTraditionalNBA(_FakeMatchupNBA):
        def fetch_opponent_team_stats(self, date_from, **kwargs):
            return pd.DataFrame(
                columns=(
                    "TEAM_ID",
                    "TEAM_NAME",
                    "GP",
                    "MIN",
                    "OPP_TOV",
                    "OPP_STL",
                    "OPP_BLK",
                )
            )

    team_ids = _fixture_team_ids()
    observed_at = datetime(2025, 4, 15, 15, tzinfo=timezone.utc)
    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15))
    engine = create_engine(f"sqlite:///{tmp_path / 'empty-traditional.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    prior_facts = _complete_traditional_facts(value=77)
    _publish_snapshot_batch(
        repository,
        scope,
        facts=prior_facts,
        observations=[TeamMatchupObservation("traditional", "available")],
        retrieved_at=observed_at - timedelta(hours=1),
    )
    TeamMatchupRefreshService(
        repository=repository,
        event_catalog=FakeEventCatalog(_one_game_for_every_team(scope.as_of)),
        nba_stats_provider=EmptyTraditionalNBA(team_ids),
        pbp_stats_provider=_FakeMatchupPBP(team_ids),
        clock=lambda: observed_at,
    ).refresh("2024-25", as_of=scope.as_of)

    snapshot = repository.get_snapshot(scope)
    observation = next(
        item for item in snapshot.observations if item.surface == "traditional"
    )
    assert observation.status == "unavailable"
    assert observation.unavailable_reason == "surface_incomplete"
    assert {fact.raw_value for fact in snapshot.facts if fact.base == "traditional"} == {
        77
    }


def test_season_surface_rejects_a_substituted_off_roster_team_locally(tmp_path):
    team_ids = _fixture_team_ids()

    class OffRosterSeasonPBP(_FakeMatchupPBP):
        def fetch_totals_frame(self, data_type, **kwargs):
            frame = super().fetch_totals_frame(data_type, **kwargs)
            if kwargs["team_id"] is None:
                frame.loc[frame.index[-1], "TeamId"] = 999_999_999
            return frame

    observed_at = datetime(2025, 4, 15, 15, tzinfo=timezone.utc)
    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15))
    engine = create_engine(f"sqlite:///{tmp_path / 'off-roster.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    prior_facts = [
        TeamMatchupFact(
            team_id=team_id,
            base="assist_locations",
            slice_key="Assists",
            stat_key="Assists",
            raw_value=77,
            denominator_value=48 * 60,
            denominator_unit="seconds",
            provider="pbp_stats",
        )
        for team_id in team_ids
    ]
    _publish_snapshot_batch(
        repository,
        scope,
        facts=prior_facts,
        observations=[TeamMatchupObservation("assist_locations", "available")],
        retrieved_at=observed_at - timedelta(hours=1),
    )
    TeamMatchupRefreshService(
        repository=repository,
        event_catalog=FakeEventCatalog(_one_game_for_every_team(scope.as_of)),
        nba_stats_provider=_FakeMatchupNBA(team_ids),
        pbp_stats_provider=OffRosterSeasonPBP(team_ids),
        clock=lambda: observed_at,
    ).refresh("2024-25", as_of=scope.as_of)

    snapshot = repository.get_snapshot(scope)
    observations = {item.surface: item for item in snapshot.observations}
    assert observations["assist_locations"].status == "unavailable"
    assert (
        observations["assist_locations"].unavailable_reason
        == "provider_roster_mismatch"
    )
    assert {
        fact.team_id for fact in snapshot.facts if fact.base == "assist_locations"
    } == set(team_ids)
    assert {
        fact.raw_value for fact in snapshot.facts if fact.base == "assist_locations"
    } == {77}
    for surface in ("traditional", "shot_types", "shot_zones", "play_types"):
        assert observations[surface].status == "available"


def test_backdated_unbounded_play_types_keep_their_truthful_failure_reason(tmp_path):
    class MalformedTraditionalNBA(_FakeMatchupNBA):
        def fetch_opponent_team_stats(self, date_from, **kwargs):
            raise ProviderResponseError("bad traditional schema")

    team_ids = _fixture_team_ids()
    observed_at = datetime(2025, 4, 16, 10, tzinfo=timezone.utc)
    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15))
    engine = create_engine(f"sqlite:///{tmp_path / 'backdated-malformed.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    service = TeamMatchupRefreshService(
        repository=repository,
        event_catalog=FakeEventCatalog(_one_game_for_every_team(scope.as_of)),
        nba_stats_provider=MalformedTraditionalNBA(team_ids),
        pbp_stats_provider=_FakeMatchupPBP(team_ids),
        clock=lambda: observed_at,
    )

    service.refresh("2024-25", as_of=scope.as_of)

    observations = {
        item.surface: item for item in repository.get_snapshot(scope).observations
    }
    assert observations["play_types"].status == "unavailable"
    assert (
        observations["play_types"].unavailable_reason
        == "provider_unbounded_as_of"
    )
    for surface in ("traditional", "shot_types", "shot_zones"):
        assert observations[surface].status == "unavailable"
        assert (
            observations[surface].unavailable_reason
            == "provider_malformed_response"
        )


def test_refresh_records_malformed_pbp_surface_and_continues_nba_surfaces(tmp_path):
    class MalformedPBP(_FakeMatchupPBP):
        def fetch_totals_frame(self, data_type, **kwargs):
            raise ProviderResponseError("bad opponent schema")

    team_ids = _fixture_team_ids()
    observed_at = datetime(2025, 4, 15, 15, tzinfo=timezone.utc)
    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15))
    engine = create_engine(f"sqlite:///{tmp_path / 'malformed-pbp.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    service = TeamMatchupRefreshService(
        repository=repository,
        event_catalog=FakeEventCatalog(_one_game_for_every_team(scope.as_of)),
        nba_stats_provider=_FakeMatchupNBA(team_ids),
        pbp_stats_provider=MalformedPBP(team_ids),
        clock=lambda: observed_at,
    )

    service.refresh("2024-25", as_of=scope.as_of)

    snapshot = repository.get_snapshot(scope)
    observations = {item.surface: item for item in snapshot.observations}
    assert observations["assist_locations"].status == "unavailable"
    assert (
        observations["assist_locations"].unavailable_reason
        == "provider_malformed_response"
    )
    assert observations["traditional"].status == "available"
    assert any(fact.base == "traditional" for fact in snapshot.facts)


def test_rolling_fanout_preserves_the_first_shot_surface_failure(tmp_path):
    team_ids = _fixture_team_ids()
    events = [
        _event(
            game_day * 15 + pair_index // 2 + 1,
            date(2025, 3, 1) + timedelta(days=game_day),
            home_team_id=team_ids[pair_index],
            away_team_id=team_ids[pair_index + 1],
        )
        for game_day in range(15)
        for pair_index in range(0, 30, 2)
    ]

    class OrderedFailuresNBA(_FakeMatchupNBA):
        def fetch_opponent_team_stats(self, date_from, **kwargs):
            if kwargs["team_id"] == team_ids[1]:
                return pd.DataFrame()
            return super().fetch_opponent_team_stats(date_from, **kwargs)

        def fetch_opponent_shot_chart(self, general_range, date_from, **kwargs):
            if kwargs["team_id"] == team_ids[0]:
                raise ProviderResponseError("first shot-type response is malformed")
            return super().fetch_opponent_shot_chart(
                general_range, date_from, **kwargs
            )

        def fetch_opponent_shooting_zone(self, date_from, **kwargs):
            if kwargs["team_id"] == team_ids[0]:
                raise ProviderResponseError("first shot-zone response is malformed")
            return super().fetch_opponent_shooting_zone(date_from, **kwargs)

    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)
    engine = create_engine(f"sqlite:///{tmp_path / 'ordered-failures.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    TeamMatchupRefreshService(
        repository=repository,
        event_catalog=FakeEventCatalog(events),
        nba_stats_provider=OrderedFailuresNBA(team_ids),
        pbp_stats_provider=_FakeMatchupPBP(team_ids),
        clock=lambda: datetime(2025, 4, 16, 10, tzinfo=timezone.utc),
    ).refresh("2024-25", as_of=scope.as_of)

    observations = {
        item.surface: item for item in repository.get_snapshot(scope).observations
    }
    for surface in ("shot_types", "shot_zones"):
        assert observations[surface].status == "unavailable"
        assert (
            observations[surface].unavailable_reason
            == "provider_malformed_response"
        )
    assert observations["traditional"].unavailable_reason == (
        "provider_window_unverified"
    )
    assert observations["assist_locations"].status == "available"


def test_refresh_marks_cross_phase_last_15_unavailable_without_mixing_games(
    tmp_path,
):
    team_ids = [
        row["team_id"]
        for row in json.loads(
            (
                Path(__file__).parents[1]
                / "fixtures"
                / "team_matchups"
                / "thirty_teams.json"
            ).read_text()
        )
    ]
    events = []
    for game_day in range(15):
        for pair_index in range(0, 30, 2):
            event = _event(
                game_day * 15 + pair_index // 2 + 1,
                date(2025, 4, 1) + timedelta(days=game_day),
                home_team_id=team_ids[pair_index],
                away_team_id=team_ids[pair_index + 1],
                classification="Regular Season" if game_day < 2 else "Playoffs",
            )
            if game_day >= 2:
                event["nba_game_id"] = f"004240{game_day * 15 + pair_index // 2 + 1:04d}"
            events.append(event)
    engine = create_engine(f"sqlite:///{tmp_path / 'cross-phase.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    nba = _FakeMatchupNBA(team_ids)
    pbp = _FakeMatchupPBP(team_ids)
    service = TeamMatchupRefreshService(
        repository=repository,
        event_catalog=FakeEventCatalog(events),
        nba_stats_provider=nba,
        pbp_stats_provider=pbp,
        clock=lambda: datetime(2025, 4, 16, 10, tzinfo=timezone.utc),
    )

    service.refresh("2024-25", as_of=date(2025, 4, 15))

    last_15 = repository.get_snapshot(
        TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)
    )
    assert last_15.facts == ()
    assert {
        (item.surface, item.status, item.unavailable_reason)
        for item in last_15.observations
    } == {
        ("assist_locations", "unavailable", "provider_window_unrepresentable"),
        ("play_types", "unavailable", "provider_window_unsupported"),
        ("shot_types", "unavailable", "provider_window_unrepresentable"),
        ("shot_zones", "unavailable", "provider_window_unrepresentable"),
        ("traditional", "unavailable", "provider_window_unrepresentable"),
    }
    assert not any(call[-1].get("last_n_games") == 15 for call in nba.calls)
    assert not any(call["team_id"] is not None for call in pbp.calls)


def test_refresh_refuses_to_label_an_unverified_nba_window_as_last_15(tmp_path):
    team_ids = [
        row["team_id"]
        for row in json.loads(
            (
                Path(__file__).parents[1]
                / "fixtures"
                / "team_matchups"
                / "thirty_teams.json"
            ).read_text()
        )
    ]
    events = [
        _event(
            game_day * 15 + pair_index // 2 + 1,
            date(2025, 3, 1) + timedelta(days=game_day),
            home_team_id=team_ids[pair_index],
            away_team_id=team_ids[pair_index + 1],
        )
        for game_day in range(15)
        for pair_index in range(0, 30, 2)
    ]
    engine = create_engine(f"sqlite:///{tmp_path / 'unverified.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    service = TeamMatchupRefreshService(
        repository=repository,
        event_catalog=FakeEventCatalog(events),
        nba_stats_provider=_FakeMatchupNBA(team_ids, rolling_games=14),
        pbp_stats_provider=_FakeMatchupPBP(team_ids),
        clock=lambda: datetime(2025, 4, 16, 10, tzinfo=timezone.utc),
    )

    service.refresh("2024-25", as_of=date(2025, 4, 15))

    last_15 = repository.get_snapshot(
        TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)
    )
    observations = {item.surface: item for item in last_15.observations}
    assert not any(fact.provider == "nba_stats" for fact in last_15.facts)
    for surface in ("traditional", "shot_types", "shot_zones"):
        assert observations[surface].status == "unavailable"
        assert observations[surface].unavailable_reason == "provider_window_unverified"
    assert observations["assist_locations"].status == "available"
    assert any(fact.provider == "pbp_stats" for fact in last_15.facts)


def test_empty_rolling_surface_is_unverified_and_preserves_prior_facts(tmp_path):
    team_ids = _fixture_team_ids()
    events = [
        _event(
            game_day * 15 + pair_index // 2 + 1,
            date(2025, 3, 1) + timedelta(days=game_day),
            home_team_id=team_ids[pair_index],
            away_team_id=team_ids[pair_index + 1],
        )
        for game_day in range(15)
        for pair_index in range(0, 30, 2)
    ]

    class EmptyRollingShotTypesNBA(_FakeMatchupNBA):
        def fetch_opponent_shot_chart(self, general_range, date_from, **kwargs):
            if kwargs["team_id"] == BOS:
                self.calls.append(("shot_types", general_range, date_from, kwargs))
                return pd.DataFrame()
            return super().fetch_opponent_shot_chart(
                general_range, date_from, **kwargs
            )

    observed_at = datetime(2025, 4, 16, 10, tzinfo=timezone.utc)
    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)
    prior_facts = [
        TeamMatchupFact(
            team_id=team_id,
            base="shot_types",
            slice_key="At Rim",
            stat_key="FG2M",
            raw_value=77,
            denominator_value=48,
            denominator_unit="minutes",
            provider="nba_stats",
        )
        for team_id in team_ids
    ]
    engine = create_engine(f"sqlite:///{tmp_path / 'empty-rolling.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    _publish_snapshot_batch(
        repository,
        scope,
        facts=prior_facts,
        observations=[TeamMatchupObservation("shot_types", "available")],
        retrieved_at=observed_at - timedelta(hours=1),
    )
    service = TeamMatchupRefreshService(
        repository=repository,
        event_catalog=FakeEventCatalog(events),
        nba_stats_provider=EmptyRollingShotTypesNBA(team_ids),
        pbp_stats_provider=_FakeMatchupPBP(team_ids),
        clock=lambda: observed_at,
    )

    service.refresh("2024-25", as_of=scope.as_of)

    snapshot = repository.get_snapshot(scope)
    observations = {item.surface: item for item in snapshot.observations}
    assert observations["shot_types"].status == "unavailable"
    assert (
        observations["shot_types"].unavailable_reason
        == "provider_window_unverified"
    )
    assert {fact.raw_value for fact in snapshot.facts if fact.base == "shot_types"} == {
        77
    }
    for surface in ("traditional", "shot_zones", "assist_locations"):
        assert observations[surface].status == "available"
        assert any(fact.base == surface for fact in snapshot.facts)


@pytest.mark.parametrize(
    ("evidence_column", "reported_value"),
    [
        ("TeamId", None),
        ("TeamId", NYK),
        ("SecondsPlayed", None),
        ("GamesPlayed", None),
        ("GamesPlayed", 14),
        ("GamesPlayed", 22),
    ],
)
def test_refresh_refuses_to_label_unverified_pbp_evidence_as_last_15(
    tmp_path,
    evidence_column,
    reported_value,
):
    team_ids = _fixture_team_ids()
    events = [
        _event(
            game_day * 15 + pair_index // 2 + 1,
            date(2025, 3, 1) + timedelta(days=game_day),
            home_team_id=team_ids[pair_index],
            away_team_id=team_ids[pair_index + 1],
        )
        for game_day in range(15)
        for pair_index in range(0, 30, 2)
    ]

    class UnverifiedPBP(_FakeMatchupPBP):
        def fetch_totals_frame(self, data_type, **kwargs):
            frame = super().fetch_totals_frame(data_type, **kwargs)
            if kwargs["team_id"] is not None:
                if reported_value is None:
                    return frame.drop(columns=evidence_column)
                frame[evidence_column] = reported_value
            return frame

    engine = create_engine(f"sqlite:///{tmp_path / 'unverified-pbp.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    service = TeamMatchupRefreshService(
        repository=repository,
        event_catalog=FakeEventCatalog(events),
        nba_stats_provider=_FakeMatchupNBA(team_ids),
        pbp_stats_provider=UnverifiedPBP(team_ids),
        clock=lambda: datetime(2025, 4, 16, 10, tzinfo=timezone.utc),
    )

    service.refresh("2024-25", as_of=date(2025, 4, 15))

    last_15 = repository.get_snapshot(
        TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)
    )
    observation = next(
        item
        for item in last_15.observations
        if item.surface == "assist_locations"
    )
    assert observation.status == "unavailable"
    assert observation.unavailable_reason == "provider_window_unverified"
    assert not any(fact.provider == "pbp_stats" for fact in last_15.facts)


def test_refresh_never_substitutes_own_team_stats_for_opponent_stats(tmp_path):
    team_ids = _fixture_team_ids()
    events = [
        _event(
            game_day * 15 + pair_index // 2 + 1,
            date(2025, 3, 1) + timedelta(days=game_day),
            home_team_id=team_ids[pair_index],
            away_team_id=team_ids[pair_index + 1],
        )
        for game_day in range(15)
        for pair_index in range(0, 30, 2)
    ]

    class OwnStatsOnlyNBA(_FakeMatchupNBA):
        def fetch_opponent_team_stats(self, date_from, **kwargs):
            frame = super().fetch_opponent_team_stats(date_from, **kwargs)
            return frame.rename(
                columns={
                    "OPP_TOV": "TOV",
                    "OPP_STL": "STL",
                    "OPP_BLK": "BLK",
                }
            )

    engine = create_engine(f"sqlite:///{tmp_path / 'own-stats.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    TeamMatchupRefreshService(
        repository=repository,
        event_catalog=FakeEventCatalog(events),
        nba_stats_provider=OwnStatsOnlyNBA(team_ids),
        pbp_stats_provider=_FakeMatchupPBP(team_ids),
        clock=lambda: datetime(2025, 4, 16, 10, tzinfo=timezone.utc),
    ).refresh("2024-25", as_of=date(2025, 4, 15))

    for window_games in (None, 15):
        snapshot = repository.get_snapshot(
            TeamMatchupSnapshotScope(
                "2024-25", date(2025, 4, 15), window_games
            )
        )
        observation = next(
            item for item in snapshot.observations if item.surface == "traditional"
        )
        assert observation.status == "unavailable"
        assert observation.unavailable_reason == "provider_invalid_response"
        assert not any(fact.base == "traditional" for fact in snapshot.facts)


def test_refresh_degrades_zero_minute_dependent_surfaces_only(tmp_path):
    team_ids = _fixture_team_ids()
    events = [
        _event(
            pair_index + 1,
            date(2025, 4, 15),
            home_team_id=team_ids[pair_index],
            away_team_id=team_ids[pair_index + 1],
        )
        for pair_index in range(0, 30, 2)
    ]

    class ZeroMinutesNBA(_FakeMatchupNBA):
        def fetch_opponent_team_stats(self, date_from, **kwargs):
            frame = super().fetch_opponent_team_stats(date_from, **kwargs)
            frame.loc[0, "MIN"] = 0
            return frame

    engine = create_engine(f"sqlite:///{tmp_path / 'zero-minutes.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    TeamMatchupRefreshService(
        repository=repository,
        event_catalog=FakeEventCatalog(events),
        nba_stats_provider=ZeroMinutesNBA(team_ids),
        pbp_stats_provider=_FakeMatchupPBP(team_ids),
        clock=lambda: datetime(2025, 4, 15, 16, tzinfo=timezone.utc),
    ).refresh("2024-25", as_of=date(2025, 4, 15))

    season = repository.get_snapshot(
        TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15))
    )
    observations = {item.surface: item for item in season.observations}
    assert observations["assist_locations"].status == "available"
    for surface in ("traditional", "shot_types", "shot_zones", "play_types"):
        assert observations[surface].status == "unavailable"
        assert observations[surface].unavailable_reason == "provider_invalid_response"
    assert {fact.base for fact in season.facts} == {"assist_locations"}


def test_related_window_replacement_is_idempotent_and_transactional(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'atomic.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    observed_at = datetime(2025, 4, 16, 10, tzinfo=timezone.utc)
    season = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15))
    last_15 = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)

    def facts(value):
        return [
            TeamMatchupFact(
                team_id,
                "traditional",
                "OPP_TOV",
                "OPP_TOV",
                value,
                48,
                "minutes",
                "nba_stats",
            )
            for team_id in _fixture_team_ids()
        ]

    observation = TeamMatchupObservation("traditional", "available")
    repository.replace_snapshots(
        ((season, facts(10), [observation]), (last_15, facts(20), [observation])),
        retrieved_at=observed_at,
    )
    repository.replace_snapshots(
        ((season, facts(10), [observation]), (last_15, facts(20), [observation])),
        retrieved_at=observed_at,
    )
    assert len(repository.get_snapshot(season).facts) == 30
    assert len(repository.get_snapshot(last_15).facts) == 30

    with pytest.raises(IntegrityError):
        repository.replace_snapshots(
            (
                (season, facts(99), [observation]),
                (last_15, facts(88), [observation, observation]),
            ),
            retrieved_at=observed_at + timedelta(hours=1),
        )

    assert repository.get_snapshot(season).facts[0].raw_value == 10
    assert repository.get_snapshot(last_15).facts[0].raw_value == 20


def test_missing_surface_is_persisted_without_inventing_facts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'missing.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)
    retrieved_at = datetime(2025, 4, 16, 10, tzinfo=timezone.utc)

    _publish_snapshot_batch(
        repository,
        scope,
        facts=[],
        observations=[
            TeamMatchupObservation(
                "assist_locations", "missing", "provider_no_observation"
            )
        ],
        retrieved_at=retrieved_at,
    )

    window = TeamMatchupQueryService(repository).get_window(scope)
    assert window.league_metrics == ()
    assert window.team_metrics == {}
    assert window.observations[0].status == "missing"
    assert window.observations[0].unavailable_reason == "provider_no_observation"


def test_latest_window_resolution_is_deterministic_and_keeps_snapshot_freshness(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'latest.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    older_scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 14), 15)
    newer_scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 16), 15)
    older_retrieved_at = datetime(2025, 4, 15, 9, tzinfo=timezone.utc)
    newer_retrieved_at = datetime(2025, 4, 17, 9, tzinfo=timezone.utc)
    _publish_snapshot_batch(
        repository,
        older_scope,
        facts=_complete_traditional_facts(value=10),
        observations=[TeamMatchupObservation("traditional", "available")],
        retrieved_at=older_retrieved_at,
    )
    _publish_snapshot_batch(
        repository,
        newer_scope,
        facts=[],
        observations=[
            TeamMatchupObservation(
                "traditional", "unavailable", "provider_invalid_response"
            )
        ],
        retrieved_at=newer_retrieved_at,
    )
    query = TeamMatchupQueryService(
        repository,
        clock=lambda: datetime(2025, 4, 17, 9, tzinfo=timezone.utc),
    )

    latest = query.get_latest_window("2024-25", window_games=15)
    latest_before_cutoff = query.get_latest_window(
        "2024-25", window_games=15, as_of=date(2025, 4, 15)
    )

    assert latest is not None
    assert latest.scope == newer_scope
    assert latest.fact_scopes == {"traditional": older_scope}
    assert latest.fact_retrieved_at == {"traditional": older_retrieved_at}
    assert latest.league_metrics[0].average_allowed_per_48 == 10
    assert latest.observations[0].retrieved_at == newer_retrieved_at
    assert latest.observations[0].status == "unavailable"
    assert latest_before_cutoff is not None
    assert latest_before_cutoff.scope == older_scope
    assert latest_before_cutoff.fact_scopes == {"traditional": older_scope}
    assert latest_before_cutoff.fact_retrieved_at == {
        "traditional": older_retrieved_at
    }
    assert latest_before_cutoff.observations[0].retrieved_at == older_retrieved_at
    assert (
        query.get_latest_window(
            "2024-25", window_games=15, as_of=date(2025, 4, 13)
        )
        is None
    )


def test_latest_window_reads_a_shared_fact_scope_once(tmp_path):
    surfaces = (
        "assist_locations",
        "play_types",
        "shot_types",
        "shot_zones",
        "traditional",
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'latest-query-count.sqlite3'}")
    run_migrations(engine)
    repository = _CountingTeamMatchupRepository(engine)
    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)
    facts = [
        TeamMatchupFact(
            team_id=team_id,
            base=surface,
            slice_key="governed",
            stat_key="raw",
            raw_value=surface_index + 1,
            denominator_value=48,
            denominator_unit="minutes",
            provider="recorded",
        )
        for surface_index, surface in enumerate(surfaces)
        for team_id in _fixture_team_ids()
    ]
    _publish_snapshot_batch(
        repository,
        scope,
        facts=facts,
        observations=[
            TeamMatchupObservation(surface, "available") for surface in surfaces
        ],
        retrieved_at=datetime(2025, 4, 15, 15, tzinfo=timezone.utc),
    )

    latest = TeamMatchupQueryService(
        repository,
        clock=lambda: datetime(2025, 4, 15, 16, tzinfo=timezone.utc),
    ).get_latest_window("2024-25", window_games=15)

    assert latest is not None
    assert latest.scope == scope
    assert latest.fact_scopes == {surface: scope for surface in surfaces}
    assert len(latest.league_metrics) == 5
    assert len(latest.team_metrics) == 30
    assert repository.snapshot_calls == [scope]


def test_latest_window_reads_each_distinct_usable_fact_scope_once(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'distinct-query-count.sqlite3'}")
    run_migrations(engine)
    repository = _CountingTeamMatchupRepository(engine)
    traditional_scope = TeamMatchupSnapshotScope(
        "2024-25", date(2025, 4, 13), 15
    )
    zone_scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 14), 15)
    observation_scope = TeamMatchupSnapshotScope(
        "2024-25", date(2025, 4, 15), 15
    )
    _publish_snapshot_batch(
        repository,
        traditional_scope,
        facts=_complete_traditional_facts(value=10),
        observations=[TeamMatchupObservation("traditional", "available")],
        retrieved_at=datetime(2025, 4, 13, 15, tzinfo=timezone.utc),
    )
    zone_facts = [
        TeamMatchupFact(
            team_id=team_id,
            base="shot_zones",
            slice_key="Restricted Area",
            stat_key="FGM",
            raw_value=20,
            denominator_value=48,
            denominator_unit="minutes",
            provider="nba_stats",
        )
        for team_id in _fixture_team_ids()
    ]
    _publish_snapshot_batch(
        repository,
        zone_scope,
        facts=zone_facts,
        observations=[TeamMatchupObservation("shot_zones", "available")],
        retrieved_at=datetime(2025, 4, 14, 15, tzinfo=timezone.utc),
    )
    _publish_snapshot_batch(
        repository,
        observation_scope,
        facts=[],
        observations=[
            TeamMatchupObservation(
                "shot_zones", "unavailable", "provider_malformed_response"
            ),
            TeamMatchupObservation(
                "traditional", "unavailable", "provider_malformed_response"
            ),
        ],
        retrieved_at=datetime(2025, 4, 15, 15, tzinfo=timezone.utc),
    )

    latest = TeamMatchupQueryService(
        repository,
        clock=lambda: datetime(2025, 4, 15, 16, tzinfo=timezone.utc),
    ).get_latest_window("2024-25", window_games=15)

    assert latest is not None
    assert latest.fact_scopes == {
        "shot_zones": zone_scope,
        "traditional": traditional_scope,
    }
    assert {
        (metric.base, metric.average_allowed_per_48)
        for metric in latest.league_metrics
    } == {("shot_zones", 20), ("traditional", 10)}
    assert repository.snapshot_calls[0] == observation_scope
    assert len(repository.snapshot_calls) == 3
    assert set(repository.snapshot_calls) == {
        observation_scope,
        zone_scope,
        traditional_scope,
    }


def test_latest_window_distinguishes_a_same_day_failure_from_older_facts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'same-day-latest.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)
    valid_at = datetime(2025, 4, 15, 14, tzinfo=timezone.utc)
    failed_at = datetime(2025, 4, 15, 16, tzinfo=timezone.utc)
    _publish_snapshot_batch(
        repository,
        scope,
        facts=_complete_traditional_facts(value=10),
        observations=[TeamMatchupObservation("traditional", "available")],
        retrieved_at=valid_at,
    )
    _publish_snapshot_batch(
        repository,
        scope,
        facts=[],
        observations=[
            TeamMatchupObservation(
                "traditional", "unavailable", "provider_malformed_response"
            )
        ],
        retrieved_at=failed_at,
    )

    latest = TeamMatchupQueryService(
        repository,
        clock=lambda: failed_at,
    ).get_latest_window("2024-25", window_games=15)

    assert latest is not None
    assert latest.scope == scope
    assert latest.fact_scopes == {"traditional": scope}
    assert latest.fact_retrieved_at == {"traditional": valid_at}
    assert latest.observations[0].retrieved_at == failed_at
    assert latest.observations[0].status == "unavailable"
    assert latest.league_metrics[0].average_allowed_per_48 == 10


def test_latest_window_rejects_future_cutoffs_and_ignores_future_observations(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'future-query.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    current_scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)
    future_scope = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 16), 15)
    _publish_snapshot_batch(
        repository,
        current_scope,
        facts=_complete_traditional_facts(),
        observations=[TeamMatchupObservation("traditional", "available")],
        retrieved_at=datetime(2025, 4, 15, 14, tzinfo=timezone.utc),
    )
    _publish_snapshot_batch(
        repository,
        future_scope,
        facts=[],
        observations=[
            TeamMatchupObservation("traditional", "missing", "future_bad_data")
        ],
        retrieved_at=datetime(2025, 4, 17, 15, tzinfo=timezone.utc),
    )
    query = TeamMatchupQueryService(
        repository,
        clock=lambda: datetime(2025, 4, 15, 16, tzinfo=timezone.utc),
    )

    assert query.get_latest_window("2024-25", window_games=15).scope == current_scope
    with pytest.raises(ValueError, match="future as_of"):
        query.get_latest_window(
            "2024-25", window_games=15, as_of=date(2025, 4, 16)
        )
