"""``season`` on Matchup, selection, Slate, edges, profile and baselines (statsplus#330 B2).

2025-26 is the Published Season and 2026-27 candidates are held.  An explicit
``season`` reads through ``reader.for_season`` exactly as game logs do (B1):
2026-27 serves its game-log candidate, families that are not ready trail on
2025-26 with their sample, and a season that is not offered is refused.  The
game's own schedule facts follow the game, so 2026-27 evidence for a 2025-26
game is refused.  Every test reads through the real reader, repositories and
services of ``tests/support/selected_season``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event

from app.models.game_logs import GameLogQuery
from app.services.database_first_activation import ReadSeason, TeamGames
from app.services.matchup_snapshot import SnapshotMatchups, read_game_matchup, request_matchups
from app.services.research_season import season_scoped
from tests.services.test_serving_season_matchup import _game_event
from tests.support.season_cutover import NEW, OLD, PLAYER_LOGS
from tests.support.selected_season import (
    GAME_ID,
    OLD_GAME_ID,
    OLD_SLATE_DATE,
    PLAYER_ID,
    PLAYER_NAME,
    SelectedWorld,
    _StoredInjuries,
)

SLATE_DATE = "2026-10-26"
CONTEXT = {"selected_season": NEW, "published_season": OLD}
ONE_GAME_A_TEAM = {"min": 1, "max": 1}


def _trailing(family, rule, team_games=ONE_GAME_A_TEAM):
    return {"family": family, "serving_season": OLD, "rule": rule, "team_games": team_games}


DIETS = _trailing("player_diets", "every team has played 10 games")
TEAM_DEFENSE = _trailing("team_defense", "every team has played 10 games")
LAST_15 = _trailing("last_15", "its first window of the season is composed")


@pytest.fixture
def world(tmp_path):
    built = SelectedWorld(tmp_path)
    yield built
    built.engine.dispose()


@pytest.fixture
def served(world, dependencies):
    dependencies.game_service = world.games
    dependencies.matchup_service = world.service
    dependencies.matchup_selection_service = world.selections
    dependencies.slate_service = world.slates
    dependencies.slate_edges_service = world.edges
    dependencies.player_service = world.profiles
    dependencies.diet_baselines_service = world.baselines
    return world


ROUTES = {
    "matchup": f"/api/games/matchup?game_id={GAME_ID}",
    "selection": f"/api/games/matchup/selection?game_id={GAME_ID}&player_id={PLAYER_ID}",
    "slate": f"/api/games/slate?date={SLATE_DATE}",
    "slate_edges": f"/api/games/slate/edges?date={SLATE_DATE}",
    "profile": "/api/players/profile?player_name=Player+0&category=Shooting+Type",
    "diet_baselines": "/api/diet/baselines?",
}
#: Slate, edges and profile also answer anonymous callers.
ANONYMOUS = ("slate", "slate_edges", "profile")


def _with_season(path):
    return path + ("season=" if path.endswith("?") else "&season=")


def _generation(body, stream_key):
    return [
        (entry["publication_id"], entry["status"])
        for entry in body["provenance"]["generation"]
        if entry["stream_key"] == stream_key
    ]


def _refusal(response):
    error = response.get_json()["error"]
    return response.status_code, error["code"], error.get("details")


# --- the contract on every route --------------------------------------------


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_every_route_refuses_a_malformed_season(served, client, authenticate, route):
    response = client.get(f"{_with_season(ROUTES[route])}2026-28", headers=authenticate())

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_every_route_refuses_2026_27_while_it_is_not_offered(served, client, authenticate, route):
    response = client.get(f"{_with_season(ROUTES[route])}{NEW}", headers=authenticate())

    assert _refusal(response) == (503, "season_unavailable", CONTEXT)


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_every_route_names_an_explicit_published_season_in_its_context(served, client, authenticate, route):
    served.hold_opening_week()

    body = client.get(f"{_with_season(ROUTES[route])}{OLD}", headers=authenticate()).get_json()

    assert body["season_context"] == {"selected_season": OLD, "published_season": OLD}


@pytest.mark.parametrize("route", ANONYMOUS)
def test_an_anonymous_caller_cannot_select_a_held_season(served, client, route):
    served.hold_opening_week()

    response = client.get(f"{_with_season(ROUTES[route])}{NEW}")

    assert _refusal(response) == (503, "season_unavailable", CONTEXT)


# --- Matchup ------------------------------------------------------------------


def test_a_selected_2026_27_matchup_scores_from_its_game_logs_and_trails_the_rest(served, client):
    logs, _defense = served.hold_opening_week()

    body = client.get(f"{ROUTES['matchup']}&season={NEW}").get_json()

    assert body["season_context"] == CONTEXT
    assert _generation(body, PLAYER_LOGS) == [(logs.publication_id, "candidate")]
    # One game a team: no family but the game logs is ready, so each trails
    # on 2025-26, whose game logs have one game a team.
    assert body["trailing"] == [DIETS, TEAM_DEFENSE, LAST_15]
    # The game's own schedule facts are still the 2026-27 game's.
    assert body["game"]["game_id"] == GAME_ID


def _web_app_decodes(body):
    """The season rule of the web app's Matchup decoder (statsplus-frontend#353, ``matchupApi.js``).

    ``as_of.season`` is the Published Season, so a ``season_context`` naming
    another Published Season is refused as invalid.
    """

    context = body.get("season_context")
    return context is None or body["as_of"]["season"] == context["published_season"]


@pytest.mark.parametrize(
    ("state", "published", "season_defense"),
    [
        ("hold_opening_week", OLD, OLD),
        ("hold_ten_games", OLD, NEW),
        ("opening_week_cutover", NEW, OLD),
    ],
)
def test_a_selected_2026_27_matchup_keeps_as_of_on_the_published_season(
    served, client, state, published, season_defense
):
    getattr(served, state)()

    body = client.get(f"{ROUTES['matchup']}&season={NEW}").get_json()

    assert _web_app_decodes(body)
    assert (body["as_of"]["season"], body["season_context"]) == (
        published,
        {"selected_season": NEW, "published_season": published},
    )
    # Each input names the season it serves on its own section.
    assert body["experience"]["sections"]["season_defense"]["season"] == season_defense


def test_ten_games_a_team_serve_team_defense_and_the_rest_trail_with_their_own_sample(served, client):
    served.hold_ten_games()

    body = client.get(f"{ROUTES['matchup']}&season={NEW}").get_json()

    assert body["trailing"] == [DIETS, LAST_15]


def test_once_2026_27_is_published_its_trailing_families_name_2025_26s_sample(served, client):
    served.opening_week_cutover()

    selected = client.get(f"{ROUTES['matchup']}&season={NEW}").get_json()
    omitted = client.get(ROUTES["matchup"]).get_json()

    assert selected["trailing"] == [DIETS, TEAM_DEFENSE]
    assert selected["season_context"] == {"selected_season": NEW, "published_season": NEW}
    # Without a selection the same entries carry no sample.
    assert omitted["trailing"] == [
        {key: value for key, value in entry.items() if key != "team_games"} for entry in (DIETS, TEAM_DEFENSE)
    ]
    assert "season_context" not in omitted


def test_only_a_selected_capture_counts_its_trailing_sample(served):
    """An omitted read pays no team-games statement: game logs name their season unscoped."""

    served.opening_week_cutover()
    keys = (PLAYER_LOGS, "traditional_opponent_season")

    unscoped = served.reader.snapshot(keys, season=ReadSeason(NEW, requested=NEW))
    selected = served.reader.for_season(NEW).snapshot(keys, season=ReadSeason(NEW))

    assert unscoped.reads["traditional_opponent_season"].status == "prior_season"
    assert (dict(unscoped.team_games), dict(selected.team_games)) == ({}, {OLD: TeamGames(min=1, max=1)})


@pytest.mark.parametrize(
    "path",
    [
        f"/api/games/matchup?game_id={OLD_GAME_ID}",
        f"/api/games/matchup/selection?game_id={OLD_GAME_ID}&player_id={PLAYER_ID}",
        f"/api/games/slate?date={OLD_SLATE_DATE}",
        f"/api/games/slate/edges?date={OLD_SLATE_DATE}",
    ],
    ids=["matchup", "selection", "slate", "slate_edges"],
)
def test_2026_27_evidence_for_a_2025_26_game_is_refused(served, client, authenticate, path):
    served.hold_opening_week()

    response = client.get(f"{path}&season={NEW}", headers=authenticate())

    assert _refusal(response) == (
        400,
        "season_after_game",
        {"selected_season": NEW, "game_season": OLD},
    )


def test_a_matchup_selection_and_slate_edges_read_without_writing(served):
    served.hold_opening_week()
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    event.listen(served.engine, "before_cursor_execute", record)
    try:
        season_scoped(served.service, NEW).get_matchup(game_id=GAME_ID)
        season_scoped(served.selections, NEW).get_selection(game_id=GAME_ID, player_id=PLAYER_ID)
        season_scoped(served.edges, NEW).get_edges(SLATE_DATE)
    finally:
        event.remove(served.engine, "before_cursor_execute", record)

    assert statements
    assert set(statements) <= {"SELECT", "WITH", "SAVEPOINT", "RELEASE", "ROLLBACK"}


# --- selection --------------------------------------------------------------


def test_a_selected_2026_27_selection_reads_its_head_to_head_from_2026_27(served, client):
    served.hold_opening_week()

    selected = client.get(f"{ROUTES['selection']}&season={NEW}").get_json()
    omitted = client.get(ROUTES["selection"]).get_json()

    def head_to_head(body):
        return [
            (row["game_date"], row["stats"]["STKS"])
            for row in body["h2h"]["rows"]
            if row["row_type"] == "game"
        ]

    # 2025-26: one steal and two blocks; 2026-27: three steals and one block.
    assert head_to_head(omitted) == [("2026-04-13", 3.0)]
    assert head_to_head(selected) == [("2026-10-24", 4.0)]
    assert selected["season_context"] == CONTEXT
    assert "season_context" not in omitted


# --- Slate and slate edges ------------------------------------------------------


def test_a_selected_2026_27_slate_lists_its_trailing_families_with_their_sample(served, client, authenticate):
    served.hold_opening_week()

    selected = client.get(f"{ROUTES['slate']}&season={NEW}", headers=authenticate()).get_json()
    omitted = client.get(ROUTES["slate"]).get_json()

    assert (selected["season_context"], selected["trailing"]) == (CONTEXT, [DIETS, TEAM_DEFENSE, LAST_15])
    assert [game["game_id"] for game in selected["games"]] == [GAME_ID]
    assert (omitted["trailing"], "season_context" in omitted) == ([], False)


def test_selected_slate_edges_compose_2026_27_and_list_its_trailing_families(served, client, authenticate):
    served.hold_opening_week()

    body = client.get(f"{ROUTES['slate_edges']}&season={NEW}", headers=authenticate()).get_json()

    assert (body["season_context"], body["trailing"]) == (CONTEXT, [DIETS, TEAM_DEFENSE, LAST_15])


def test_an_anonymous_caller_may_name_the_published_season(served, client):
    served.hold_opening_week()

    body = client.get(f"{ROUTES['slate']}&season={OLD}").get_json()

    assert (body["season_context"], body["trailing"]) == (
        {"selected_season": OLD, "published_season": OLD},
        [],
    )


# --- caches ---------------------------------------------------------------------


@pytest.mark.parametrize("first", ["omitted", "selected"])
def test_slate_edges_never_serve_one_seasons_answer_to_the_other(served, client, authenticate, first):
    served.hold_opening_week()
    selected_path = f"{ROUTES['slate_edges']}&season={NEW}"
    order = [ROUTES["slate_edges"], selected_path]
    if first == "selected":
        order.reverse()

    bodies = {path: client.get(path, headers=authenticate()).get_json() for path in order}

    assert bodies[selected_path]["trailing"] == [DIETS, TEAM_DEFENSE, LAST_15]
    assert bodies[ROUTES["slate_edges"]]["trailing"] == []
    assert "season_context" not in bodies[ROUTES["slate_edges"]]


def test_published_slate_edges_with_and_without_a_selection_are_cached_apart(served, client, authenticate):
    """Same season, same generation: only the selection tells the two apart."""

    served.opening_week_cutover()
    selected_path = f"{ROUTES['slate_edges']}&season={NEW}"

    omitted = client.get(ROUTES["slate_edges"]).get_json()
    selected = client.get(selected_path, headers=authenticate()).get_json()
    omitted_again = client.get(ROUTES["slate_edges"]).get_json()

    assert selected["trailing"] == [DIETS, TEAM_DEFENSE]
    assert (
        omitted["trailing"]
        == omitted_again["trailing"]
        == [
            {key: value for key, value in entry.items() if key != "team_games"}
            for entry in (DIETS, TEAM_DEFENSE)
        ]
    )


def test_a_composed_matchup_cached_for_2026_27_is_never_served_for_2025_26(served):
    served.hold_opening_week()
    game = {
        "game_id": GAME_ID,
        "status": {"state": "scheduled"},
        "scheduled_at": _game_event()["scheduled_at"],
    }

    def compose(reader):
        matchups = request_matchups(
            composer=served.service,
            publication_reader=reader,
            injuries=_StoredInjuries(),
            settings=served.edges.settings,
            cache=served.edges.matchup_cache,
        )
        assert isinstance(matchups, SnapshotMatchups)
        return read_game_matchup(matchups, game)

    selected = compose(served.reader.for_season(NEW))
    omitted = compose(served.reader)
    selected_again = compose(served.reader.for_season(NEW))

    assert _generation(omitted, PLAYER_LOGS) == [(served.live(PLAYER_LOGS), "active")]
    assert _generation(selected, PLAYER_LOGS) == _generation(selected_again, PLAYER_LOGS)
    assert _generation(selected, PLAYER_LOGS)[0][1] == "candidate"
    # Both name the Published Season; the generation tells them apart.
    assert omitted["as_of"]["season"] == selected_again["as_of"]["season"] == OLD


# --- profile and baselines ------------------------------------------------------


def test_a_selected_profile_wraps_todays_body_with_its_context(served, client, authenticate):
    served.hold_opening_week()

    selected = client.get(f"{ROUTES['profile']}&season={NEW}", headers=authenticate()).get_json()
    omitted = client.get(ROUTES["profile"]).get_json()

    assert selected == {"data": [], "season_context": CONTEXT}
    assert omitted == []


def test_selected_baselines_report_the_season_their_diets_serve(served, client):
    served.hold_opening_week()

    selected = client.get(f"{ROUTES['diet_baselines']}season={NEW}").get_json()
    omitted = client.get(ROUTES["diet_baselines"]).get_json()

    assert (selected["season_context"], selected["season"], selected["trailing"]) == (CONTEXT, OLD, [DIETS])
    # The Diets trail: 2025-26's shot-type baseline.
    assert selected["shares"]["shot_types"]["Pullups"] == 0.3
    assert (omitted["season"], "trailing" in omitted, "season_context" in omitted) == (OLD, False, False)


def test_baselines_selected_after_the_cutover_still_name_their_trailing_diets(served, client):
    served.opening_week_cutover()

    body = client.get(f"{ROUTES['diet_baselines']}season={NEW}").get_json()

    assert (body["season"], body["trailing"]) == (OLD, [DIETS])
    assert body["season_context"] == {"selected_season": NEW, "published_season": NEW}


# --- game logs (B1) carry the sample too ----------------------------------------


def test_selected_game_logs_list_their_trailing_families_with_their_sample(served):
    served.hold_ten_games()

    selected = season_scoped(served.games, NEW).get_filtered_logs(
        PLAYER_NAME, GameLogQuery(season_filter=NEW)
    )
    omitted = served.games.get_filtered_logs(PLAYER_NAME, GameLogQuery(season_filter=OLD))

    # Game logs read the Diets and the Season rankings, not Last 15; ten
    # games a team serve the team-defense candidate, so only the Diets trail.
    assert selected["trailing"] == [DIETS]
    assert "trailing" not in omitted


# --- cost -----------------------------------------------------------------------


def test_slate_edges_judge_each_candidate_once_however_many_games_they_score(served, client, authenticate):
    served.hold_ten_games()
    second = {**_game_event(), "nba_game_id": "0022600161", "scheduled_at": "2026-10-27T00:00:00+00:00"}
    served.slates.event_catalog.events_by_season[NEW].append(second)
    served.service.event_catalog = served.slates.event_catalog
    judged = []
    lineage = served.publications.season_candidate_lineage_refusal

    def counted(session, stream, candidate):
        judged.append(candidate.stream_key)
        return lineage(session, stream, candidate)

    served.publications.season_candidate_lineage_refusal = counted

    body = client.get(f"{ROUTES['slate_edges']}&season={NEW}", headers=authenticate()).get_json()

    assert [game["game_id"] for game in body["games"]] == [GAME_ID, "0022600161"]
    # Both candidates pass their memoized checks; each one's ledger lineage is
    # rechecked once for the request, not once per game.
    assert sorted(judged) == sorted([PLAYER_LOGS, "traditional_opponent_season"])
