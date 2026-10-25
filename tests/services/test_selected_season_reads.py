"""A selected 2026-27 reads its held candidates as if the cutover had run (statsplus#330).

2025-26 is the Published Season; 2026-27 candidates are held.  Only a read
scoped to an explicit selection may serve them, and only a candidate that
passes the cutover's own per-publication checks.  Families that are not ready
keep their 2025-26 Publication labelled ``prior_season``; game logs that cannot
serve make the season unavailable.  Every test reads through the real reader,
repositories and services of ``tests/support/selected_season``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models.game_logs import GameLogQuery
from app.services.database_first_activation import ReadSeason, SeasonMode, SeasonUnavailable
from app.services.research_season import season_scoped
from tests.support.season_cutover import (
    L15_STREAM,
    NEW,
    NEW_CUTOFF,
    NEW_GAMES,
    NEW_PROVENANCE,
    OLD,
    PLAYER_LOGS,
    SEASON_STREAM,
)
from tests.support.selected_season import PLAYER_NAME, SelectedWorld

KEYS = (PLAYER_LOGS, SEASON_STREAM, L15_STREAM)


@pytest.fixture
def world(tmp_path):
    built = SelectedWorld(tmp_path)
    yield built
    built.engine.dispose()


def _reads(snapshot):
    return {
        key: (read.status, read.season, read.unavailable_reason, read.publication_id)
        for key, read in snapshot.reads.items()
    }


def _live(world, key):
    with world.engine.connect() as connection:
        return connection.scalar(
            text("SELECT active_publication_id FROM publication_pointers WHERE stream_key = :k"), {"k": key}
        )


def _logs(world, season, *, selected):
    service = season_scoped(world.games, season) if selected else world.games
    return service.get_filtered_logs(PLAYER_NAME, GameLogQuery(season_filter=season))


# --- the reader ------------------------------------------------------------


def test_an_opening_week_selection_reads_logs_from_2026_27_and_trails_team_defense(world):
    logs, _defense = world.hold_opening_week()

    snapshot = world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD))

    assert (snapshot.season, snapshot.mode, snapshot.published_season) == (
        NEW,
        SeasonMode.UNPUBLISHED,
        OLD,
    )
    assert _reads(snapshot) == {
        PLAYER_LOGS: ("candidate", NEW, None, logs.publication_id),
        # One game a team: team defense is not ready, so it keeps 2025-26.
        SEASON_STREAM: ("prior_season", OLD, None, _live(world, SEASON_STREAM)),
        # No 2026-27 Last 15 composed yet: it keeps 2025-26 until one serves.
        L15_STREAM: ("prior_season", OLD, None, _live(world, L15_STREAM)),
    }


def test_ten_games_a_team_serve_the_team_defense_candidate(world):
    logs, defense = world.hold_ten_games()

    snapshot = world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD))

    assert _reads(snapshot)[SEASON_STREAM] == ("candidate", NEW, None, defense.publication_id)
    assert _reads(snapshot)[PLAYER_LOGS] == ("candidate", NEW, None, logs.publication_id)


def test_a_coupled_family_member_that_fails_its_checks_holds_the_whole_family_back(world):
    world.hold_ten_games()
    l15 = world.compose_new_l15(NEW_GAMES, cutoff=NEW_CUTOFF, provenance=NEW_PROVENANCE)
    with world.engine.begin() as connection:
        connection.execute(
            text("UPDATE publication_versions SET checksum = 'corrupt' WHERE publication_id = :p"),
            {"p": l15.publication_id},
        )

    reads = _reads(world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD)))
    families = world.games.list_seasons()["choices"][0]["families"]

    assert reads[SEASON_STREAM] == ("prior_season", OLD, None, _live(world, SEASON_STREAM))
    assert reads[L15_STREAM] == ("prior_season", OLD, None, _live(world, L15_STREAM))
    assert families["last_15"] == {"served_season": OLD, "state": "trailing", "reason": "window_not_ready"}


def test_game_logs_that_fail_their_checks_make_the_season_unavailable(world):
    logs, _defense = world.hold_ten_games()
    with world.engine.begin() as connection:
        connection.execute(
            text("UPDATE publication_versions SET checksum = 'corrupt' WHERE publication_id = :p"),
            {"p": logs.publication_id},
        )

    with pytest.raises(SeasonUnavailable) as refused:
        world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD))

    assert (refused.value.season, refused.value.published_season) == (NEW, OLD)


def test_without_a_game_log_candidate_the_selection_is_unavailable(world):
    with pytest.raises(SeasonUnavailable):
        world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD))


def test_a_season_beyond_the_next_is_unavailable(world):
    world.hold_ten_games()

    with pytest.raises(SeasonUnavailable) as refused:
        world.reader.for_season("2027-28").snapshot(KEYS, season=ReadSeason(OLD))

    assert refused.value.season == "2027-28"


def test_each_candidate_is_checked_once_per_publication(world):
    world.hold_ten_games()
    checked = []
    validate = world.publications.season_candidate_refusal

    def counting(session, stream, candidate, **kwargs):
        checked.append(candidate.publication_id)
        return validate(session, stream, candidate, **kwargs)

    world.publications.season_candidate_refusal = counting
    for _ in range(3):
        world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD))

    assert len(checked) == len(set(checked)) == 2


def test_an_unscoped_reader_never_serves_a_candidate(world):
    world.hold_ten_games()

    assert world.reader.for_season(None) is world.reader
    snapshot = world.reader.snapshot(KEYS, season=ReadSeason(OLD))
    explicit = world.reader.snapshot(KEYS, season=ReadSeason(OLD, requested=NEW))

    assert (snapshot.season, snapshot.mode) == (OLD, SeasonMode.PUBLISHED)
    assert {read.status for read in snapshot.reads.values()} == {"active"}
    # A season string from an internal caller (injury scenarios) reads as today.
    assert _reads(explicit)[PLAYER_LOGS] == ("missing", OLD, "window_not_ready", _live(world, PLAYER_LOGS))


# --- game logs ---------------------------------------------------------------


def test_a_selected_2026_27_reads_its_game_logs_and_names_the_published_season(world):
    logs, _defense = world.hold_opening_week()

    body = _logs(world, NEW, selected=True)

    assert [(row["GAME_DATE"], row["MATCHUP"], row["PTS"]) for row in body["game_logs"]] == [
        ("2026-10-24", "ATL vs. BOS", 12)
    ]
    assert body["season_game_count"] == 1
    assert body["season_context"] == {"selected_season": NEW, "published_season": OLD}
    generation = {entry["stream_key"]: entry for entry in body["provenance"]["generation"]}
    assert (generation[PLAYER_LOGS]["publication_id"], generation[PLAYER_LOGS]["status"]) == (
        logs.publication_id,
        "candidate",
    )


def test_a_selected_published_season_reads_as_before_with_its_context(world):
    world.hold_opening_week()

    selected = _logs(world, OLD, selected=True)
    default = _logs(world, OLD, selected=False)

    assert selected.pop("season_context") == {"selected_season": OLD, "published_season": OLD}
    assert selected == default
    assert "season_context" not in default


def test_an_unselected_2026_27_read_stays_empty(world):
    """Injury scenarios read the schedule season's logs; held candidates must not leak in."""

    world.hold_opening_week()

    body = _logs(world, NEW, selected=False)

    assert body["game_logs"] == []
    assert "season_context" not in body


# --- /api/games/seasons -----------------------------------------------------


def test_choices_list_2026_27_only_while_its_game_logs_serve(world):
    assert world.games.list_seasons() == {
        "seasons": [OLD],
        "default_season": OLD,
        "choices": [
            {
                "season": OLD,
                "mode": "published",
                "families": {
                    "game_logs": {"served_season": OLD, "state": "serving"},
                    "player_diets": {"served_season": OLD, "state": "serving"},
                    "team_defense": {"served_season": OLD, "state": "serving"},
                    "last_15": {"served_season": OLD, "state": "serving"},
                },
            }
        ],
    }

    world.hold_opening_week()

    [unpublished, published] = world.games.list_seasons()["choices"]
    assert unpublished == {
        "season": NEW,
        "mode": "unpublished",
        "families": {
            "game_logs": {"served_season": NEW, "state": "serving"},
            # The Diet has no 2026-27 composition; team defense has one with a game a team.
            "player_diets": {"served_season": OLD, "state": "trailing", "reason": "season_not_composed"},
            "team_defense": {"served_season": OLD, "state": "trailing", "reason": "readiness_not_met"},
            "last_15": {"served_season": OLD, "state": "trailing", "reason": "season_not_composed"},
        },
        "team_games": {"min": 1, "max": 1},
    }
    assert published["season"] == OLD


# --- the HTTP boundary -------------------------------------------------------


@pytest.fixture
def served(world, dependencies):
    dependencies.game_service = world.games
    return world


def _get_logs(client, season):
    return client.get(f"/api/games/game_logs?player_name=Player+0&season_filter={season}")


def test_the_route_serves_a_selected_2026_27_with_its_context(served, client):
    served.hold_opening_week()

    response = _get_logs(client, NEW)

    assert response.status_code == 200
    body = response.get_json()
    assert [row["PTS"] for row in body["game_logs"]] == [12]
    assert body["season_context"] == {"selected_season": NEW, "published_season": OLD}


def test_the_route_refuses_an_unoffered_season_naming_the_published_one(served, client):
    response = _get_logs(client, NEW)

    assert response.status_code == 503
    error = response.get_json()["error"]
    assert (error["code"], error["details"]) == (
        "season_unavailable",
        {"selected_season": NEW, "published_season": OLD},
    )


def test_the_route_rejects_a_malformed_season(served, client):
    response = _get_logs(client, "2026-28")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"


def test_the_route_without_a_season_adds_no_context(served, client):
    served.hold_opening_week()

    body = client.get("/api/games/game_logs?player_name=Player+0").get_json()

    assert [row["PTS"] for row in body["game_logs"]] == [30]
    assert "season_context" not in body


def test_team_games_are_the_fewest_and_most_games_any_team_has(world):
    logs, _defense = world.hold_ten_games()
    with world.engine.begin() as connection:
        # Every row of the first game: its two teams now show nine games.
        connection.execute(
            text("DELETE FROM publication_player_game_logs WHERE publication_id = :p AND game_id = :g"),
            {"p": logs.publication_id, "g": NEW_GAMES[0][0]},
        )

    [unpublished, _published] = world.games.list_seasons()["choices"]

    assert unpublished["team_games"] == {"min": 9, "max": 10}
