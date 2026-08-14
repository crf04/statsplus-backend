"""Ledger-owned Season and exact L15 matchup materialization contracts (#114).

The high-level materialization interface accepts one season and shared cutoff,
selects the full governed Regular Season and each team's exact 15 most recent
governed games, records the exact selected game IDs and ledger checksum, and
aggregates every contracted PBP-owned non-shot opponent fact exclusively from
typed ledger counts and denominators -- with no provider dependency at all.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from app.services.ledger_derivations import window_ledger_checksum
from app.services.ledger_matchup_materialization import (
    LedgerMatchupMaterializationService,
)
from app.services.team_matchup_query import TeamMatchupQueryService
from app.services.team_matchup_repository import (
    TeamMatchupFact,
    TeamMatchupObservation,
    TeamMatchupRepository,
    TeamMatchupSnapshotScope,
)
from tests.services.test_ledger_derivations import _league_games


AS_OF = date(2025, 10, 15)
RETRIEVED_AT = datetime(2025, 10, 16, 10, tzinfo=timezone.utc)


def _engine(tmp_path, name: str):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    run_migrations(engine)
    return engine


def _materialize(engine, games, *, as_of=AS_OF):
    repository = CanonicalGameLedgerRepository(engine)
    repository.replace_games_atomic(games)
    matchup_repository = TeamMatchupRepository(engine)
    service = LedgerMatchupMaterializationService(
        repository,
        matchup_repository,
        clock=lambda: RETRIEVED_AT,
    )
    return service, matchup_repository, service.materialize("2025-26", as_of=as_of)


def _season_scope():
    return TeamMatchupSnapshotScope("2025-26", AS_OF)


def _l15_scope():
    return TeamMatchupSnapshotScope("2025-26", AS_OF, 15)


def _expected_l15_by_team(games):
    from collections import defaultdict

    per_team = defaultdict(list)
    for game in games:
        for fact in game.team_facts:
            per_team[fact.team_id].append(game)
    return {
        team_id: tuple(
            game.game_id
            for game in sorted(
                team_games, key=lambda item: (item.game_date, item.game_id), reverse=True
            )[:15]
        )
        for team_id, team_games in per_team.items()
    }


def test_high_level_interface_accepts_season_and_shared_cutoff(tmp_path):
    engine = _engine(tmp_path, "shared-cutoff.sqlite3")
    games = _league_games()
    service, matchup_repository, result = _materialize(engine, games)

    assert result.season == "2025-26"
    assert result.as_of == AS_OF
    assert result.season_selection.scope == _season_scope()
    assert result.l15_selection.scope == _l15_scope()
    assert matchup_repository.get_latest_scope(
        "2025-26", as_of=AS_OF
    ) == _season_scope()
    assert matchup_repository.get_latest_scope(
        "2025-26", window_games=15, as_of=AS_OF
    ) == _l15_scope()


def test_season_and_every_team_l15_record_exact_game_ids_and_checksum(tmp_path):
    engine = _engine(tmp_path, "exact-ids.sqlite3")
    games = _league_games()
    repository = CanonicalGameLedgerRepository(engine)
    repository.replace_games_atomic(games)
    matchup_repository = TeamMatchupRepository(engine)
    service = LedgerMatchupMaterializationService(
        repository,
        matchup_repository,
        clock=lambda: RETRIEVED_AT,
    )
    service.materialize("2025-26", as_of=AS_OF)

    checksums = repository.game_checksums("2025-26", through=AS_OF)
    all_game_ids = tuple(sorted(game.game_id for game in games))
    season_checksum = window_ledger_checksum(all_game_ids, checksums)

    season_snapshot = matchup_repository.get_snapshot(_season_scope())
    season_observation = season_snapshot.observations[0]
    assert season_observation.game_ids == all_game_ids
    assert season_observation.ledger_checksum == season_checksum
    for fact in season_snapshot.facts:
        assert fact.ledger_checksum == season_checksum

    expected_l15 = _expected_l15_by_team(games)
    l15_union = tuple(
        sorted(
            {
                game_id
                for team_ids in expected_l15.values()
                for game_id in team_ids
            }
        )
    )
    l15_checksum = window_ledger_checksum(l15_union, checksums)

    l15_snapshot = matchup_repository.get_snapshot(_l15_scope())
    l15_observation = l15_snapshot.observations[0]
    assert l15_observation.game_ids == l15_union
    assert l15_observation.ledger_checksum == l15_checksum

    by_team = {}
    for fact in l15_snapshot.facts:
        by_team.setdefault(fact.team_id, {})[
            (fact.base, fact.slice_key, fact.stat_key)
        ] = fact
    for team_id, exact_ids in expected_l15.items():
        team_facts = by_team[team_id]
        assert set(team_facts) == {
            (surface, key, key)
            for surface, key in (
                ("traditional", "OPP_REB"),
                ("traditional", "OPP_TOV"),
                ("traditional", "OPP_STL"),
                ("traditional", "OPP_BLK"),
                ("assist_locations", "Assists"),
                ("assist_locations", "Arc3Assists"),
                ("assist_locations", "Corner3Assists"),
                ("assist_locations", "AtRimAssists"),
                ("assist_locations", "ShortMidRangeAssists"),
                ("assist_locations", "LongMidRangeAssists"),
            )
        }
        assert set(team_facts[("traditional", "OPP_REB", "OPP_REB")].game_ids) == set(
            exact_ids
        )
        assert (
            team_facts[("traditional", "OPP_REB", "OPP_REB")].ledger_checksum
            == l15_checksum
        )


def test_opponent_facts_are_aggregated_from_typed_ledger_counts(tmp_path):
    engine = _engine(tmp_path, "ledger-counts.sqlite3")
    games = _league_games()
    service, matchup_repository, _result = _materialize(engine, games)

    team_id = 1
    expected = {"rebounds": 0.0, "turnovers": 0.0, "steals": 0.0, "blocks": 0.0}
    expected_minutes = 0.0
    expected_assists = 0
    for game in games:
        if team_id not in {game.home_team_id, game.away_team_id}:
            continue
        defense = next(
            fact for fact in game.team_facts if fact.team_id == team_id
        )
        opponent = next(
            fact
            for fact in game.team_facts
            if fact.team_id == defense.opponent_team_id
        )
        expected_minutes += defense.team_minutes
        for key in expected:
            expected[key] += float(getattr(opponent, key))
        for player in game.player_facts:
            if player.team_id == defense.opponent_team_id:
                expected_assists += player.assists

    l15_snapshot = matchup_repository.get_snapshot(_l15_scope())
    facts = {
        (fact.base, fact.stat_key): fact
        for fact in l15_snapshot.facts
        if fact.team_id == team_id
    }
    for stat_key, metric in (
        ("OPP_REB", "rebounds"),
        ("OPP_TOV", "turnovers"),
        ("OPP_STL", "steals"),
        ("OPP_BLK", "blocks"),
    ):
        fact = facts[("traditional", stat_key)]
        assert fact.raw_value == expected[metric]
        assert fact.denominator_value == expected_minutes
        assert fact.denominator_unit == "minutes"
        assert fact.provider == "ledger"
    assert facts[("assist_locations", "Assists")].raw_value == expected_assists


def test_ledger_surfaces_require_no_provider_collaborators(tmp_path):
    engine = _engine(tmp_path, "no-provider.sqlite3")
    games = _league_games()
    repository = CanonicalGameLedgerRepository(engine)
    repository.replace_games_atomic(games)

    class SpyRepository(TeamMatchupRepository):
        def __init__(self, wrapped):
            super().__init__(wrapped.engine)
            self.snapshot_calls = 0

        def replace_snapshots(self, snapshots, *, retrieved_at):
            self.snapshot_calls += 1
            return super().replace_snapshots(snapshots, retrieved_at=retrieved_at)

    matchup_repository = SpyRepository(TeamMatchupRepository(engine))
    service = LedgerMatchupMaterializationService(
        repository,
        matchup_repository,
        clock=lambda: RETRIEVED_AT,
    )

    service.materialize("2025-26", as_of=AS_OF)

    assert matchup_repository.snapshot_calls == 1


def test_pre_15_league_l15_is_explicitly_unavailable_not_approximated(tmp_path):
    engine = _engine(tmp_path, "pre-15.sqlite3")
    games = _league_games()[:150]
    service, matchup_repository, result = _materialize(engine, games)

    season = matchup_repository.get_snapshot(_season_scope())
    assert any(fact.base == "traditional" for fact in season.facts)

    l15 = matchup_repository.get_snapshot(_l15_scope())
    assert l15.facts == ()
    assert {
        (item.surface, item.status, item.unavailable_reason)
        for item in l15.observations
    } == {
        ("assist_locations", "missing", "insufficient_governed_games"),
        ("traditional", "missing", "insufficient_governed_games"),
    }
    assert not result.l15_selection.complete


def test_incomplete_governed_roster_publishes_missing_observations(tmp_path):
    from tests.services.test_canonical_game_ledger import _game

    game = _game()
    engine = _engine(tmp_path, "roster.sqlite3")
    repository = CanonicalGameLedgerRepository(engine)
    repository.replace_games_atomic((game,))
    matchup_repository = TeamMatchupRepository(engine)
    service = LedgerMatchupMaterializationService(
        repository,
        matchup_repository,
        clock=lambda: RETRIEVED_AT,
    )

    result = service.materialize(game.season, as_of=game.game_date)

    for window_games in (None, 15):
        snapshot = matchup_repository.get_snapshot(
            TeamMatchupSnapshotScope(game.season, game.game_date, window_games)
        )
        assert snapshot.facts == ()
        assert {
            (item.status, item.unavailable_reason) for item in snapshot.observations
        } == {("missing", "governed_team_roster_incomplete")}
    assert not result.season_selection.complete


def test_missing_assist_locations_do_not_block_ledger_traditional(tmp_path):
    from app.services.canonical_game_ledger import raw_rows_from_facts

    engine = _engine(tmp_path, "assist-missing.sqlite3")
    games = []
    for game in _league_games():
        without_locations = replace(
            game,
            player_facts=tuple(
                replace(
                    player,
                    two_point_assists=None,
                    three_point_assists=None,
                    arc3_assists=None,
                    corner3_assists=None,
                    at_rim_assists=None,
                    short_mid_range_assists=None,
                    long_mid_range_assists=None,
                )
                for player in game.player_facts
            ),
            checksum=None,
        )
        without_locations = replace(
            without_locations,
            raw_rows=raw_rows_from_facts(without_locations),
        ).with_checksum()
        games.append(without_locations)
    service, matchup_repository, result = _materialize(engine, tuple(games))

    for scope in (_season_scope(), _l15_scope()):
        snapshot = matchup_repository.get_snapshot(scope)
        observations = {item.surface: item for item in snapshot.observations}
        assert observations["traditional"].status == "available"
        assert observations["assist_locations"].status == "unavailable"
        assert (
            observations["assist_locations"].unavailable_reason
            == "assist_location_evidence_incomplete"
        )
        assert any(fact.base == "traditional" for fact in snapshot.facts)
        assert not any(fact.base == "assist_locations" for fact in snapshot.facts)
    assert result.season_selection.complete


def test_nba_owned_surface_failure_does_not_prevent_ledger_surfaces(tmp_path):
    engine = _engine(tmp_path, "nba-surface.sqlite3")
    games = _league_games()
    repository = CanonicalGameLedgerRepository(engine)
    repository.replace_games_atomic(games)
    matchup_repository = TeamMatchupRepository(engine)
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
            window_start_date=AS_OF,
        )
        for team_id in range(1, 31)
    ]
    matchup_repository.replace_snapshots(
        (
            (
                _l15_scope(),
                prior_zone_facts,
                [TeamMatchupObservation("shot_zones", "available")],
            ),
        ),
        retrieved_at=RETRIEVED_AT,
    )
    service = LedgerMatchupMaterializationService(
        repository,
        matchup_repository,
        clock=lambda: RETRIEVED_AT,
    )

    service.materialize("2025-26", as_of=AS_OF)

    snapshot = matchup_repository.get_snapshot(_l15_scope())
    assert {fact.base for fact in snapshot.facts} == {
        "shot_zones",
        "traditional",
        "assist_locations",
    }
    assert {fact.raw_value for fact in snapshot.facts if fact.base == "shot_zones"} == {
        77
    }
    observations = {item.surface: item for item in snapshot.observations}
    assert observations["shot_zones"].status == "available"
    assert observations["traditional"].status == "available"


def test_league_complete_ranking_is_deterministic_and_30_team_gated(tmp_path):
    engine = _engine(tmp_path, "ranking.sqlite3")
    games = _league_games()
    service, matchup_repository, _result = _materialize(engine, games)

    query = TeamMatchupQueryService(
        matchup_repository, clock=lambda: RETRIEVED_AT
    )
    l15_window = query.get_window(_l15_scope())

    assert len(l15_window.team_metrics) == 30
    traditional_keys = {
        (metric.slice_key, metric.stat_key)
        for metric in l15_window.league_metrics
        if metric.base == "traditional"
    }
    assert traditional_keys == {
        ("OPP_REB", "OPP_REB"),
        ("OPP_TOV", "OPP_TOV"),
        ("OPP_STL", "OPP_STL"),
        ("OPP_BLK", "OPP_BLK"),
    }
    for metric in l15_window.league_metrics:
        assert metric.team_count == 30
    traditional_team = l15_window.team_metrics[1]
    traditional = {
        metric.stat_key: metric for metric in traditional_team
        if metric.base == "traditional"
    }
    assert traditional["OPP_BLK"].rank == 1
    assert traditional["OPP_TOV"].rank == traditional["OPP_STL"].rank


def test_read_model_keeps_contracted_metrics_and_lineage_per_fact(tmp_path):
    engine = _engine(tmp_path, "read-model.sqlite3")
    games = _league_games()
    service, matchup_repository, _result = _materialize(engine, games)

    season = matchup_repository.get_snapshot(_season_scope())
    traditional = next(
        fact for fact in season.facts if fact.base == "traditional"
    )
    assert traditional.slice_key == traditional.stat_key
    assert traditional.denominator_unit == "minutes"
    assert traditional.denominator_value > 0
    assert traditional.provider == "ledger"
    assert traditional.window_end_date == AS_OF
    assert traditional.retrieved_at == RETRIEVED_AT
    assert traditional.ledger_checksum
    assert traditional.game_ids


def test_shared_cutoff_binds_season_and_l15_to_one_as_of(tmp_path):
    engine = _engine(tmp_path, "one-cutoff.sqlite3")
    games = _league_games()
    service, matchup_repository, _result = _materialize(engine, games)

    season = matchup_repository.get_snapshot(_season_scope())
    l15 = matchup_repository.get_snapshot(_l15_scope())
    assert season.scope == _season_scope()
    assert l15.scope == _l15_scope()
    assert {fact.window_end_date for fact in season.facts} == {AS_OF}
    assert {fact.window_end_date for fact in l15.facts} == {AS_OF}
    assert {obs.game_ids for obs in season.observations}
    assert {obs.game_ids for obs in l15.observations}


def test_future_as_of_is_rejected_before_any_write(tmp_path):
    engine = _engine(tmp_path, "future.sqlite3")
    games = _league_games()
    repository = CanonicalGameLedgerRepository(engine)
    repository.replace_games_atomic(games)
    matchup_repository = TeamMatchupRepository(engine)
    service = LedgerMatchupMaterializationService(
        repository,
        matchup_repository,
        clock=lambda: RETRIEVED_AT,
    )

    with pytest.raises(ValueError, match="future as_of"):
        service.materialize("2025-26", as_of=date(2025, 10, 17))

    assert matchup_repository.get_latest_scope("2025-26") is None


def test_stored_facts_are_sqlite_persisted_with_lineage_columns(tmp_path):
    engine = _engine(tmp_path, "columns.sqlite3")
    games = _league_games()
    service, matchup_repository, _result = _materialize(engine, games)

    from sqlalchemy import inspect

    fact_columns = {
        column["name"] for column in inspect(engine).get_columns("team_matchup_facts")
    }
    observation_columns = {
        column["name"]
        for column in inspect(engine).get_columns("team_matchup_surface_observations")
    }
    assert {"game_ids", "ledger_checksum"} <= fact_columns
    assert {"game_ids", "ledger_checksum"} <= observation_columns
