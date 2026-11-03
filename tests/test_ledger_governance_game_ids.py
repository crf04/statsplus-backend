"""LedgerGovernance's per-team Regular Season game ids (crf04/statsplus-backend#612)."""

from datetime import UTC, datetime

from app.services.ledger_runtime import LedgerGovernance


def _event(game_id: str, home: int, away: int) -> dict[str, object]:
    return {"nba_game_id": game_id, "home_team_id": home, "away_team_id": away}


def test_each_team_expects_exactly_the_games_it_played_home_or_away():
    governance = LedgerGovernance(
        season="2025-26",
        cutoff=datetime(2026, 4, 13, tzinfo=UTC),
        expected_game_ids=frozenset({"g1", "g2", "g3"}),
        team_ids=frozenset({1, 2, 3, 4}),
        expected_l15_game_ids={},
        events=(_event("g1", 1, 2), _event("g2", 3, 1), _event("g3", 2, 3)),
    )

    assert governance.expected_season_game_ids == {
        1: frozenset({"g1", "g2"}),
        2: frozenset({"g1", "g3"}),
        3: frozenset({"g2", "g3"}),
        4: frozenset(),
    }
