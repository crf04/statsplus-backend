"""Slate edges: per-game points effects and the slate's Best/Toughest picks.

The service composes the Slate and Matchup documents, so these tests hand it
fake Slate and Matchup seams whose documents carry only the fields the
Matchup contract names, and assert literal expected responses.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from app.errors import InvalidInputError, ProviderUnavailableError
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
        components={"play_types": _cell(value, thin)},
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
    service = SlateEdgesService(
        slates=FakeSlate(games), matchups=FakeMatchups(payloads)
    )
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
        {"canonical_id": 4, "name": "Huge Plus", "tricode": "AWY",
         "season_scoring": 20.0, "value": 0.11, "thin": False},
        {"canonical_id": 2, "name": "Big Minus", "tricode": "AWY",
         "season_scoring": 20.0, "value": -0.09, "thin": False},
        {"canonical_id": 3, "name": "Mid Plus", "tricode": "AWY",
         "season_scoring": 20.0, "value": 0.05, "thin": False},
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
        {"canonical_id": 4, "name": "Partial", "tricode": "AWY",
         "season_scoring": 20.0, "value": 0.07, "thin": False},
        {"canonical_id": 1, "name": "Blended", "tricode": "AWY",
         "season_scoring": 20.0, "value": 0.06, "thin": False},
        {"canonical_id": 2, "name": "Component", "tricode": "AWY",
         "season_scoring": 20.0, "value": -0.04, "thin": True},
    ]


def test_a_thin_blend_is_an_effect_but_never_a_pick():
    players = [_blend_player(1, "Thin Star", 0.2, thin=True)]

    edges = _edges(
        [_game()], {"G1": _matchup(players, {HOME: HOME_DEFENSE, AWAY: AWAY_DEFENSE})}
    )

    assert edges["games"][0]["effects"][0]["thin"] is True
    assert edges["spotlight"] == {"best": None, "toughest": None}


def test_a_single_component_is_never_a_pick():
    players = [_player(1, "Component", components={"traditional": _cell(0.3)})]

    edges = _edges(
        [_game()], {"G1": _matchup(players, {HOME: HOME_DEFENSE, AWAY: AWAY_DEFENSE})}
    )

    assert edges["games"][0]["effects"][0]["value"] == 0.3
    assert edges["spotlight"] == {"best": None, "toughest": None}


def test_picks_need_twelve_points_a_game():
    players = [
        _blend_player(1, "Eleven", 0.4, scoring=11.9),
        _blend_player(2, "Twelve", 0.1, scoring=12.0),
    ]

    edges = _edges(
        [_game()], {"G1": _matchup(players, {HOME: HOME_DEFENSE, AWAY: AWAY_DEFENSE})}
    )

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

    edges = _edges(
        [_game("G1"), _game("G2")], {"G1": g1, "G2": g2}
    )

    assert edges["spotlight"]["best"]["canonical_id"] == 3
    assert edges["spotlight"]["best"]["game_id"] == "G2"
    assert edges["spotlight"]["best"]["opponent"] == "AWY"
    assert edges["spotlight"]["best"]["tricode"] == "HOM"
    assert edges["spotlight"]["toughest"]["canonical_id"] == 4
    assert edges["spotlight"]["toughest"]["value"] == -0.12


def test_best_is_null_without_a_positive_pick_and_toughest_without_a_negative():
    only_negative = _matchup(
        [_blend_player(1, "Neg", -0.05)], {HOME: HOME_DEFENSE, AWAY: AWAY_DEFENSE}
    )
    only_positive = _matchup(
        [_blend_player(2, "Pos", 0.05)], {HOME: HOME_DEFENSE, AWAY: AWAY_DEFENSE}
    )

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

    pick = _edges(
        [_game()], {"G1": _matchup([player], {HOME: HOME_DEFENSE})}
    )["spotlight"]["best"]

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

    pick = _edges(
        [_game()], {"G1": _matchup([player], {HOME: HOME_DEFENSE})}
    )["spotlight"]["toughest"]

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
        1, "Runner", 0.05,
        slices=(("Transition", 0.4), ("Isolation", 0.3), ("Spotup", 0.1)),
    )
    defense = [
        _row("Transition", 5.0, 20),
        _row("Isolation", 8.0, 22),
        _row("Spotup", 90.0, 30),
    ]

    pick = _edges(
        [_game()], {"G1": _matchup([player], {HOME: defense})}
    )["spotlight"]["best"]

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


def test_a_player_with_no_readable_reason_gives_way_to_the_next_pick():
    unreadable = _blend_player(1, "No Reading", 0.2)
    readable = _blend_player(2, "Reading", 0.1)
    defense = {HOME: HOME_DEFENSE}

    # Player 1's slices are named differently, so the defense has no row.
    unreadable["diet_shares"]["play_types"] = [_slice("Cut", 0.5)]

    pick = _edges(
        [_game()], {"G1": _matchup([unreadable, readable], defense)}
    )["spotlight"]["best"]

    assert pick["canonical_id"] == 2


def test_a_game_that_cannot_be_read_has_no_effects_scores_or_pick_and_the_rest_succeed():
    good = _matchup(
        [_blend_player(7, "Survivor", 0.05, team=HOME, tricode=HOME_TRI)],
        {AWAY: AWAY_DEFENSE},
    )

    edges = _edges(
        [_game("BAD"), _game("GOOD")],
        {"BAD": ProviderUnavailableError("Stored matchup team facts are incomplete."),
         "GOOD": good},
    )

    assert edges["games"][0] == {"game_id": "BAD", "effects": [], "scores": []}
    assert [effect["canonical_id"] for effect in edges["games"][1]["effects"]] == [7]
    assert edges["spotlight"]["best"]["game_id"] == "GOOD"


def test_an_empty_slate_has_no_games_and_null_picks_and_reads_no_matchup():
    matchups = FakeMatchups({})
    service = SlateEdgesService(slates=FakeSlate([]), matchups=matchups)

    assert service.get_edges("2026-07-04") == {
        "slate_date": "2026-07-04",
        "games": [],
        "spotlight": {"best": None, "toughest": None},
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


def test_an_invalid_date_is_the_slates_invalid_input():
    service = SlateEdgesService(
        slates=FakeSlate([], error=InvalidInputError("The slate date must use YYYY-MM-DD.")),
        matchups=FakeMatchups({}),
    )

    with pytest.raises(InvalidInputError, match="YYYY-MM-DD"):
        service.get_edges("04-10-2026")


# --- the result cache and the concurrent compose ------------------------------

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
    """Composes from a snapshot, recording how many ran at once."""

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


def _cached_service(composer, redis_client, games, *, publications=None, workers=4):
    return SlateEdgesService(
        slates=FakeSlate(games),
        matchups=composer,
        publication_reader=publications or Publications(),
        injuries=object(),
        settings=RuntimeSettings(
            environment="testing", nba={"current_season": "2025-26"}
        ),
        redis_client=redis_client,
        max_workers=workers,
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


def test_a_new_publication_generation_misses_the_cache():
    games, payloads = _two_games()
    redis_client = DictRedis()
    _cached_service(SnapshotComposer(payloads), redis_client, games).get_edges("d")
    composer = SnapshotComposer(payloads)
    changed = Publications((("player_game_logs", "pub-2", 1, 8),))

    _cached_service(composer, redis_client, games, publications=changed).get_edges("d")

    assert composer.calls == ["G1", "G2"]
    assert len(redis_client.values) == 2


def test_a_game_changing_state_misses_the_cache():
    games, payloads = _two_games()
    redis_client = DictRedis()
    _cached_service(SnapshotComposer(payloads), redis_client, games).get_edges("d")
    finished = [dict(game, status={"state": "final"}) for game in games]
    composer = SnapshotComposer(payloads)

    _cached_service(composer, redis_client, finished).get_edges("d")

    assert composer.calls == ["G1", "G2"]


def test_a_different_slate_date_misses_the_cache():
    games, payloads = _two_games()
    redis_client = DictRedis()
    _cached_service(SnapshotComposer(payloads), redis_client, games).get_edges("2026-04-10")
    composer = SnapshotComposer(payloads)

    _cached_service(composer, redis_client, games).get_edges("2026-04-11")

    assert composer.calls == ["G1", "G2"]


def test_a_disabled_flag_or_missing_client_never_caches():
    games, payloads = _two_games()
    redis_client = DictRedis()
    off = _cached_service(SnapshotComposer(payloads), redis_client, games)
    off._cache_enabled = False
    off.get_edges("d")
    off.get_edges("d")
    no_client = _cached_service(SnapshotComposer(payloads), None, games)
    no_client.get_edges("d")

    assert redis_client.values == {}


def test_a_redis_failure_is_a_miss_not_an_error():
    import redis as redis_package

    class Down(DictRedis):
        def get(self, key):
            raise redis_package.exceptions.ConnectionError("down")

        def setex(self, key, ttl, payload):
            raise redis_package.exceptions.ConnectionError("down")

    games, payloads = _two_games()

    edges = _cached_service(SnapshotComposer(payloads), Down(), games).get_edges("d")

    assert [game["game_id"] for game in edges["games"]] == ["G1", "G2"]


def test_matchups_compose_concurrently_but_never_more_than_the_worker_bound():
    games = [_game(f"G{i}") for i in range(12)]
    payloads = {f"G{i}": _matchup([_blend_player(i, f"P{i}", 0.01 * i)]) for i in range(12)}
    composer = SnapshotComposer(payloads, delay=0.05)

    edges = _cached_service(composer, None, games, workers=3).get_edges("d")

    assert composer.peak == 3
    assert [game["game_id"] for game in edges["games"]] == [f"G{i}" for i in range(12)]


def test_the_first_game_composes_alone_before_the_workers_start():
    games = [_game(f"G{i}") for i in range(5)]
    payloads = {f"G{i}": _matchup([]) for i in range(5)}
    composer = SnapshotComposer(payloads, delay=0.05)

    _cached_service(composer, None, games, workers=4).get_edges("d")

    assert composer.calls[0] == "G0"
    assert composer.peak == 4


# --- the HTTP seam -------------------------------------------------------------


@pytest.fixture
def edges_service(dependencies):
    dependencies.slate_edges_service = Mock(name="slate_edges_service")
    return dependencies.slate_edges_service


def test_the_route_returns_the_edges_for_the_requested_date(
    client, authenticate, edges_service
):
    body = {
        "slate_date": "2026-04-10",
        "games": [{"game_id": "G1", "effects": [], "scores": []}],
        "spotlight": {"best": None, "toughest": None},
    }
    edges_service.get_edges.return_value = body

    response = client.get(
        "/api/games/slate/edges?date=2026-04-10", headers=authenticate()
    )

    assert response.status_code == 200
    assert response.get_json() == body
    edges_service.get_edges.assert_called_once_with("2026-04-10")


def test_the_route_forwards_an_absent_date_as_none(
    client, authenticate, edges_service
):
    edges_service.get_edges.return_value = {}

    client.get("/api/games/slate/edges", headers=authenticate())

    edges_service.get_edges.assert_called_once_with(None)


def test_the_route_reports_a_malformed_date_as_invalid_input(
    client, authenticate, dependencies
):
    dependencies.slate_service.get_slate.side_effect = InvalidInputError(
        "The slate date must use YYYY-MM-DD."
    )
    dependencies.slate_edges_service = SlateEdgesService(
        slates=dependencies.slate_service, matchups=Mock()
    )

    response = client.get(
        "/api/games/slate/edges?date=16-01-2026", headers=authenticate()
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == {
        "code": "invalid_input",
        "message": "The slate date must use YYYY-MM-DD.",
    }


def test_the_route_refuses_an_unauthenticated_caller(
    client, authenticate, edges_service
):
    authenticate()

    response = client.get("/api/games/slate/edges")

    assert response.status_code == 401
    edges_service.get_edges.assert_not_called()
