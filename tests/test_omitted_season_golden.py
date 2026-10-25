"""With no season chosen, every response is byte for byte what it was (statsplus#330 AC1).

The goldens were recorded from master before Selected Season reads existed,
over a world that publishes 2025-26 and holds 2026-27 candidates of the game
logs and team defense, so a default read that touched a held candidate would
change them.  Publication IDs are random per run and are replaced, in order of
first appearance, before comparing.  ``GOLDEN_RECORD=1`` rewrites the files.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tests.support.selected_season import GAME_ID, PLAYER_ID, PLAYER_NAME, SelectedWorld

GOLDEN = Path(__file__).parent / "fixtures" / "omitted_season"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture
def world(tmp_path, dependencies):
    built = SelectedWorld(tmp_path)
    built.hold_opening_week()
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
