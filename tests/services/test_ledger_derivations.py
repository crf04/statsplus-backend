"""Pure ledger derivation and ranking contracts."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from app.services.ledger_derivations import (
    ASSIST_METRICS,
    LedgerDerivationUnavailable,
    competition_ranks,
    derive_assist_location_facts,
    governed_assist_locations,
    nominal_team_minutes,
    nominal_window_minutes,
    derive_player_per36_facts,
    derive_traditional_opponent_facts,
    materialize_assist_location_window,
    materialize_team_window,
)
from app.services.ledger_materialization import (
    LedgerMaterializationService,
    LedgerMaterializationUnavailable,
)
from app.services.ledger_parity import LedgerParityArtifactRepository, compare_ledger_to_legacy
from app.services.collection_control import PublicationService
from app.services.canonical_game_ledger import (
    CanonicalGame,
    CanonicalGameLedgerRepository,
    PlayerGameFact,
    TeamGameFact,
    raw_rows_from_facts,
)
from app.migrations import run_migrations
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.models.collection_control import (
    CatalogPublication,
    CollectionManifest,
    CollectionObservation,
    PublicationVersion,
)
from app.models.canonical_game_ledger import (
    LedgerObservationEvidence,
    LedgerParityArtifact,
    LedgerPublication,
)
from tests.services.test_canonical_game_ledger import _game


def _raw_rows_for_game(game):
    return raw_rows_from_facts(game)


class _ParityReader:
    def read(self, stream_key):
        raise ValueError(f"{stream_key} diagnostic unavailable")


def test_traditional_opponent_is_derived_from_the_other_team_fact():
    game = _game()
    facts = derive_traditional_opponent_facts((game,))

    assert len(facts) == 2
    assert facts[0].opponent_team_id != facts[0].team_id
    assert {fact.opponent_points for fact in facts} == {16}


def test_assist_locations_are_counted_without_provider_percentage_sums():
    game = _game()
    players = tuple(
        replace(
            player,
            two_point_assists=1,
            three_point_assists=2,
            arc3_assists=1,
            corner3_assists=0,
            at_rim_assists=1,
            short_mid_range_assists=0,
            long_mid_range_assists=0,
        )
        for player in game.player_facts
    )
    facts = derive_assist_location_facts((replace(game, player_facts=players),))

    assert len(facts) == 4
    assert facts[0].location_total == 2


def test_sparse_assist_location_omissions_are_governed_zeros_when_reconciled():
    # The PBP wire omits observed-zero counters: one short-mid-range assist
    # arrives as TwoPtAssists=1, ShortMidRangeAssists=1 and nothing else.
    game = _game()
    sparse = replace(
        game.player_facts[0],
        assists=1,
        two_point_assists=1,
        three_point_assists=None,
        arc3_assists=None,
        corner3_assists=None,
        at_rim_assists=None,
        short_mid_range_assists=1,
        long_mid_range_assists=None,
    )
    assert governed_assist_locations(sparse) == {
        "two_point_assists": 1, "three_point_assists": 0, "arc3_assists": 0,
        "corner3_assists": 0, "at_rim_assists": 0, "short_mid_range_assists": 1,
        "long_mid_range_assists": 0,
    }
    fact = derive_assist_location_facts((replace(game, player_facts=(sparse,)),))[0]
    assert fact.location_total == 1 and fact.short_mid_range_assists == 1


def test_unreconciled_assist_location_omissions_stay_unavailable():
    game = _game()
    # Assists observed, but no location split at all: not provably zero.
    unobserved = replace(
        game.player_facts[0],
        assists=3,
        two_point_assists=None, three_point_assists=None, arc3_assists=None,
        corner3_assists=None, at_rim_assists=None, short_mid_range_assists=None,
        long_mid_range_assists=None,
    )
    assert governed_assist_locations(unobserved) is None
    # Each identity failing in isolation is likewise unavailable.
    explicit_two = dict(two_point_assists=3, at_rim_assists=3)
    assert governed_assist_locations(replace(unobserved, two_point_assists=2, at_rim_assists=2)) is None
    assert governed_assist_locations(replace(unobserved, **explicit_two, short_mid_range_assists=1)) is None
    assert governed_assist_locations(replace(unobserved, assists=4, **explicit_two, three_point_assists=1)) is None
    assert governed_assist_locations(replace(unobserved, assists=4, **explicit_two, three_point_assists=1, arc3_assists=1)) == {
        "two_point_assists": 3, "three_point_assists": 1, "arc3_assists": 1,
        "corner3_assists": 0, "at_rim_assists": 3, "short_mid_range_assists": 0,
        "long_mid_range_assists": 0,
    }
    with pytest.raises(LedgerDerivationUnavailable):
        derive_assist_location_facts((replace(game, player_facts=(unobserved,)),))
    # A player with no assists and no location fields is a complete zero row.
    none_at_all = replace(unobserved, assists=0)
    assert governed_assist_locations(none_at_all) == {metric: 0 for metric in ASSIST_METRICS}


def test_explicit_locations_exceeding_assists_are_unavailable_in_both_consumers():
    game = _game()
    overflow = replace(
        game.player_facts[0],
        assists=1,
        two_point_assists=2, three_point_assists=0, arc3_assists=0,
        corner3_assists=0, at_rim_assists=2, short_mid_range_assists=0,
        long_mid_range_assists=0,
    )
    assert governed_assist_locations(overflow) is None
    with pytest.raises(LedgerDerivationUnavailable):
        derive_assist_location_facts((replace(game, player_facts=(overflow,)),))


def test_window_materialization_counts_reconciled_sparse_locations():
    reconciled = dict(
        assists=3, two_point_assists=1, three_point_assists=2, arc3_assists=1,
        corner3_assists=1, at_rim_assists=1, short_mid_range_assists=0,
        long_mid_range_assists=0,
    )
    games = tuple(
        replace(
            game,
            player_facts=tuple(replace(player, **reconciled) for player in game.player_facts),
        )
        for game in _league_games()
    )
    # Re-express every player's explicit location split as the sparse wire
    # would: observed-zero counters omitted.
    sparse_games = tuple(
        replace(
            game,
            player_facts=tuple(
                replace(
                    player,
                    **{
                        metric: (None if getattr(player, metric) == 0 else getattr(player, metric))
                        for metric in ASSIST_METRICS
                    },
                )
                for player in game.player_facts
            ),
        )
        for game in games
    )
    kwargs = dict(
        season="2025-26",
        as_of=date(2025, 10, 15),
        window_games=15,
        expected_game_ids=frozenset(game.game_id for game in games),
        expected_team_game_ids={
            team_id: frozenset(
                game.game_id for game in games
                if team_id in {game.home_team_id, game.away_team_id}
            )
            for team_id in range(1, 31)
        },
        team_ids=frozenset(range(1, 31)),
    )
    explicit = materialize_assist_location_window(games, **kwargs)
    sparse = materialize_assist_location_window(sparse_games, **kwargs)
    assert sparse.complete and explicit.complete
    assert [team.counts for team in sparse.teams] == [team.counts for team in explicit.teams]


def test_nominal_team_minutes_recovers_game_length_from_retained_drift():
    # Production drift is seconds of PBP clock precision around 48 + 5k.
    assert nominal_team_minutes(48.0) == 48.0
    assert nominal_team_minutes(47.976666) == 48.0
    assert nominal_team_minutes(53.02) == 53.0
    assert nominal_team_minutes(58.0) == 58.0
    assert nominal_team_minutes(48.05) == 48.0
    assert nominal_team_minutes(47.95) == 48.0
    for drifted in (48.06, 47.94, 43.2, 8.0, 50.5):
        with pytest.raises(LedgerDerivationUnavailable):
            nominal_team_minutes(drifted)
    # No retained minutes keeps the count-per-game replay fallback.
    assert nominal_team_minutes(0.0) == 0.0
    assert nominal_team_minutes(-1.0) == 0.0


def test_nominal_window_minutes_reads_legacy_window_drift():
    # Production: the legacy PBP aggregate reported 719.995 for a 15-game window.
    assert nominal_window_minutes(719.995, 15) == 720.0
    assert nominal_window_minutes(725.0, 15) == 725.0
    assert nominal_window_minutes(730.04, 15) == 730.0
    assert nominal_window_minutes(719.9, 15) is None
    assert nominal_window_minutes(720.0, 0) is None
    assert nominal_window_minutes(0.0, 15) is None


def _drifted_games(drift_by_team):
    """League games whose single player row and team fact carry valid drift."""

    games = []
    for game in _league_games():
        player_facts = tuple(
            replace(player, minutes=5 * (48.0 + drift_by_team.get(player.team_id, 0.0)))
            for player in game.player_facts
        )
        minutes_by_team = {player.team_id: player.minutes / 5 for player in player_facts}
        games.append(replace(
            game,
            player_facts=player_facts,
            team_facts=tuple(
                replace(fact, team_minutes=minutes_by_team[fact.team_id])
                for fact in game.team_facts
            ),
        ))
    return tuple(games)


def _window_kwargs(games):
    return dict(
        season="2025-26",
        as_of=date(2025, 10, 15),
        window_games=15,
        expected_game_ids=frozenset(game.game_id for game in games),
        expected_team_game_ids={
            team_id: frozenset(
                game.game_id for game in games
                if team_id in {game.home_team_id, game.away_team_id}
            )
            for team_id in range(1, 31)
        },
        team_ids=frozenset(range(1, 31)),
    )


def test_window_denominator_is_the_nominal_game_length():
    # Team 1 drifts low and team 2 high; both are regulation games.
    games = _drifted_games({1: -0.02, 2: 0.02})
    window = materialize_team_window(games, **_window_kwargs(games))
    assert window.complete
    assert all(team.team_minutes == 15 * 48.0 for team in window.teams)
    assist = materialize_assist_location_window(games, **_window_kwargs(games))
    assert all(team.team_minutes == 15 * 48.0 for team in assist.teams)
    # Identical opponent counts rank identically once drift is removed.
    by_team = {team.team_id: team for team in window.teams}
    for metric in ("rebounds", "turnovers", "steals", "blocks"):
        ranks = {team_id: by_team[team_id].competition_rank[metric] for team_id in by_team}
        per48 = {team_id: by_team[team_id].per48[metric] for team_id in by_team}
        for left, right in ((1, 2), (4, 5)):
            if by_team[left].counts[metric] == by_team[right].counts[metric]:
                assert per48[left] == per48[right] and ranks[left] == ranks[right]


def test_non_nominal_team_minutes_fail_closed_in_every_consumer():
    games = _drifted_games({3: -0.2})
    with pytest.raises(LedgerDerivationUnavailable):
        materialize_team_window(games, **_window_kwargs(games))
    with pytest.raises(LedgerDerivationUnavailable):
        materialize_assist_location_window(games, **_window_kwargs(games))
    from app.services.ledger_parity import compare_ledger_to_legacy

    with pytest.raises(LedgerDerivationUnavailable):
        compare_ledger_to_legacy(
            games, (), season="2025-26",
            legacy_traditional_rows=({"TEAM_ID": 1, "OPP_REB": 1.0},),
        )


def test_per36_aggregates_traded_player_counts_and_retains_game_teams():
    game = _game()
    traded = game.player_facts[0]
    second_game = replace(
        game,
        game_id="0022400002",
        game_date=game.game_date.replace(day=16),
        player_facts=(replace(traded, team_id=game.away_team_id, team_tricode=game.away_team_tricode), *game.player_facts[1:]),
    )
    facts = derive_player_per36_facts((game, second_game), season="2024-25")
    result = next(fact for fact in facts if fact.player_id == traded.player_id)

    assert result.game_count == 2
    assert result.team_ids_at_game == (game.home_team_id, game.away_team_id)
    assert result.points_per36 == 14.4


def test_competition_ranks_are_deterministic_and_leave_gaps_after_ties():
    assert competition_ranks({3: 10.0, 2: 10.0, 1: 7.0}) == {2: 1, 3: 1, 1: 3}
    assert competition_ranks({3: 10.0, 2: 10.0, 1: 7.0}, descending=False) == {1: 1, 2: 2, 3: 2}


def _league_games():
    teams = list(range(1, 31))
    games = []
    for round_index in range(15):
        for pair_index in range(15):
            home = teams[pair_index]
            away = teams[-1 - pair_index]
            game_id = f"00225{round_index:02d}{pair_index:03d}"
            facts = []
            team_facts = []
            for team_id, opponent_id, is_home in ((home, away, True), (away, home, False)):
                assists = 4 + (team_id % 3)
                facts.append(PlayerGameFact(
                    player_id=1000 + team_id,
                    player_name=f"Player {team_id}",
                    team_id=team_id,
                    team_tricode=f"T{team_id:02d}",
                    minutes=240.0,
                    points=10 + team_id,
                    field_goals_made=5,
                    field_goals_attempted=10,
                    two_pointers_made=4,
                    two_pointers_attempted=8,
                    three_pointers_made=1,
                    three_pointers_attempted=2,
                    free_throws_made=0,
                    free_throws_attempted=0,
                    offensive_rebounds=1,
                    defensive_rebounds=4,
                    rebounds=5,
                    assists=assists,
                    turnovers=1,
                    steals=1,
                    blocks=0,
                    personal_fouls=1,
                    two_point_assists=2,
                    three_point_assists=2,
                    arc3_assists=1,
                    corner3_assists=1,
                    at_rim_assists=1,
                    short_mid_range_assists=0,
                    long_mid_range_assists=0,
                ))
                team_facts.append(TeamGameFact(
                    team_id=team_id,
                    team_tricode=f"T{team_id:02d}",
                    opponent_team_id=opponent_id,
                    opponent_team_tricode=f"T{opponent_id:02d}",
                    is_home=is_home,
                    points=10 + team_id,
                    field_goals_made=5,
                    field_goals_attempted=10,
                    two_pointers_made=4,
                    two_pointers_attempted=8,
                    three_pointers_made=1,
                    three_pointers_attempted=2,
                    offensive_rebounds=1,
                    defensive_rebounds=4,
                    rebounds=5,
                    assists=assists,
                    turnovers=1,
                    steals=1,
                    personal_fouls=1,
                    team_minutes=48.0,
                ))
            game = CanonicalGame(
                game_id=game_id,
                season="2025-26",
                game_date=date(2025, 10, 1) + timedelta(days=round_index),
                home_team_id=home,
                home_team_tricode=f"T{home:02d}",
                away_team_id=away,
                away_team_tricode=f"T{away:02d}",
                team_facts=tuple(team_facts),
                player_facts=tuple(facts),
                source_observation_id=f"obs:{game_id}",
                retrieved_at=datetime(2025, 11, 1, tzinfo=timezone.utc),
                participant_ids_by_team=((home, (1000 + home,)), (away, (1000 + away,))),
            )
            games.append(
                replace(game, raw_rows=_raw_rows_for_game(game)).with_checksum()
            )
        teams = [teams[0], teams[-1], *teams[1:-1]]
    return tuple(games)


def test_exact_l15_is_league_complete_and_defensive_ranks_are_ascending():
    games = _league_games()
    expected = frozenset(game.game_id for game in games)
    expected_by_team = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }
    window = materialize_team_window(
        games,
        season="2025-26",
        as_of=date(2025, 10, 15),
        window_games=15,
        expected_game_ids=expected,
        expected_team_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
    )
    assert window.complete and len(window.teams) == 30
    lowest = min(window.teams, key=lambda team: team.per48["points"])
    assert lowest.competition_rank["points"] == 1
    assist = materialize_assist_location_window(
        games,
        season="2025-26",
        as_of=date(2025, 10, 15),
        window_games=15,
        expected_game_ids=expected,
        expected_team_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
    )
    assert assist.complete and len(assist.teams) == 30
    assert all(team.game_count == 15 for team in assist.teams)


def test_traditional_opponent_facts_credit_rebounds_to_players():
    game = _game()
    away_fact = next(fact for fact in game.team_facts if fact.team_id == game.away_team_id)
    with_residual = replace(
        game,
        team_facts=tuple(
            replace(fact, defensive_rebounds=fact.defensive_rebounds + 7, rebounds=fact.rebounds + 7)
            if fact.team_id == game.away_team_id else fact
            for fact in game.team_facts
        ),
    )
    home = next(
        fact for fact in derive_traditional_opponent_facts((with_residual,))
        if fact.team_id == game.home_team_id
    )
    player_sum = sum(p.rebounds for p in game.player_facts if p.team_id == game.away_team_id)
    assert home.opponent_rebounds == player_sum
    assert home.opponent_rebounds != away_fact.rebounds + 7


def test_matchup_opponent_rebounds_exclude_team_only_residuals():
    games = tuple(
        replace(
            game,
            team_facts=tuple(
                replace(
                    fact,
                    defensive_rebounds=fact.defensive_rebounds + 7,
                    rebounds=fact.rebounds + 7,
                    turnovers=fact.turnovers + 3,
                    steals=fact.steals + 2,
                    blocks=fact.blocks + 1,
                )
                for fact in game.team_facts
            ),
        )
        for game in _league_games()
    )
    expected = frozenset(game.game_id for game in games)
    expected_by_team = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }

    window = materialize_team_window(
        games,
        season="2025-26",
        as_of=date(2025, 10, 15),
        window_games=15,
        expected_game_ids=expected,
        expected_team_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
    )

    assert window.complete
    assert window.teams[0].counts["rebounds"] == 75
    assert window.teams[0].counts["turnovers"] == 60
    assert window.teams[0].counts["steals"] == 45
    assert window.teams[0].counts["blocks"] == 15


def test_assist_total_includes_team_only_residual_assists():
    games = tuple(
        replace(
            game,
            team_facts=tuple(
                replace(fact, assists=fact.assists + 1)
                if fact.team_id == 1
                else fact
                for fact in game.team_facts
            ),
        )
        for game in _league_games()
    )
    expected = frozenset(game.game_id for game in games)
    expected_by_team = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }
    window = materialize_assist_location_window(
        games,
        season="2025-26",
        as_of=date(2025, 10, 15),
        expected_game_ids=expected,
        expected_team_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
    )
    assert len(window.teams) == 30
    for team in window.teams:
        player_total = 0
        team_total = 0
        faced_residual = 0
        for game in games:
            if team.team_id not in {game.home_team_id, game.away_team_id}:
                continue
            defense = next(
                fact for fact in game.team_facts if fact.team_id == team.team_id
            )
            opponent = next(
                fact
                for fact in game.team_facts
                if fact.team_id == defense.opponent_team_id
            )
            team_total += opponent.assists
            player_total += sum(
                player.assists
                for player in game.player_facts
                if player.team_id == opponent.team_id
            )
            if opponent.team_id == 1:
                faced_residual += 1
        assert team.counts["assists"] == team_total
        assert team_total == player_total + faced_residual


def test_assist_total_is_carried_into_derived_window_metrics():
    games = _league_games()
    expected = frozenset(game.game_id for game in games)
    expected_by_team = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }
    window = materialize_assist_location_window(
        games,
        season="2025-26",
        as_of=date(2025, 10, 15),
        expected_game_ids=expected,
        expected_team_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
    )
    for team in window.teams:
        assert "assists" in team.per48
        assert "assists" in team.league_average
        assert "assists" in team.population_sigma
        assert "assists" in team.competition_rank
        assert team.per48["assists"] == team.counts["assists"] * 48.0 / team.team_minutes
    league_average = sum(team.per48["assists"] for team in window.teams) / len(window.teams)
    assert window.teams[0].league_average["assists"] == league_average


def test_materialization_persists_full_payloads_and_inactive_control_versions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'materialization.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    publications = PublicationService(
        engine,
        clock=lambda: datetime(2025, 11, 1, tzinfo=timezone.utc),
    )
    publications.register_default_streams()
    games = _league_games()
    repository.replace_games_atomic(games)
    candidate_cutoff = datetime(2025, 10, 15, 5, 22, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(CatalogPublication.__table__.insert().values(
            publication_id="ledger-event-catalog",
            season="2025-26",
            catalog_type="event",
            cutoff=candidate_cutoff,
            version="event-v1",
            checksum="ledger-event-catalog-checksum",
            payload='{"events":[]}',
            complete=True,
            published_at=candidate_cutoff,
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="ledger-manifest", season="2025-26",
            cutoff=candidate_cutoff,
            collect_before=datetime(2025, 11, 2, tzinfo=timezone.utc),
            accepted_versions="[1]", scopes='["canonical_game_ledger"]',
            checksum="ledger-manifest", status="expired",
            created_at=datetime(2025, 10, 15, tzinfo=timezone.utc),
            event_catalog_publication_id="ledger-event-catalog",
            event_catalog_checksum="ledger-event-catalog-checksum",
        ))
        connection.execute(CollectionObservation.__table__.insert(), [
            {
                "observation_id": game.source_observation_id,
                "client_observation_id": game.source_observation_id,
                "collector_id": "test",
                "manifest_id": "ledger-manifest",
                "environment": "testing",
                "provider": "pbp",
                "observation_type": "canonical_game_ledger",
                "scope": json.dumps({
                    "game_id": game.game_id,
                    "surface": "canonical_game_ledger",
                }),
                "season": game.season,
                "cutoff": candidate_cutoff,
                "schema_version": 1,
                "checksum": game.checksum,
                "payload": "{}",
                "payload_bytes": 2,
                "retrieved_at": game.retrieved_at,
                "accepted_at": game.retrieved_at,
            }
            for game in games
        ])
        connection.execute(LedgerObservationEvidence.__table__.insert(), [
            {
                "observation_id": game.source_observation_id,
                "game_id": game.game_id,
                "created_at": game.retrieved_at,
            }
            for game in games
        ])
    expected = frozenset(game.game_id for game in games)
    expected_by_team = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }

    service = LedgerMaterializationService(
        repository,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=_ParityReader(),
        publication_service=publications,
    )
    with pytest.raises(
        LedgerMaterializationUnavailable,
        match="publication cutoff must be explicit",
    ):
        service.compose(
            games,
            season="2025-26",
            as_of=date(2025, 10, 15),
            expected_game_ids=expected,
            expected_l15_game_ids=expected_by_team,
            team_ids=frozenset(range(1, 31)),
            require_assist_locations=True,
        )
    with engine.connect() as connection:
        assert connection.execute(select(LedgerPublication)).all() == []

    result = service.compose(
        games,
        season="2025-26",
        as_of=date(2025, 10, 15),
        cutoff=candidate_cutoff,
        expected_game_ids=expected,
        expected_l15_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
        require_assist_locations=True,
    )

    assert len(result.assist_location_season.teams) == 30
    with engine.connect() as connection:
        ledger_payloads = connection.execute(select(LedgerPublication.payload)).scalars().all()
        candidates = connection.execute(select(PublicationVersion).where(
            PublicationVersion.status == "candidate",
        )).all()
        parity = connection.execute(
            select(LedgerParityArtifact.__table__)
        ).mappings().all()
    assert len(ledger_payloads) == 8
    assert all(payload not in {"", "{}", "[]"} for payload in ledger_payloads)
    assert len(candidates) == 6
    assert all(
        row.cutoff.replace(tzinfo=timezone.utc) == candidate_cutoff
        for row in candidates
    )
    assert len(parity) == 4
    assert {row["stream_key"] for row in parity} == {
        "player_game_logs",
        "traditional_opponent_season",
        "traditional_opponent_l15",
        "player_per36",
    }
    assert all(row["status"] == "pending_adjudication" for row in parity)
    assert all(
        row["publication_id"] and len(row["payload_checksum"]) == 64
        for row in parity
    )
    with Session(engine) as session:
        player_log_before = session.scalars(select(PublicationVersion).where(
            PublicationVersion.stream_key == "player_game_logs"
        )).all()
    service.compose(
        games,
        season="2025-26",
        as_of=date(2025, 10, 15),
        cutoff=candidate_cutoff,
        expected_game_ids=expected,
        expected_l15_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
        require_assist_locations=True,
        candidate_stream_keys=frozenset({
            "traditional_opponent_season", "traditional_opponent_l15",
            "assist_locations_season", "assist_locations_l15",
            "player_per36",
        }),
    )
    with Session(engine) as session:
        player_log_after = session.scalars(select(PublicationVersion).where(
            PublicationVersion.stream_key == "player_game_logs"
        )).all()
    assert [row.publication_id for row in player_log_after] == [
        row.publication_id for row in player_log_before
    ]
    traditional_payload = json.loads(repository.get_publication(
        "traditional_opponent_season",
        season="2025-26",
        window_kind="season",
        window_games=0,
        as_of=date(2025, 10, 15),
    ).payload)
    assert len(traditional_payload) == 30
    assert {"per48", "league_average", "population_sigma", "competition_rank"} <= set(
        traditional_payload[0]
    )


def test_missing_assist_evidence_does_not_block_independent_streams(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'independent.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
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
        games.append(
            replace(without_locations, raw_rows=raw_rows_from_facts(without_locations)).with_checksum()
        )
    games = tuple(games)
    repository.replace_games_atomic(games)
    expected = frozenset(game.game_id for game in games)
    expected_by_team = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }

    result = LedgerMaterializationService(
        repository,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=_ParityReader(),
    ).compose(
        games,
        season="2025-26",
        as_of=date(2025, 10, 15),
        expected_game_ids=expected,
        expected_l15_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
    )

    assert result.assist_location_season is None
    with engine.connect() as connection:
        publications = connection.execute(
            select(LedgerPublication.__table__)
        ).mappings().all()
        streams = {row["stream_key"] for row in publications}
    assert {"player_game_logs", "traditional_opponent_season", "traditional_opponent_l15", "player_per36"} <= streams
    assert {"assist_locations_season", "assist_locations_l15"} <= streams
    unavailable = {
        row["stream_key"]: row for row in publications
        if row["stream_key"].startswith("assist_locations_")
    }
    assert {row["status"] for row in unavailable.values()} == {"unavailable"}
    assert {row["payload"] for row in unavailable.values()} == {"[]"}


def test_one_unavailable_assist_window_does_not_suppress_healthy_sibling(
    tmp_path, monkeypatch,
):
    import app.services.ledger_materialization as materialization_module

    engine = create_engine(f"sqlite:///{tmp_path / 'assist-window.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    games = _league_games()
    repository.replace_games_atomic(games)
    expected = frozenset(game.game_id for game in games)
    expected_by_team = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }
    real_materialize = materialization_module.materialize_assist_location_window

    def fail_season_only(*args, **kwargs):
        if kwargs.get("window_games") is None:
            raise ValueError("Season assist evidence unavailable")
        return real_materialize(*args, **kwargs)

    monkeypatch.setattr(
        materialization_module,
        "materialize_assist_location_window",
        fail_season_only,
    )
    result = LedgerMaterializationService(
        repository,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=_ParityReader(),
    ).compose(
        games,
        season="2025-26",
        as_of=date(2025, 10, 15),
        expected_game_ids=expected,
        expected_l15_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
    )

    assert result.assist_location_season is None
    assert result.assist_location_l15 is not None
    with engine.connect() as connection:
        publications = connection.execute(
            select(LedgerPublication.__table__).where(
                LedgerPublication.stream_key.in_((
                    "assist_locations_season", "assist_locations_l15",
                ))
            )
        ).mappings().all()
    by_stream = {row["stream_key"]: row for row in publications}
    assert by_stream["assist_locations_season"]["status"] == "unavailable"
    assert by_stream["assist_locations_season"]["payload"] == "[]"
    assert by_stream["assist_locations_l15"]["status"] == "complete"
    assert by_stream["assist_locations_l15"]["payload"] != "[]"


def test_one_game_without_location_observation_leaves_l15_available(tmp_path):
    """An early game with no location split makes Season unavailable, not L15."""
    engine = create_engine(f"sqlite:///{tmp_path / 'one-game.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    base = _league_games()
    # 16 rounds: the first round falls outside every team's exact L15.
    extra_round = tuple(
        replace(
            game,
            game_id=f"0022599{index:03d}",
            game_date=game.game_date - timedelta(days=30),
            checksum=None,
        )
        for index, game in enumerate(base[:15])
    )
    first = extra_round[0]
    unobserved = replace(
        first,
        player_facts=tuple(
            replace(
                player, assists=max(player.assists, 1),
                two_point_assists=None, three_point_assists=None, arc3_assists=None,
                corner3_assists=None, at_rim_assists=None, short_mid_range_assists=None,
                long_mid_range_assists=None,
            )
            for player in first.player_facts
        ),
    )
    games = tuple(
        replace(game, raw_rows=raw_rows_from_facts(game)).with_checksum()
        for game in (unobserved, *extra_round[1:], *base)
    )
    repository.replace_games_atomic(games)
    expected = frozenset(game.game_id for game in games)
    expected_by_team = {
        team_id: frozenset(
            game.game_id
            for game in sorted(
                (game for game in games if team_id in {game.home_team_id, game.away_team_id}),
                key=lambda game: (game.game_date, game.game_id),
            )[-15:]
        )
        for team_id in range(1, 31)
    }
    result = LedgerMaterializationService(
        repository,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=_ParityReader(),
    ).compose(
        games,
        season="2025-26",
        as_of=date(2025, 10, 15),
        expected_game_ids=expected,
        expected_l15_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
    )
    assert result.assist_locations == ()
    assert result.assist_location_season is None
    assert result.assist_location_l15 is not None
    assert len(result.assist_location_l15.teams) == 30
    with engine.connect() as connection:
        by_stream = {
            row["stream_key"]: row
            for row in connection.execute(
                select(LedgerPublication.__table__).where(
                    LedgerPublication.stream_key.in_((
                        "assist_locations_season", "assist_locations_l15",
                    ))
                )
            ).mappings().all()
        }
    assert by_stream["assist_locations_season"]["status"] == "unavailable"
    assert by_stream["assist_locations_season"]["payload"] == "[]"
    assert by_stream["assist_locations_l15"]["status"] == "complete"
    assert by_stream["assist_locations_l15"]["payload"] != "[]"
    assert by_stream["assist_locations_l15"]["game_count"] == len(
        set().union(*expected_by_team.values())
    )
    assert by_stream["assist_locations_season"]["game_count"] == len(expected)


def test_parity_report_compares_shared_primitives_but_ignores_provider_rates():
    game = _game()
    player = game.player_facts[0]
    report = compare_ledger_to_legacy(
        (game,),
        ({"game_id": game.game_id, "player_id": player.player_id, "PTS": player.points, "FG_PCT": 0.5},),
        season="2024-25",
    )
    assert not report.exact
    assert report.compared_count == 1
    assert any(item.classification == "missing_legacy_identity" for item in report.differences)

    different = compare_ledger_to_legacy(
        (game,),
        ({"game_id": game.game_id, "player_id": player.player_id, "PTS": player.points + 1},),
        season="2024-25",
    )
    assert different.adjudication_required
    assert any(item.field == "points" for item in different.differences)


def test_materialization_fails_closed_before_a_30_team_window(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    service = LedgerMaterializationService(
        repository,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=_ParityReader(),
    )

    try:
        service.compose((_game(),), season="2024-25", as_of=_game().game_date)
    except LedgerMaterializationUnavailable as error:
        assert "governed game IDs" in str(error) or "L15" in str(error)
    else:
        raise AssertionError("partial league window unexpectedly published")


def test_materialization_rejects_extra_stored_game_identity(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'extra.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    repository.replace_game(game)
    service = LedgerMaterializationService(
        repository,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=_ParityReader(),
    )

    try:
        service.compose(
            (),
            season=game.season,
            as_of=game.game_date,
            expected_game_ids=frozenset(),
        )
    except LedgerMaterializationUnavailable as error:
        assert "exactly equal" in str(error)
    else:
        raise AssertionError("extra stored game unexpectedly accepted")


def test_historical_materialization_ignores_later_ledger_rows(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'historical.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    games = _league_games()
    base = games[0]
    later = replace(
        base,
        game_id="later-game",
        game_date=date(2025, 10, 16),
        source_observation_id="later-observation",
        raw_rows=tuple(
            replace(row, game_id="later-game") for row in base.raw_rows
        ),
        checksum=None,
    ).with_checksum()
    repository.replace_games_atomic((*games, later))
    expected = frozenset(game.game_id for game in games)
    expected_by_team = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }

    result = LedgerMaterializationService(
        repository,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=_ParityReader(),
    ).compose(
        (*games, later), season="2025-26", as_of=date(2025, 10, 15),
        expected_game_ids=expected, expected_l15_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
    )

    assert result.season_window.complete
    assert "later-game" not in result.season_window.governed_game_ids
