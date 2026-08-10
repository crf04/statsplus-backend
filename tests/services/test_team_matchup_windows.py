"""Window-aware team matchup persistence and query contracts."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
import pandas as pd
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from app.migrations import run_migrations
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


BOS = 1610612738
NYK = 1610612752


class FakeEventCatalog:
    def __init__(self, events):
        self.events = events

    def get_events(self, season):
        assert season == "2024-25"
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

    boundaries = TeamWindowBoundaryResolver(
        FakeEventCatalog(governed + excluded)
    ).last_n("2024-25", as_of=as_of, window_games=15)

    assert boundaries[BOS].from_date == date(2025, 3, 1)
    assert boundaries[BOS].to_date == as_of
    assert boundaries[BOS].game_ids == tuple(
        f"002240{index:04d}" for index in range(15, 0, -1)
    )


def test_migration_012_stores_window_ready_facts_and_surface_observations(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'matchups.sqlite3'}")

    result = run_migrations(engine)

    assert "012_create_team_matchup_facts" in result.applied
    assert {
        "team_matchup_facts",
        "team_matchup_surface_observations",
    }.issubset(inspect(engine).get_table_names())


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
    repository.replace_snapshot(
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
                        "TOV": 100 + index,
                        "STL": 50 + index,
                        "BLK": 25 + index,
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
                "SecondsPlayed": 15 * 48 * 60,
                "Assists": 300,
                "Arc3Assists": 60,
                "Corner3Assists": 30,
                "AtRimAssists": 90,
                "ShortMidRangeAssists": 75,
                "LongMidRangeAssists": 45,
            }
            if kwargs["team_id"] == BOS:
                row.update(pbp_fixture["last_15"])
            return pd.DataFrame([row])

    engine = create_engine(f"sqlite:///{tmp_path / 'refresh.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    nba = FakeNBAStats()
    pbp = FakePBPStats()
    retrieved_at = datetime(2025, 4, 16, 10, tzinfo=timezone.utc)
    service = TeamMatchupRefreshService(
        repository,
        FakeEventCatalog(events),
        nba,
        pbp,
        clock=lambda: retrieved_at,
    )

    service.refresh("2024-25", as_of=date(2025, 4, 15))

    rolling_calls = [call for call in nba.calls if call[-1].get("team_id") is not None]
    assert rolling_calls
    assert all(call[-1]["last_n_games"] == 15 for call in rolling_calls)
    assert all(call[-1]["date_to"] == "2025-04-15" for call in rolling_calls)
    bos_pbp_call = next(call for call in pbp.calls if call["team_id"] == BOS)
    assert bos_pbp_call["from_date"] == "2025-03-01"
    assert bos_pbp_call["to_date"] == "2025-04-15"

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
    assert last_15_bos_assists.raw_value == 525
    assert all(
        fact.denominator_unit == "minutes"
        for fact in last_15.facts
        if fact.provider == "nba_stats"
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
    assert play_type_observation.unavailable_reason == "provider_unsupported"
    assert all(
        fact.status == "unavailable"
        and fact.unavailable_reason == "provider_unsupported"
        for fact in last_15.facts
        if fact.base == "play_types"
    )


def test_related_window_replacement_is_idempotent_and_transactional(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'atomic.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    observed_at = datetime(2025, 4, 16, 10, tzinfo=timezone.utc)
    season = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15))
    last_15 = TeamMatchupSnapshotScope("2024-25", date(2025, 4, 15), 15)

    def fact(value):
        return TeamMatchupFact(
            BOS,
            "traditional",
            "OPP_TOV",
            "OPP_TOV",
            value,
            48,
            "minutes",
            "nba_stats",
        )

    observation = TeamMatchupObservation("traditional", "available")
    repository.replace_snapshots(
        ((season, [fact(10)], [observation]), (last_15, [fact(20)], [observation])),
        retrieved_at=observed_at,
    )
    repository.replace_snapshots(
        ((season, [fact(10)], [observation]), (last_15, [fact(20)], [observation])),
        retrieved_at=observed_at,
    )
    assert len(repository.get_snapshot(season).facts) == 1
    assert len(repository.get_snapshot(last_15).facts) == 1

    with pytest.raises(IntegrityError):
        repository.replace_snapshots(
            (
                (season, [fact(99)], [observation]),
                (last_15, [fact(88)], [observation, observation]),
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

    repository.replace_snapshot(
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
