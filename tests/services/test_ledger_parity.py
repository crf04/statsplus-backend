"""Canonical ledger semantic-parity evidence contracts."""

from dataclasses import asdict

from app.services.ledger_derivations import (
    derive_player_per36_facts,
    derive_traditional_opponent_facts,
)
from app.services.ledger_parity import (
    compare_ledger_to_legacy,
    generate_semantic_difference_report,
)
from tests.services.test_canonical_game_ledger import _game


def _legacy_player_rows(game):
    return tuple(
        {
            "GAME_ID": game.game_id,
            "PLAYER_ID": player.player_id,
            "PTS": player.points,
            "REB": player.rebounds,
            "AST": player.assists,
            "MIN": player.minutes,
        }
        for player in game.player_facts
    )


def test_identity_set_differences_are_symmetric_and_prevent_exact_parity():
    game = _game()
    legacy = list(_legacy_player_rows(game))
    missing_from_legacy = legacy.pop()
    legacy.append({"GAME_ID": game.game_id, "PLAYER_ID": 999999, "PTS": 1})

    report = compare_ledger_to_legacy((game,), legacy, season=game.season)

    assert not report.exact
    assert report.adjudication_required
    assert {difference.classification for difference in report.differences} == {
        "missing_legacy_identity",
        "missing_ledger_identity",
    }
    assert any(str(missing_from_legacy["PLAYER_ID"]) in item.identity for item in report.differences)


def test_empty_comparison_cannot_claim_exact_parity():
    report = compare_ledger_to_legacy((), (), season="2024-25")

    assert not report.exact
    assert report.status == "adjudication_required"
    assert report.differences[0].classification == "empty_comparison"


def test_traditional_opponent_and_per36_compare_derived_semantics():
    game = _game()
    traditional = tuple(asdict(fact) for fact in derive_traditional_opponent_facts((game,)))
    per36 = tuple(asdict(fact) for fact in derive_player_per36_facts((game,), season=game.season))

    report = compare_ledger_to_legacy(
        (game,),
        _legacy_player_rows(game),
        season=game.season,
        legacy_traditional_rows=traditional,
        legacy_per36_rows=per36,
    )

    assert report.exact
    assert report.compared_count == len(game.player_facts) * 2 + len(game.team_facts)

    changed_traditional = list(traditional)
    changed_traditional[0] = {**changed_traditional[0], "opponent_points": 999}
    changed_per36 = list(per36)
    changed_per36[0] = {**changed_per36[0], "points_per36": 999.0}
    different = compare_ledger_to_legacy(
        (game,),
        _legacy_player_rows(game),
        season=game.season,
        legacy_traditional_rows=changed_traditional,
        legacy_per36_rows=changed_per36,
    )

    assert not different.exact
    assert {item.field for item in different.differences} == {
        "opponent_points",
        "points_per36",
    }


def test_generated_report_persists_status_through_injected_artifact_sink():
    game = _game()
    artifacts = []
    legacy = list(_legacy_player_rows(game))
    legacy[0] = {**legacy[0], "PTS": 999}

    report = generate_semantic_difference_report(
        (game,),
        legacy,
        season=game.season,
        artifact_sink=artifacts.append,
    )

    assert artifacts == [report]
    assert artifacts[0].status == "adjudication_required"
    assert artifacts[0].adjudication_required
