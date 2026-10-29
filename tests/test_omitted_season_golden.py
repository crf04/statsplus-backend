"""With no season chosen, every response is byte for byte what it was (statsplus#330 AC1).

The goldens were recorded from master before a selection served candidates
whose Readiness Rule does not hold (statsplus#386), over a world that
publishes 2025-26 and holds opening-week 2026-27 candidates of the game logs,
team defense, play-type defense and player Synergy, and whose reader has
already served them to a selection, so a default read that touched a held
candidate or a selection's cached verdict would change them.  Publication IDs are random per run and are replaced, in order of
first appearance, before comparing.  ``GOLDEN_RECORD=1`` rewrites the files.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from app.services.database_first_activation import ReadSeason
from tests.support.season_cutover import (
    L15_STREAM,
    NEW,
    OLD,
    PLAYER_LOGS,
    SEASON_STREAM,
    THIN_CUTOFF,
    THIN_GAMES,
    THIN_PROVENANCE,
)
from tests.support.selected_season import (
    GAME_ID,
    PLAY_TYPE_DEFENSE,
    PLAYER_ID,
    PLAYER_NAME,
    PLAYER_SYNERGY,
    SelectedWorld,
)

GOLDEN = Path(__file__).parent / "fixtures" / "omitted_season"
SELECTED_KEYS = (PLAYER_LOGS, SEASON_STREAM, L15_STREAM, PLAY_TYPE_DEFENSE, PLAYER_SYNERGY)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture
def world(tmp_path, dependencies):
    built = SelectedWorld(tmp_path)
    built.publish_play_types()
    built.hold_opening_week()
    for stream_key in (PLAY_TYPE_DEFENSE, PLAYER_SYNERGY):
        built.compose_new_play_types(stream_key, THIN_GAMES, cutoff=THIN_CUTOFF, provenance=THIN_PROVENANCE)
    built.reader.for_season(NEW).snapshot(SELECTED_KEYS, season=ReadSeason(OLD))
    dependencies.game_service = built.games
    dependencies.matchup_service = built.service
    dependencies.slate_service = built.slates
    dependencies.matchup_selection_service = built.selections
    dependencies.slate_edges_service = built.edges
    dependencies.player_service = built.profiles
    dependencies.diet_baselines_service = built.baselines
    yield built
    built.engine.dispose()


def _stable(body: bytes) -> str:
    text = body.decode()
    names: dict[str, str] = {}
    return _UUID.sub(lambda match: names.setdefault(match.group(0), f"publication-{len(names) + 1}"), text)


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("game_logs", f"/api/games/game_logs?player_name={PLAYER_NAME.replace(' ', '+')}"),
        ("matchup", f"/api/games/matchup?game_id={GAME_ID}"),
        ("slate", "/api/games/slate?date=2026-10-26"),
        ("selection", f"/api/games/matchup/selection?game_id={GAME_ID}&player_id={PLAYER_ID}"),
        ("slate_edges", "/api/games/slate/edges?date=2026-10-26"),
        (
            "profile",
            f"/api/players/profile?player_name={PLAYER_NAME.replace(' ', '+')}&category=Shooting+Type",
        ),
        ("diet_baselines", "/api/diet/baselines"),
        ("unscheduled", f"/api/matchups/unscheduled?player_id={PLAYER_ID}&opponent=BOS"),
    ],
)
def test_an_omitted_season_answers_exactly_as_before(world, client, name, path):
    response = client.get(path)

    assert response.status_code == 200
    golden = GOLDEN / f"{name}.json"
    if os.environ.get("GOLDEN_RECORD") == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(_stable(response.data))
    assert _stable(response.data) == golden.read_text()


def test_seasons_keeps_its_listing_and_default(world, client):
    body = client.get("/api/games/seasons").get_json()

    assert (body["seasons"], body["default_season"]) == (["2025-26"], "2025-26")
