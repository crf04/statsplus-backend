"""Selected Season reads on PostgreSQL (crf04/statsplus#330).

A selected 2026-27 game-log, Matchup or selection read captures a held
candidate, then reads its rows from the candidate's projection.  A recomposition that supersedes the
candidate and a prune that deletes it can both commit in between; the request
reads one database snapshot, so its answer is the one it captured.  Skipped
unless ``TEST_DATABASE_URL`` names a disposable database.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.models import Base
from app.models.game_logs import GameLogQuery
from app.services.research_season import season_scoped
from tests.support.season_cutover import NEW, OLD
from tests.support.selected_season import GAME_ID, PLAYER_ID, PLAYER_NAME, SelectedWorld

pytestmark = pytest.mark.integration


def _clean(engine):
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))


@pytest.fixture
def world(tmp_path):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url)
    _clean(engine)
    built = SelectedWorld(tmp_path, engine=engine)
    yield built
    _clean(engine)
    engine.dispose()


def _logs(world):
    body = season_scoped(world.games, NEW).get_filtered_logs(PLAYER_NAME, GameLogQuery(season_filter=NEW))
    generation = {entry["stream_key"]: entry for entry in body["provenance"]["generation"]}
    return (
        [(row["GAME_DATE"], row["PTS"]) for row in body["game_logs"]],
        body["season_game_count"],
        generation["player_game_logs"]["publication_id"],
        body["season_context"],
    )


def _versions(world, publication_id):
    with world.engine.connect() as connection:
        return connection.scalar(
            text("SELECT count(*) FROM publication_versions WHERE publication_id = :p"), {"p": publication_id}
        )


def test_a_prune_between_capture_and_projection_read_leaves_the_answer_unchanged(world):
    held, _defense = world.hold_opening_week()
    projected = world.logs._projected_player_rows
    raced = []

    def supersede_and_prune_first(*args, **kwargs):
        if not raced:
            raced.append(world.hold_ten_games()[0].publication_id)
            world.publications.prune_history(stream_key="player_game_logs")
        return projected(*args, **kwargs)

    world.logs._projected_player_rows = supersede_and_prune_first

    answer = _logs(world)

    assert answer == (
        [("2026-10-24", 12)],
        1,
        held.publication_id,
        {"selected_season": NEW, "published_season": OLD},
    )
    # The race happened: the captured candidate is gone, and the next read
    # serves the one that replaced it.
    assert _versions(world, held.publication_id) == 0
    world.logs._projected_player_rows = projected
    assert _logs(world)[2] == raced[0]


def test_choices_and_a_selected_read_on_postgres(world):
    logs, _defense = world.hold_opening_week()

    [unpublished, published] = world.games.list_seasons()["choices"]
    answer = _logs(world)

    assert (unpublished["season"], unpublished["mode"], unpublished["team_games"]) == (
        NEW,
        "unpublished",
        {"min": 1, "max": 1},
    )
    assert unpublished["families"]["team_defense"] == {
        "served_season": OLD,
        "state": "trailing",
        "reason": "readiness_not_met",
    }
    assert (published["season"], published["mode"]) == (OLD, "published")
    assert answer[2] == logs.publication_id


def _race_once(world, read):
    """``read`` that first supersedes and prunes the held game-log candidate, once."""

    raced = []

    def raced_read(*args, **kwargs):
        if not raced:
            raced.append(world.hold_ten_games()[0].publication_id)
            world.publications.prune_history(stream_key="player_game_logs")
        return read(*args, **kwargs)

    return raced, raced_read


def test_a_matchup_keeps_the_candidate_it_captured_through_a_prune(world):
    held, _defense = world.hold_opening_week()
    logs = world.service.player_logs
    summaries = logs.get_player_summaries
    raced, logs.get_player_summaries = _race_once(world, summaries)

    body = season_scoped(world.service, NEW).get_matchup(game_id=GAME_ID)

    generation = {entry["stream_key"]: entry for entry in body["provenance"]["generation"]}
    [player] = body["players"]
    assert (generation["player_game_logs"]["publication_id"], player["season_scoring"]) == (
        held.publication_id,
        12.0,
    )
    assert _versions(world, held.publication_id) == 0
    assert raced


def test_a_selection_keeps_the_candidate_it_captured_through_a_prune(world):
    world.hold_opening_week()
    h2h = world.logs.list_h2h_rows
    raced, world.logs.list_h2h_rows = _race_once(world, h2h)

    body = season_scoped(world.selections, NEW).get_selection(game_id=GAME_ID, player_id=PLAYER_ID)

    rows = [
        (row["game_date"], row["stats"]["STKS"]) for row in body["h2h"]["rows"] if row["row_type"] == "game"
    ]
    assert rows == [("2026-10-24", 4.0)]
    assert body["season_context"] == {"selected_season": NEW, "published_season": OLD}
    assert raced
