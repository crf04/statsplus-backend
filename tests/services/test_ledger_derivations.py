"""Pure ledger derivation and ranking contracts."""

from dataclasses import replace

from app.services.ledger_derivations import (
    competition_ranks,
    derive_assist_location_facts,
    derive_player_per36_facts,
    derive_traditional_opponent_facts,
)
from app.services.ledger_materialization import (
    LedgerMaterializationService,
    LedgerMaterializationUnavailable,
)
from app.services.ledger_parity import compare_ledger_to_legacy
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from sqlalchemy import create_engine
from tests.services.test_canonical_game_ledger import _game


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


def test_parity_report_compares_shared_primitives_but_ignores_provider_rates():
    game = _game()
    player = game.player_facts[0]
    report = compare_ledger_to_legacy(
        (game,),
        ({"game_id": game.game_id, "player_id": player.player_id, "PTS": player.points, "FG_PCT": 0.5},),
        season="2024-25",
    )
    assert report.exact
    assert report.compared_count == 1

    different = compare_ledger_to_legacy(
        (game,),
        ({"game_id": game.game_id, "player_id": player.player_id, "PTS": player.points + 1},),
        season="2024-25",
    )
    assert different.adjudication_required
    assert different.differences[0].field == "points"


def test_materialization_fails_closed_before_a_30_team_window(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.sqlite3'}")
    repository = CanonicalGameLedgerRepository(engine, minimum_active_players_per_team=1)
    service = LedgerMaterializationService(repository)

    try:
        service.compose((_game(),), season="2024-25", as_of=_game().game_date)
    except LedgerMaterializationUnavailable as error:
        assert "League Complete" in str(error) or "L15" in str(error)
    else:
        raise AssertionError("partial league window unexpectedly published")
