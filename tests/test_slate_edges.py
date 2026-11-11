"""Slate edges: per-game points effects and the slate's Best/Toughest picks.

The service composes the Slate and Matchup documents, so these tests hand it
fake Slate and Matchup seams whose documents carry only the fields the
Matchup contract names, and assert literal expected responses.
"""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import Mock

import pytest

from app.errors import InvalidInputError, ProviderUnavailableError, ResourceNotFoundError
from app.services.slate_edges import SlateEdgesService

AWAY, HOME = 100, 200
AWAY_TRI, HOME_TRI = "AWY", "HOM"


def _game(game_id="G1", away=(AWAY, AWAY_TRI), home=(HOME, HOME_TRI)):
    return {
        "game_id": game_id,
        "status": {"state": "scheduled", "label": "7:00 pm ET"},
        "scheduled_at": "2026-04-10T23:00:00+00:00",
        "away_team": {"team_id": away[0], "tricode": away[1], "name": None},
        "home_team": {"team_id": home[0], "tricode": home[1], "name": None},
    }


def _cell(value, thin=False):
    return {"value": value, "thin": thin}


def _slice(key, share, league=0.2):
    return {
        "key": key,
        "season": {"share": share, "league_average_share": league},
    }


def _row(slice_key, percent, rank):
    return {
        "key": f"{slice_key}:PTS",
        "season": {"percent_vs_league_average": percent, "rank": rank},
    }


def _player(
    cid,
    name,
    *,
    team=AWAY,
    tricode=AWAY_TRI,
    scoring=20.0,
    blend=None,
    components=None,
    missing=(),
    slices=(("Transition", 0.3), ("Isolation", 0.2), ("Spotup", 0.1)),
    source="player_pool",
):
    window = {
        "components": components if components is not None else {},
        "blend": blend,
        "missing_inputs": list(missing),
    }
    return {
        "canonical_id": cid,
        "name": name,
        "team_id": team,
        "tricode": tricode,
        "player_source": source,
        "season_scoring": scoring,
        "diet_shares": {"play_types": [_slice(k, s) for k, s in slices]},
        "scores": {"PTS": {"season": window}},
    }


def _blend_player(cid, name, value, **kwargs):
    thin = kwargs.pop("thin", False)
    return _player(
        cid,
        name,
        blend=_cell(value, thin),
        # A component that disagrees with the Blend: the Blend is the score.
        components={"play_types": _cell(round(value + 0.5, 4), thin)},
        **kwargs,
    )


def _matchup(players, opponent_rows=None):
    """Both defenses share ``opponent_rows`` unless given per team id."""

    rows = opponent_rows or {}
    return {
        "players": players,
        "teams": [
            {
                "team_id": team,
                "defense_sheet": {"play_types": rows.get(team, [])},
            }
            for team in (AWAY, HOME)
        ],
    }


class FakeSlate:
    def __init__(self, games, error=None):
        self.games = games
        self.error = error
        self.calls = []

    def get_slate(self, requested_date=None):
        self.calls.append(requested_date)
        if self.error is not None:
            raise self.error
        return {"slate_date": requested_date or "2026-04-10", "games": self.games}


class FakeMatchups:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get_matchup(self, *, game_id):
        self.calls.append(game_id)
        payload = self.payloads[game_id]
        if isinstance(payload, Exception):
            raise payload
        return payload


def _edges(games, payloads, requested_date="2026-04-10"):
    service = SlateEdgesService(slates=FakeSlate(games), matchups=FakeMatchups(payloads))
    return service.get_edges(requested_date)


# Opponent HOM allows 30% more than the league on Transition (rank 29) and
# 12% fewer on Isolation (rank 4); AWY allows the reverse.
HOME_DEFENSE = [_row("Transition", 30.0, 29), _row("Isolation", -12.0, 4)]
AWAY_DEFENSE = [_row("Transition", -10.0, 3), _row("Isolation", 25.0, 28)]


def test_effects_are_ordered_by_absolute_value_and_capped_at_three():
    players = [
        _blend_player(1, "Small Plus", 0.02),
        _blend_player(2, "Big Minus", -0.09),
        _blend_player(3, "Mid Plus", 0.05),
        _blend_player(4, "Huge Plus", 0.11),
        _blend_player(5, "Tiny Minus", -0.01),
    ]

    game = _edges([_game()], {"G1": _matchup(players)})["games"][0]

    assert game["effects"] == [
        {
            "canonical_id": 4,
            "name": "Huge Plus",
            "tricode": "AWY",
            "season_scoring": 20.0,
            "value": 0.11,
            "thin": False,
        },
        {
            "canonical_id": 2,
            "name": "Big Minus",
            "tricode": "AWY",
            "season_scoring": 20.0,
            "value": -0.09,
            "thin": False,
        },
        {
            "canonical_id": 3,
            "name": "Mid Plus",
            "tricode": "AWY",
            "season_scoring": 20.0,
            "value": 0.05,
            "thin": False,
        },
    ]


def test_fewer_than_three_qualifying_players_yield_fewer_effects():
    players = [_blend_player(1, "Only One", 0.04)]

    game = _edges([_game()], {"G1": _matchup(players)})["games"][0]

    assert [effect["canonical_id"] for effect in game["effects"]] == [1]


def test_effects_need_eight_points_a_game_but_scores_keep_everyone():
    players = [
        _blend_player(1, "Exactly Eight", 0.03, scoring=8.0),
        _blend_player(2, "Just Under", 0.5, scoring=7.9),
        _blend_player(3, "Unknown Scoring", 0.4, scoring=None),
    ]

    game = _edges([_game()], {"G1": _matchup(players)})["games"][0]

    assert [effect["canonical_id"] for effect in game["effects"]] == [1]
    assert game["scores"] == [
        {"canonical_id": 1, "value": 0.03},
        {"canonical_id": 2, "value": 0.5},
        {"canonical_id": 3, "value": 0.4},
    ]


def test_scores_use_the_blend_or_single_component_rule_and_skip_unscored_players():
    players = [
        _blend_player(1, "Blended", 0.06),
        # No Blend, no missing input, one component: the component is the score.
        _player(2, "Component", components={"traditional": _cell(-0.04, True)}),
        # No Blend and a named gap: the withheld Blend is not replaced.
        _player(
            3,
            "Withheld",
            components={"play_types": _cell(0.2)},
            missing=["player_diet:shot_types"],
        ),
        # A partial Blend still shows its Blend even with a named gap.
        _player(
            4,
            "Partial",
            blend=_cell(0.07),
            components={"play_types": _cell(0.07)},
            missing=["player_diet:shot_types"],
        ),
        _player(5, "Nothing"),
    ]

    game = _edges([_game()], {"G1": _matchup(players)})["games"][0]

    assert game["scores"] == [
        {"canonical_id": 1, "value": 0.06},
        {"canonical_id": 2, "value": -0.04},
        {"canonical_id": 4, "value": 0.07},
    ]
    assert game["effects"] == [
        {
            "canonical_id": 4,
            "name": "Partial",
            "tricode": "AWY",
            "season_scoring": 20.0,
            "value": 0.07,
            "thin": False,
        },
        {
            "canonical_id": 1,
            "name": "Blended",
            "tricode": "AWY",
            "season_scoring": 20.0,
            "value": 0.06,
            "thin": False,
        },
        {
            "canonical_id": 2,
            "name": "Component",
            "tricode": "AWY",
            "season_scoring": 20.0,
            "value": -0.04,
            "thin": True,
        },
    ]


def test_a_thin_blend_is_an_effect_but_never_a_pick():
    players = [_blend_player(1, "Thin Star", 0.2, thin=True)]

    edges = _edges([_game()], {"G1": _matchup(players, {HOME: HOME_DEFENSE, AWAY: AWAY_DEFENSE})})

    assert edges["games"][0]["effects"][0]["thin"] is True
    assert edges["spotlight"] == {"best": None, "toughest": None}


def test_a_single_component_is_never_a_pick():
    players = [_player(1, "Component", components={"traditional": _cell(0.3)})]

    edges = _edges([_game()], {"G1": _matchup(players, {HOME: HOME_DEFENSE, AWAY: AWAY_DEFENSE})})

    assert edges["games"][0]["effects"][0]["value"] == 0.3
    assert edges["spotlight"] == {"best": None, "toughest": None}


def test_picks_need_twelve_points_a_game():
    players = [
        _blend_player(1, "Eleven", 0.4, scoring=11.9),
        _blend_player(2, "Twelve", 0.1, scoring=12.0),
    ]

    edges = _edges([_game()], {"G1": _matchup(players, {HOME: HOME_DEFENSE, AWAY: AWAY_DEFENSE})})

    assert edges["spotlight"]["best"]["canonical_id"] == 2


def test_a_rotation_player_is_an_effect_and_a_leader_but_never_a_pick():
    """A pick should be playable, so it keeps to players with a line (statsplus#459)."""

    players = [
        _blend_player(1, "No Line", 0.4, source="rotation"),
        _blend_player(2, "Posted", 0.1),
    ]

    edges = _edges([_game()], {"G1": _matchup(players, {HOME: HOME_DEFENSE, AWAY: AWAY_DEFENSE})})

    assert [effect["canonical_id"] for effect in edges["games"][0]["effects"]] == [1, 2]
    assert [leader["canonical_id"] for leader in edges["games"][0]["leaders"]] == [1, 2]
    assert edges["spotlight"]["best"]["canonical_id"] == 2


def test_best_and_toughest_are_the_slate_wide_extremes():
    g1 = _matchup(
        [_blend_player(1, "A", 0.05), _blend_player(2, "B", -0.03)],
        {HOME: HOME_DEFENSE, AWAY: AWAY_DEFENSE},
    )
    g2 = _matchup(
        [
            _blend_player(3, "C", 0.08, team=HOME, tricode=HOME_TRI),
            _blend_player(4, "D", -0.12, team=HOME, tricode=HOME_TRI),
        ],
        {HOME: HOME_DEFENSE, AWAY: AWAY_DEFENSE},
    )

    edges = _edges([_game("G1"), _game("G2")], {"G1": g1, "G2": g2})

    assert edges["spotlight"]["best"]["canonical_id"] == 3
    assert edges["spotlight"]["best"]["game_id"] == "G2"
    assert edges["spotlight"]["best"]["opponent"] == "AWY"
    assert edges["spotlight"]["best"]["tricode"] == "HOM"
    assert edges["spotlight"]["toughest"]["canonical_id"] == 4
    assert edges["spotlight"]["toughest"]["value"] == -0.12


def test_best_is_null_without_a_positive_pick_and_toughest_without_a_negative():
    only_negative = _matchup([_blend_player(1, "Neg", -0.05)], {HOME: HOME_DEFENSE, AWAY: AWAY_DEFENSE})
    only_positive = _matchup([_blend_player(2, "Pos", 0.05)], {HOME: HOME_DEFENSE, AWAY: AWAY_DEFENSE})

    negative = _edges([_game()], {"G1": only_negative})["spotlight"]
    positive = _edges([_game()], {"G1": only_positive})["spotlight"]

    assert negative["best"] is None and negative["toughest"]["canonical_id"] == 1
    assert positive["toughest"] is None and positive["best"]["canonical_id"] == 2


def test_a_favourable_pick_takes_the_slice_the_defense_gives_up_most():
    # Share order: Transition 0.30, Isolation 0.25 (Spotup 0.05 is third).
    # Home defense: Transition +30.0 (rank 29), Isolation -12.0 (rank 4).
    player = _blend_player(
        1,
        "Runner",
        0.0674,
        scoring=15.4,
        slices=(("Spotup", 0.05), ("Isolation", 0.25), ("Transition", 0.30)),
    )

    pick = _edges([_game()], {"G1": _matchup([player], {HOME: HOME_DEFENSE})})["spotlight"]["best"]

    assert pick == {
        "game_id": "G1",
        "canonical_id": 1,
        "name": "Runner",
        "tricode": "AWY",
        "opponent": "HOM",
        "season_scoring": 15.4,
        "value": 0.0674,
        "reason": {
            "base": "play_types",
            "slice_key": "Transition",
            "share": 0.3,
            "league_average_share": 0.2,
            "opponent_rank": 29,
            "opponent_percent_vs_league": 30.0,
        },
    }


def test_an_unfavourable_pick_takes_the_slice_the_defense_takes_away_most():
    # Same diet and defense as above, negative score: Isolation's -12.0 pushes
    # hardest down, though Transition is the larger slice.
    player = _blend_player(
        1,
        "Runner",
        -0.05,
        slices=(("Spotup", 0.05), ("Isolation", 0.25), ("Transition", 0.30)),
    )

    pick = _edges([_game()], {"G1": _matchup([player], {HOME: HOME_DEFENSE})})["spotlight"]["toughest"]

    assert pick["reason"] == {
        "base": "play_types",
        "slice_key": "Isolation",
        "share": 0.25,
        "league_average_share": 0.2,
        "opponent_rank": 4,
        "opponent_percent_vs_league": -12.0,
    }


def test_the_reason_only_considers_the_players_two_largest_slices():
    # Spotup is the third slice; its huge reading must not win.
    player = _blend_player(
        1,
        "Runner",
        0.05,
        slices=(("Transition", 0.4), ("Isolation", 0.3), ("Spotup", 0.1)),
    )
    defense = [
        _row("Transition", 5.0, 20),
        _row("Isolation", 8.0, 22),
        _row("Spotup", 90.0, 30),
    ]

    pick = _edges([_game()], {"G1": _matchup([player], {HOME: defense})})["spotlight"]["best"]

    assert pick["reason"]["slice_key"] == "Isolation"
    assert pick["reason"]["opponent_percent_vs_league"] == 8.0


def test_the_reason_reads_the_defense_the_pick_actually_faces():
    home_player = _blend_player(1, "Home", 0.05, team=HOME, tricode=HOME_TRI)

    pick = _edges(
        [_game()],
        {"G1": _matchup([home_player], {HOME: HOME_DEFENSE, AWAY: AWAY_DEFENSE})},
    )["spotlight"]["best"]

    # Facing AWY, whose Isolation reading (+25.0, rank 28) beats Transition.
    assert pick["opponent"] == "AWY"
    assert pick["reason"]["slice_key"] == "Isolation"
    assert pick["reason"]["opponent_rank"] == 28


def test_a_pick_with_no_readable_reason_is_still_the_extreme_with_a_null_reason():
    unreadable = _blend_player(1, "No Reading", 0.20)
    readable = _blend_player(2, "Reading", 0.10)
    # Player 1's slices are named differently, so the defense has no row.
    unreadable["diet_shares"]["play_types"] = [_slice("Cut", 0.5)]

    pick = _edges([_game()], {"G1": _matchup([unreadable, readable], {HOME: HOME_DEFENSE})})["spotlight"][
        "best"
    ]

    assert pick["canonical_id"] == 1
    assert pick["value"] == 0.20
    assert pick["reason"] is None


def test_a_game_that_cannot_be_read_has_no_effects_scores_or_pick_and_the_rest_succeed():
    good = _matchup(
        [_blend_player(7, "Survivor", 0.05, team=HOME, tricode=HOME_TRI)],
        {AWAY: AWAY_DEFENSE},
    )

    edges = _edges(
        [_game("BAD"), _game("GOOD")],
        {"BAD": ProviderUnavailableError("Stored matchup team facts are incomplete."), "GOOD": good},
    )

    assert edges["games"][0] == {"game_id": "BAD", "effects": [], "leaders": [], "scores": []}
    assert [effect["canonical_id"] for effect in edges["games"][1]["effects"]] == [7]
    assert edges["spotlight"]["best"]["game_id"] == "GOOD"


def test_a_stored_read_database_failure_is_an_unreadable_game_too():
    from sqlalchemy.exc import OperationalError

    good = _matchup([_blend_player(7, "Survivor", 0.05)], {HOME: HOME_DEFENSE})

    edges = _edges(
        [_game("BAD"), _game("GOOD")],
        {"BAD": OperationalError("select 1", {}, Exception("connection lost")), "GOOD": good},
    )

    assert edges["games"][0] == {"game_id": "BAD", "effects": [], "leaders": [], "scores": []}
    assert [e["canonical_id"] for e in edges["games"][1]["effects"]] == [7]


def _windows(**markets):
    """A ``scores`` map of season windows: each market a Blend cell, or a
    window dict as given."""

    return {
        market: {
            "season": window
            if "blend" in window
            else {"blend": window, "components": {}, "missing_inputs": []}
        }
        for market, window in markets.items()
    }


def _leader_player(cid, name, scoring=20.0, **markets):
    player = _player(cid, name, scoring=scoring)
    player["scores"] = _windows(**markets)
    return player


def _leader(cid, name, market, value, scoring=20.0, thin=False):
    return {
        "canonical_id": cid,
        "name": name,
        "tricode": "AWY",
        "season_scoring": scoring,
        "market": market,
        "value": value,
        "thin": thin,
    }


def test_leaders_take_each_players_largest_cell_across_the_four_markets():
    players = [
        _leader_player(
            1, "Shooter", PTS=_cell(0.03), REB=_cell(-0.05), AST=_cell(0.02), **{"3PM": _cell(-0.18)}
        ),
        _leader_player(2, "Glass", PTS=_cell(0.01), REB=_cell(0.12, True), AST=_cell(-0.04)),
        _leader_player(3, "Passer", AST=_cell(-0.09)),
        _leader_player(4, "Scorer", PTS=_cell(0.07), **{"3PM": _cell(-0.02)}),
    ]

    game = _edges([_game()], {"G1": _matchup(players)})["games"][0]

    assert game["leaders"] == [
        _leader(1, "Shooter", "3PM", -0.18),
        _leader(2, "Glass", "REB", 0.12, thin=True),
        _leader(3, "Passer", "AST", -0.09),
        _leader(4, "Scorer", "PTS", 0.07),
    ]
    # The PTS effects are unchanged by the other markets.
    assert [(e["canonical_id"], e["value"]) for e in game["effects"]] == [(4, 0.07), (1, 0.03), (2, 0.01)]


EXCLUDED = {
    "STL": _cell(0.5),
    "BLK": _cell(-0.6),
    "STKS": _cell(0.55),
    "TOV": _cell(-0.45),
    "FTA": _cell(0.4),
    "FTM": _cell(-0.35),
}


def test_leaders_ignore_team_rate_markets_even_when_larger():
    players = [_leader_player(1, "Defender", REB=_cell(0.04), **EXCLUDED)]

    game = _edges([_game()], {"G1": _matchup(players)})["games"][0]

    assert game["leaders"] == [_leader(1, "Defender", "REB", 0.04)]


def test_a_combo_or_shot_volume_market_can_lead():
    players = [
        _leader_player(1, "Combo", PTS=_cell(0.05), REB=_cell(0.04), AST=_cell(0.03), PRA=_cell(0.11)),
        _leader_player(2, "Volume", PTS=_cell(-0.02), **{"3PM": _cell(-0.08), "FG3A": _cell(-0.13)}),
        _leader_player(3, "Twos", FGA=_cell(0.06), FG2M=_cell(0.07), FG2A=_cell(0.07)),
    ]

    game = _edges([_game()], {"G1": _matchup(players)})["games"][0]

    assert game["leaders"] == [
        _leader(2, "Volume", "FG3A", -0.13),
        _leader(1, "Combo", "PRA", 0.11),
        # FG2A and FG2M tie: FG2A comes first in market order.
        _leader(3, "Twos", "FG2A", 0.07),
    ]


@pytest.mark.parametrize(
    "market", ["PTS", "REB", "AST", "3PM", "PRA", "PR", "PA", "RA", "FGA", "FGM", "FG2A", "FG2M", "FG3A"]
)
def test_each_player_level_market_leads_when_it_is_the_largest_cell(market):
    others = {name: _cell(0.02) for name in ("PTS", "REB", "AST") if name != market}
    players = [_leader_player(1, "Solo", **others, **{market: _cell(-0.09)})]

    game = _edges([_game()], {"G1": _matchup(players)})["games"][0]

    assert game["leaders"] == [_leader(1, "Solo", market, -0.09)]


def test_a_player_with_only_team_rate_markets_is_no_leader():
    players = [_leader_player(1, "Defender", **EXCLUDED)]

    assert _edges([_game()], {"G1": _matchup(players)})["games"][0]["leaders"] == []


def test_leaders_need_eight_points_a_game():
    players = [
        _leader_player(1, "Exactly Eight", scoring=8.0, AST=_cell(0.03)),
        _leader_player(2, "Just Under", scoring=7.9, REB=_cell(0.5)),
        _leader_player(3, "Unknown Scoring", scoring=None, REB=_cell(0.4)),
    ]

    game = _edges([_game()], {"G1": _matchup(players)})["games"][0]

    assert game["leaders"] == [_leader(1, "Exactly Eight", "AST", 0.03, scoring=8.0)]


def test_leaders_are_capped_at_five_by_absolute_value():
    players = [
        _leader_player(cid, f"P{cid}", REB=_cell(value))
        for cid, value in ((1, 0.01), (2, -0.07), (3, 0.03), (4, 0.06), (5, -0.02), (6, 0.05), (7, 0.04))
    ]

    game = _edges([_game()], {"G1": _matchup(players)})["games"][0]

    assert game["leaders"] == [
        _leader(2, "P2", "REB", -0.07),
        _leader(4, "P4", "REB", 0.06),
        _leader(6, "P6", "REB", 0.05),
        _leader(7, "P7", "REB", 0.04),
        _leader(3, "P3", "REB", 0.03),
    ]


def test_leader_ties_break_on_scoring_then_canonical_id_and_markets_on_market_order():
    players = [
        _leader_player(9, "Low Scorer", scoring=10.0, AST=_cell(0.05)),
        _leader_player(8, "Late Id", scoring=25.0, **{"3PM": _cell(-0.05)}),
        _leader_player(7, "Early Id", scoring=25.0, REB=_cell(0.05)),
        # Equal |value| in every market: PTS comes first.
        _leader_player(6, "Even", scoring=12.0, PTS=_cell(-0.05), REB=_cell(0.05), AST=_cell(0.05)),
        # Equal |value| in REB and 3PM: REB comes first.
        _leader_player(5, "Split", scoring=12.0, REB=_cell(-0.05), **{"3PM": _cell(0.05)}),
    ]

    game = _edges([_game()], {"G1": _matchup(players)})["games"][0]

    assert game["leaders"] == [
        _leader(7, "Early Id", "REB", 0.05, scoring=25.0),
        _leader(8, "Late Id", "3PM", -0.05, scoring=25.0),
        _leader(5, "Split", "REB", -0.05, scoring=12.0),
        _leader(6, "Even", "PTS", -0.05, scoring=12.0),
        _leader(9, "Low Scorer", "AST", 0.05, scoring=10.0),
    ]


def test_a_leader_cell_is_the_blend_else_a_lone_component_without_missing_inputs():
    def window(blend=None, components=None, missing=()):
        return {"blend": blend, "components": components or {}, "missing_inputs": list(missing)}

    players = [
        # The Blend, not a larger disagreeing component.
        _leader_player(1, "Blended", AST=window(_cell(0.04), {"play_types": _cell(0.9)})),
        # No Blend, one component, nothing missing: the component.
        _leader_player(2, "Lone", AST=window(components={"traditional": _cell(-0.06, True)})),
        # No Blend and a named gap: no AST cell, so the smaller REB leads.
        _leader_player(
            3,
            "Withheld",
            REB=_cell(0.01),
            AST=window(components={"play_types": _cell(0.8)}, missing=["player_diet:assists"]),
        ),
        # Several components without a Blend: no AST cell.
        _leader_player(
            4, "Two Components", AST=window(components={"play_types": _cell(0.7), "shot_zones": _cell(0.6)})
        ),
    ]

    game = _edges([_game()], {"G1": _matchup(players)})["games"][0]

    assert game["leaders"] == [
        _leader(2, "Lone", "AST", -0.06, thin=True),
        _leader(1, "Blended", "AST", 0.04),
        _leader(3, "Withheld", "REB", 0.01),
    ]


def test_the_blend_value_not_a_components_is_the_score():
    player = _player(
        1,
        "Blended",
        blend=_cell(0.06),
        components={"play_types": _cell(0.91), "shot_zones": _cell(-0.40)},
    )

    game = _edges([_game()], {"G1": _matchup([player])})["games"][0]

    assert game["scores"] == [{"canonical_id": 1, "value": 0.06}]
    assert game["effects"][0]["value"] == 0.06


def test_several_components_without_a_blend_are_no_score():
    player = _player(
        1,
        "Two Components",
        components={"play_types": _cell(0.2), "shot_zones": _cell(0.4)},
    )

    game = _edges([_game()], {"G1": _matchup([player])})["games"][0]

    assert game["scores"] == []
    assert game["effects"] == []


def test_an_empty_slate_has_no_games_and_null_picks_and_reads_no_matchup():
    matchups = FakeMatchups({})
    service = SlateEdgesService(slates=FakeSlate([]), matchups=matchups)

    assert service.get_edges("2026-07-04") == {
        "slate_date": "2026-07-04",
        "games": [],
        "spotlight": {"best": None, "toughest": None},
        "trailing": [],
    }
    assert matchups.calls == []


def test_games_keep_the_slates_order_and_the_date_is_forwarded():
    slates = FakeSlate([_game("B"), _game("A")])
    service = SlateEdgesService(
        slates=slates,
        matchups=FakeMatchups({"A": _matchup([]), "B": _matchup([])}),
    )

    edges = service.get_edges(None)

    assert [game["game_id"] for game in edges["games"]] == ["B", "A"]
    assert slates.calls == [None]


class NeighbourSlate:
    """A Slate that names its next game date only when its neighbours are read."""

    def __init__(self, games_by_day, today="2026-04-10", next_day="2026-04-11"):
        self.games_by_day = games_by_day
        self.today = today
        self.next_day = next_day
        self.calls = []

    def get_slate(self, requested_date=None, *, neighbours=True):
        day = requested_date or self.today
        self.calls.append((day, neighbours))
        return {
            "slate_date": day,
            "games": self.games_by_day.get(day, []),
            "previous_game_date": None,
            "next_game_date": self.next_day if neighbours and day == self.today else None,
        }


def test_next_game_day_from_an_empty_day_answers_the_next_slate():
    service = SlateEdgesService(
        slates=NeighbourSlate({"2026-04-11": [_game("G1")]}),
        matchups=FakeMatchups({"G1": _matchup([_blend_player(1, "A", 0.05)])}),
    )

    edges = service.get_edges(fallback="next_game_day")

    assert edges["slate_date"] == "2026-04-11"
    assert [game["game_id"] for game in edges["games"]] == ["G1"]


def test_a_day_with_games_never_reads_its_neighbouring_dates():
    slates = NeighbourSlate({"2026-04-10": [_game("G1")]})
    service = SlateEdgesService(
        slates=slates,
        matchups=FakeMatchups({"G1": _matchup([_blend_player(1, "A", 0.05)])}),
    )

    service.get_edges(fallback="next_game_day")

    assert slates.calls == [("2026-04-10", False)]


def test_an_invalid_date_is_the_slates_invalid_input():
    service = SlateEdgesService(
        slates=FakeSlate([], error=InvalidInputError("The slate date must use YYYY-MM-DD.")),
        matchups=FakeMatchups({}),
    )

    with pytest.raises(InvalidInputError, match="YYYY-MM-DD"):
        service.get_edges("04-10-2026")


# --- the result cache and the serial compose ------------------------------

import threading  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from app.config.settings import RuntimeSettings  # noqa: E402

GENERATION = (("player_game_logs", "pub-1", 1, 7),)


class DictRedis:
    """The few Redis calls the cache makes, with the TTL it was given."""

    def __init__(self):
        self.values = {}
        self.ttls = {}

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, ttl, payload):
        self.values[key] = payload
        self.ttls[key] = ttl


class SnapshotComposer:
    """Composes from a snapshot, recording the order and how many ran at once."""

    def __init__(self, payloads, delay=0.0):
        self.payloads = payloads
        self.delay = delay
        self.calls = []
        self._live = 0
        self.peak = 0
        self._lock = threading.Lock()

    def get_matchup_from_snapshot(
        self, *, game_id, publication_snapshot, injuries, compose_cache=None, season=None
    ):
        with self._lock:
            self.calls.append(game_id)
            self._live += 1
            self.peak = max(self.peak, self._live)
        try:
            time.sleep(self.delay)
            return self.payloads[game_id]
        finally:
            with self._lock:
                self._live -= 1


class Publications:
    def __init__(self, generation=GENERATION):
        self.generation = generation

    def snapshot(self, stream_keys, *, season, **_):
        return SimpleNamespace(generation=self.generation, retained_history=())


def _cached_service(composer, redis_client, games, *, publications=None, settings=None, engine=None):
    return SlateEdgesService(
        slates=FakeSlate(games),
        matchups=composer,
        publication_reader=publications or Publications(),
        injuries=object(),
        settings=settings or RuntimeSettings(environment="testing", nba={"current_season": "2025-26"}),
        redis_client=redis_client,
        engine=engine,
    )


def _two_games():
    payloads = {
        "G1": _matchup([_blend_player(1, "A", 0.05)], {HOME: HOME_DEFENSE}),
        "G2": _matchup([_blend_player(2, "B", -0.04)], {HOME: HOME_DEFENSE}),
    }
    return [_game("G1"), _game("G2")], payloads


def test_a_cached_response_is_returned_without_composing_any_matchup():
    games, payloads = _two_games()
    redis_client = DictRedis()
    first = SnapshotComposer(payloads)
    cold = _cached_service(first, redis_client, games).get_edges("2026-04-10")
    second = SnapshotComposer(payloads)

    warm = _cached_service(second, redis_client, games).get_edges("2026-04-10")

    assert first.calls == ["G1", "G2"]
    assert second.calls == []
    assert warm == cold
    assert warm["spotlight"]["best"]["canonical_id"] == 1
    assert list(redis_client.ttls.values()) == [300]


def _assert_misses(
    games,
    payloads,
    *,
    second_games=None,
    publications=None,
    settings=None,
    first_date="2026-04-10",
    second_date="2026-04-10",
):
    """Prime the cache, change one input, and require a recomposed body."""

    redis_client = DictRedis()
    _cached_service(SnapshotComposer(payloads), redis_client, games).get_edges(first_date)
    composer = SnapshotComposer(payloads)
    service = _cached_service(composer, redis_client, second_games or games, publications=publications)
    if settings is not None:
        service.settings = settings
    body = service.get_edges(second_date)

    assert composer.calls == ["G1", "G2"]
    assert len(redis_client.values) == 2
    assert [game["game_id"] for game in body["games"]] == ["G1", "G2"]
    assert body["spotlight"]["best"]["canonical_id"] == 1


def test_a_new_publication_generation_misses_the_cache():
    games, payloads = _two_games()

    _assert_misses(
        games,
        payloads,
        publications=Publications((("player_game_logs", "pub-2", 1, 8),)),
    )


def test_a_changed_retained_history_misses_the_cache():
    games, payloads = _two_games()

    class Retained(Publications):
        def snapshot(self, stream_keys, *, season, **_):
            return SimpleNamespace(
                generation=self.generation,
                retained_history=(("player_game_logs", "old-row"),),
            )

    _assert_misses(games, payloads, publications=Retained())


def _settings(**scores):
    return RuntimeSettings(
        environment="testing",
        nba={"current_season": "2025-26"},
        matchup_scores=scores,
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"min_games": 6},
        {"play_types_min_volume_per_game": 100.0},
        {"shot_types_min_volume_per_game": 9.0},
    ],
)
def test_a_changed_score_threshold_misses_the_cache(changed):
    games, payloads = _two_games()

    _assert_misses(games, payloads, settings=_settings(**changed))


def test_a_changed_diet_baseline_threshold_misses_the_cache():
    games, payloads = _two_games()
    changed = RuntimeSettings(
        environment="testing",
        nba={"current_season": "2025-26"},
        player_diet_baseline={"play_types_min_volume_per_game": 60.0},
    )

    _assert_misses(games, payloads, settings=changed)


def test_a_game_changing_state_misses_the_cache():
    games, payloads = _two_games()
    finished = [dict(game, status={"state": "final"}) for game in games]

    _assert_misses(games, payloads, second_games=finished)


def test_a_moved_tip_time_misses_the_cache():
    games, payloads = _two_games()
    moved = [dict(game, scheduled_at="2026-04-11T01:00:00+00:00") for game in games]

    _assert_misses(games, payloads, second_games=moved)


def test_a_different_slate_date_misses_the_cache():
    games, payloads = _two_games()
    _assert_misses(games, payloads, first_date="2026-04-10", second_date="2026-04-11")


def test_a_disabled_flag_or_missing_client_never_caches():
    games, payloads = _two_games()
    redis_client = DictRedis()
    off = _cached_service(
        SnapshotComposer(payloads),
        redis_client,
        games,
        settings=RuntimeSettings(
            environment="testing",
            nba={"current_season": "2025-26"},
            cache={"slate_edges_enabled": False},
        ),
    )
    bodies = [off.get_edges("2026-04-10"), off.get_edges("2026-04-10")]
    no_client = _cached_service(SnapshotComposer(payloads), None, games).get_edges("2026-04-10")

    assert redis_client.values == {}
    assert off.matchups.calls == ["G1", "G2", "G1", "G2"]
    assert bodies[0] == bodies[1] == no_client
    assert no_client["spotlight"]["best"]["canonical_id"] == 1


def test_a_redis_failure_is_a_miss_not_an_error():
    import redis as redis_package

    class Down(DictRedis):
        def get(self, key):
            raise redis_package.exceptions.ConnectionError("down")

        def setex(self, key, ttl, payload):
            raise redis_package.exceptions.ConnectionError("down")

    games, payloads = _two_games()

    edges = _cached_service(SnapshotComposer(payloads), Down(), games).get_edges("2026-04-10")

    assert [game["game_id"] for game in edges["games"]] == ["G1", "G2"]


def test_matchups_compose_one_at_a_time_in_slate_order():
    games = [_game(f"G{i}") for i in range(6)]
    payloads = {f"G{i}": _matchup([_blend_player(i, f"P{i}", 0.01 * i)]) for i in range(6)}
    composer = SnapshotComposer(payloads, delay=0.01)

    edges = _cached_service(composer, None, games).get_edges("2026-04-10")

    assert composer.peak == 1
    assert composer.calls == [f"G{i}" for i in range(6)]
    assert [game["game_id"] for game in edges["games"]] == [f"G{i}" for i in range(6)]


@pytest.mark.parametrize("selected", [None, "2025-26"], ids=["omitted", "selected"])
def test_only_a_selected_season_composes_every_game_on_one_snapshot(selected):
    """As 3a1a7bb9: an omitted season composes each game on a scope of its own,
    and a selection's capture and games share one REPEATABLE READ scope."""

    from sqlalchemy import create_engine

    class ScopedComposer(SnapshotComposer):
        def get_matchup_from_snapshot(self, *, read_scope=None, **kwargs):
            self.scopes.append(read_scope)
            return super().get_matchup_from_snapshot(**kwargs)

    games, payloads = _two_games()
    composer = ScopedComposer(payloads)
    composer.scopes = []
    publications = Publications()
    publications.selected_season = selected
    service = _cached_service(composer, None, games, publications=publications)
    service._engine = create_engine("sqlite://")

    service.get_edges("2026-04-10")

    first, second = composer.scopes
    if selected is None:
        assert (first, second) == (None, None)
    else:
        assert first is not None
        assert second is first


def test_an_edges_miss_composes_fresh_and_never_stacks_a_cached_matchup():
    """A Matchup cached at t=0 must not be re-cached by an edges miss at t=299."""

    from app.services.matchup_reuse import ComposedMatchupCache
    from app.services.matchup_snapshot import SnapshotMatchups

    games, old = _two_games()
    new = {
        "G1": _matchup([_blend_player(1, "A", 0.01)], {HOME: HOME_DEFENSE}),
        "G2": _matchup([_blend_player(2, "B", -0.09)], {HOME: HOME_DEFENSE}),
    }
    redis_client = DictRedis()
    settings = RuntimeSettings(
        environment="testing",
        nba={"current_season": "2025-26"},
        cache={"slate_edges_enabled": False},
    )
    cache = ComposedMatchupCache(redis_client)
    snapshot = Publications().snapshot(("player_game_logs",), season="2025-26")
    # t=0: a Target resolution composes and caches both Matchups.
    resolver = SnapshotMatchups(
        SnapshotComposer(old),
        snapshot,
        object(),
        season="2025-26",
        cache=cache,
        settings=settings,
    )
    assert [resolver.get_matchup(game_id=g["game_id"], game=g)["players"][0]["name"] for g in games] == [
        "A",
        "B",
    ]
    # t=1: a stored injury changes what a fresh compose returns.
    composer = SnapshotComposer(new)
    service = _cached_service(composer, redis_client, games, settings=settings)
    service.matchup_cache = cache

    edges = service.get_edges("2026-04-10")

    assert composer.calls == ["G1", "G2"]
    assert edges["spotlight"]["best"]["value"] == 0.01
    assert edges["spotlight"]["toughest"]["value"] == -0.09
    # The fresh Matchups were written for the readers that do use the cache.
    later = SnapshotMatchups(
        SnapshotComposer(old),
        snapshot,
        object(),
        season="2025-26",
        cache=cache,
        settings=settings,
    )
    assert (
        later.get_matchup(game_id="G2", game=games[1])["players"][0]["scores"]["PTS"]["season"]["blend"][
            "value"
        ]
        == -0.09
    )


# --- the HTTP seam -------------------------------------------------------------


@pytest.fixture
def edges_service(dependencies):
    dependencies.slate_edges_service = Mock(name="slate_edges_service")
    return dependencies.slate_edges_service


def test_the_route_returns_the_edges_for_the_requested_date(client, authenticate, edges_service):
    body = {
        "slate_date": "2026-04-10",
        "games": [{"game_id": "G1", "effects": [], "scores": []}],
        "spotlight": {"best": None, "toughest": None},
    }
    edges_service.get_edges.return_value = body

    response = client.get("/api/games/slate/edges?date=2026-04-10", headers=authenticate())

    assert response.status_code == 200
    assert response.get_json() == body
    edges_service.get_edges.assert_called_once_with("2026-04-10", fallback=None)


def test_the_route_forwards_an_absent_date_as_none(client, authenticate, edges_service):
    edges_service.get_edges.return_value = {}

    client.get("/api/games/slate/edges", headers=authenticate())

    edges_service.get_edges.assert_called_once_with(None, fallback=None)


def test_the_route_reports_a_malformed_date_as_invalid_input(client, authenticate, dependencies):
    dependencies.slate_service.get_slate.side_effect = InvalidInputError(
        "The slate date must use YYYY-MM-DD."
    )
    dependencies.slate_edges_service = SlateEdgesService(slates=dependencies.slate_service, matchups=Mock())

    response = client.get("/api/games/slate/edges?date=16-01-2026", headers=authenticate())

    assert response.status_code == 400
    assert response.get_json()["error"] == {
        "code": "invalid_input",
        "message": "The slate date must use YYYY-MM-DD.",
    }


def test_the_route_serves_an_unauthenticated_caller(client, authenticate, edges_service):
    """Anonymous since crf04/statsplus#211, even with Firebase enforced."""
    authenticate()
    edges_service.get_edges.return_value = {"slate_date": "2026-04-10"}

    response = client.get("/api/games/slate/edges")

    assert response.status_code == 200
    assert response.get_json() == {"slate_date": "2026-04-10"}


def test_edges_recompose_when_one_providers_pool_stamp_moves():
    from app.services.matchup_reuse import ComposedMatchupCache

    games, payloads = _two_games()
    redis_client = DictRedis()
    settings = RuntimeSettings(
        environment="testing",
        nba={"current_season": "2025-26"},
        cache={"slate_edges_enabled": False},
    )
    composer = SnapshotComposer(payloads)

    class PooledSlate(FakeSlate):
        def __init__(self, games, underdog):
            super().__init__(games)
            self.underdog = underdog

        def get_slate(self, requested_date=None):
            slate = super().get_slate(requested_date)
            slate["freshness"] = {"pool": {"providers": {"underdog": {"retrieved_at": self.underdog}}}}
            return slate

    def edges(underdog):
        service = _cached_service(composer, redis_client, games, settings=settings)
        service.matchup_cache = ComposedMatchupCache(redis_client)
        service.slates = PooledSlate(games, underdog)
        service.get_edges("2026-04-10")

    edges("12:31")
    keys_first = set(redis_client.values)
    edges("12:32")

    assert len(set(redis_client.values) - keys_first) == 2


def test_edges_hand_the_slates_schedule_and_pool_blocks_to_the_matchup_reader():
    from app.services.matchup_snapshot import SnapshotMatchups

    games, payloads = _two_games()

    class Spy(SnapshotMatchups):
        seen: ClassVar = []

        def get_matchup(self, *, game_id, game=None, pool=None, schedule=None):
            self.seen.append((game_id, pool, schedule))
            return payloads[game_id]

    class Slate(FakeSlate):
        def get_slate(self, requested_date=None):
            slate = super().get_slate(requested_date)
            slate["freshness"] = {"schedule": {"retrieved_at": "S"}, "pool": {"p": 1}}
            return slate

    SlateEdgesService(slates=Slate(games), matchups=Spy(None, None, None)).get_edges("2026-04-10")

    assert Spy.seen == [
        ("G1", {"p": 1}, {"retrieved_at": "S"}),
        ("G2", {"p": 1}, {"retrieved_at": "S"}),
    ]


DIETS_TRAILING = {
    "family": "player_diets",
    "serving_season": "2025-26",
    "rule": "every team has played 10 games",
}
DEFENSE_TRAILING = {
    "family": "team_defense",
    "serving_season": "2025-26",
    "rule": "every team has played a game",
}


def test_the_trailing_families_of_every_games_matchup_are_listed_once_in_table_order():
    games = [_game("G1"), _game("G2"), _game("G3")]
    payloads = {
        "G1": {**_matchup([_blend_player(1, "A", 0.05)]), "trailing": [DEFENSE_TRAILING]},
        "G2": {**_matchup([_blend_player(2, "B", -0.04)]), "trailing": [DIETS_TRAILING, DEFENSE_TRAILING]},
        # An unreadable Matchup contributes nothing.
        "G3": ResourceNotFoundError("gone"),
    }

    assert _edges(games, payloads)["trailing"] == [DIETS_TRAILING, DEFENSE_TRAILING]


def test_a_cached_response_keeps_its_trailing_families_under_the_current_schema():
    games, payloads = _two_games()
    payloads["G1"] = {**payloads["G1"], "trailing": [DEFENSE_TRAILING]}
    redis_client = DictRedis()
    cold = _cached_service(SnapshotComposer(payloads), redis_client, games).get_edges("2026-04-10")

    warm = _cached_service(SnapshotComposer(payloads), redis_client, games).get_edges("2026-04-10")

    assert (cold["trailing"], warm["trailing"]) == ([DEFENSE_TRAILING], [DEFENSE_TRAILING])
    # A body cached before ``trailing`` (schema 2) or the preview cards'
    # ``players`` (schema 3) existed is never served.
    assert [key.split(":")[2] for key in redis_client.values] == ["v10"]


def test_the_edges_and_their_matchups_write_what_the_previous_release_wrote():
    """Every Redis key and stored payload (sha256 of its JSON text) of one edges
    miss, as 3a1a7bb9 wrote them before #671 changed how the reads are made,
    under the schemas #407 and #416 bumped for their Matchup scores.

    A change to either is a cache-format change: bump its schema instead.
    """

    import hashlib
    import zlib

    import tests.services.test_matchup_service as doubles
    from app.services.matchup_injuries import unavailable_injury_result
    from app.services.matchup_reuse import ComposedMatchupCache
    from app.services.player_game_log_repository import PlayerGameLogRepository
    from tests.test_matchup_reuse import _real_snapshot

    later = {**doubles._event(), "nba_game_id": "0022500585", "scheduled_at": "2026-01-16T03:00:00+00:00"}
    events = doubles.RecordedEvents(events=[doubles._event(), later])
    composer = doubles._service(events=events)
    repo = SimpleNamespace(_publication_reader=object())
    composer.player_logs.get_read_freshness = (
        lambda season, publication_snapshot: PlayerGameLogRepository.get_read_freshness(
            repo, season, publication_snapshot=publication_snapshot
        )
    )
    games = [composer._game(event) for event in events.events]

    class Slate:
        def get_slate(self, requested_date=None):
            return {"slate_date": requested_date, "games": games}

    class Publications:
        def snapshot(self, stream_keys, *, season, **_):
            return _real_snapshot(age=0)

    class Injuries:
        def get_injuries(self, *, event, season, pool_players):
            return unavailable_injury_result("disabled")

    redis_client = DictRedis()
    SlateEdgesService(
        slates=Slate(),
        matchups=composer,
        publication_reader=Publications(),
        injuries=Injuries(),
        settings=composer.settings,
        redis_client=redis_client,
        matchup_cache=ComposedMatchupCache(redis_client),
    ).get_edges("2026-01-15")

    assert {
        key: hashlib.sha256(zlib.decompress(payload)).hexdigest()
        for key, payload in redis_client.values.items()
    } == {
        # v12/v8 (statsplus#466): every Matchup carries its stored game_context;
        # v13/v10 (statsplus#459): rows may be Rotation Players;
        # v14 (statsplus#493): every Matchup's game_context carries its stored game_line.
        "matchup:composed:v14:d224a279f072a43eea8f594a250c1197c0e5cf53237e9a81a64740250939d8b5": (
            "6319622a4476f2688c3c7c632ed7768f4df865c563c0a77becdcbd315e4732b6"
        ),
        "matchup:composed:v14:958469890bfc578ee11146d706c50c971b768005fb08a3983729bafe2a09a975": (
            "bb8c59d79c8aac7fbe9fd66f7a9437f8ba74be2ae5b397d84ee5a306bfb4b325"
        ),
        # v9 (statsplus#470): each game carries ``leaders``.
        "slate:edges:v10:aa411456db22b7f601373770889ae6752ad8943bb0d67a099a57beede6a8e482": (
            "3b122d82fa39ffbd2cc84f8b61fc1b1a4e3779e6ff3a4e611c4e76599e2e53ec"
        ),
    }


class _SelectedPublications(Publications):
    selected_season = "2025-26"


@pytest.mark.parametrize("selected", (False, True), ids=("default season", "selected season"))
def test_the_slate_and_every_games_matchup_count_from_one_pool_read(selected):
    """With a selected season, the read is on the connection every game composes on."""

    from sqlalchemy import create_engine

    from app.services.player_pool import PlayerPool, PoolRoster

    games, payloads = _two_games()
    pools = {
        game_id: PlayerPool((), {}, {"status": game_id, "retrieved_at": None, "providers": {}})
        for game_id in ("G1", "G2")
    }

    class Read:
        season = "2025-26"
        game_ids = ("G1", "G2")

        def event(self, game_id):
            return None

        def pool(self, game_ids=None):
            return pools[next(iter(game_ids))]

        def roster(self, game_ids=None):
            return PoolRoster((), {}, {"status": "slate"})

    reads = []

    class Reader:
        def read_games(self, *, season, game_ids, connection=None):
            reads.append((season, tuple(game_ids), connection))
            return Read()

    class Slate(FakeSlate):
        def get_slate(self, requested_date=None, *, neighbours=True, player_pool=None):
            slate = super().get_slate(requested_date)
            slate["roster"] = player_pool.get_pool_roster(season="2025-26", game_ids=("G1", "G2"))
            return slate

    seen = []

    class Composer(SnapshotComposer):
        def get_matchup_from_snapshot(self, *, game_id, pool_read=None, read_scope=(None, None), **kwargs):
            seen.append((pool_read.pool((game_id,)).freshness["status"], read_scope[0]))
            return super().get_matchup_from_snapshot(game_id=game_id, **kwargs)

    service = _cached_service(
        Composer(payloads),
        None,
        games,
        publications=_SelectedPublications() if selected else None,
        engine=create_engine("sqlite://"),
    )
    service.slates = Slate(games)
    service.player_pool = Reader()

    service.get_edges("2026-04-10")

    ((season, game_ids, connection),) = reads
    assert (season, game_ids) == ("2025-26", ("G1", "G2"))
    assert seen == [("G1", connection), ("G2", connection)]
    assert (connection is not None) is selected


def test_a_player_with_no_pts_line_enters_the_edges_on_his_pts_score():
    """statsplus#457: a market without a line in CourtAI's data is still
    scored, so a player posted only in REB carries the same PTS edge as one
    posted in PTS."""

    from types import SimpleNamespace

    import tests.services.test_matchup_service as doubles
    from app.services.matchup_injuries import unavailable_injury_result
    from app.services.player_game_log_repository import PlayerGameLogRepository
    from app.services.statistic_catalog import StatisticCatalog
    from tests.test_matchup_reuse import _real_snapshot

    def edges(markets):
        composer = doubles._service(
            pool=doubles._posted_pool(markets), statistic_catalog=StatisticCatalog.load_default()
        )
        repo = SimpleNamespace(_publication_reader=object())
        composer.player_logs.get_read_freshness = (
            lambda season, publication_snapshot: PlayerGameLogRepository.get_read_freshness(
                repo, season, publication_snapshot=publication_snapshot
            )
        )
        games = [composer._game(doubles._event())]

        class Slate:
            def get_slate(self, requested_date=None):
                return {"slate_date": requested_date, "games": games}

        class Publications:
            def snapshot(self, stream_keys, *, season, **_):
                return _real_snapshot(age=0)

        class Injuries:
            def get_injuries(self, *, event, season, pool_players):
                return unavailable_injury_result("disabled")

        (game,) = SlateEdgesService(
            slates=Slate(),
            matchups=composer,
            publication_reader=Publications(),
            injuries=Injuries(),
            settings=composer.settings,
        ).get_edges("2026-01-15")["games"]
        return game

    unposted, posted = edges(("REB",)), edges(("PTS",))

    assert unposted["scores"] == [{"canonical_id": 2544, "value": -0.011875}]
    assert unposted == posted
