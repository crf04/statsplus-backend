"""Before any Season Cutover, a Matchup, the Slate and its edges read as on trunk.

crf04/statsplus-backend#547 adds Serving Season fields to a Matchup and the
Slate edges, and #552 adds ``trailing`` to the Slate and ``as_of`` to a game
Matchup.  Before a cutover nothing trails, so each response must equal
trunk's apart from those additive keys, which carry their resting values.
#654 later adds ``last_five_games`` and ``season_averages`` to each Matchup
player row; they are stripped too and pinned on their own.  #407 makes PTS and
FGA read the Traditional ``OPP_PTS`` and ``OPP_FGA`` rows, which the seeded
legacy facts lack, so those windows name ``team_defense:traditional``; that
entry is stripped too and pinned on its own.  #437 drops the impossible
``Less Than 10 ft`` 3PA/3PM Defense Sheet rows, so they are removed from the
golden before comparing and their absence is pinned on its own.
The golden file is trunk's (622a98b4) output of ``_responses``, captured by
running this module's ``__main__`` against a ``git archive`` of that commit;
regenerate it only from trunk.  Publication ids are random, so they are
numbered in order of appearance.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services.collection_control import PublicationService
from app.services.player_diet import PlayerDietRepository
from app.services.slate_edges import SlateEdgesService
from app.services.slate_service import SlateService
from tests.routes.test_response_provenance import _hand_seed
from tests.services import test_matchup_service as m
from tests.support.slate_catalog import NoNeighbourEvents

GOLDEN = Path(__file__).parent.parent / "fixtures" / "serving_season" / "pre_cutover_responses.json"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class _Schedule(NoNeighbourEvents):
    """The Event Catalog holding the Matchup's one game."""

    def count_events(self, season):
        return 1

    def get_freshness(self, season, *, now):
        return {"last_success_at": m.RETRIEVED_AT.isoformat(), "event_count": 1}

    def get_events_between(self, season, starts_at, ends_at):
        return [m._event()]


def _slate_service(matchups) -> SlateService:
    slate = SlateService(
        _Schedule(),
        settings=matchups.settings,
        clock=lambda: m.NOW,
        schedule_max_age=timedelta(hours=24),
    )
    # Assigned rather than passed, so trunk, which the golden is captured
    # from and has no such parameter, runs this module unchanged.
    slate.publication_reader = matchups.publication_reader
    slate.matchup_snapshot = lambda reader: _matchup_snapshot(matchups, reader)
    return slate


def _matchup_snapshot(matchups, reader):
    from app.services.matchup_snapshot import capture_matchup_snapshot

    return capture_matchup_snapshot(matchups.settings, reader)


def _responses(tmp_path: Path) -> dict:
    """A Matchup, the Slate and its edges over real game-log and Diet Publications."""

    engine, matchups, _logs = m._published_matchup_service(
        tmp_path,
        [m._log_row(game_id=m.GAME_ID, game_date="2026-01-14", points=31, minutes=34.0)],
    )
    _hand_seed(
        engine,
        "synergy_play_types",
        {
            "rows": [
                {
                    "player_id": 2544,
                    "slice_key": "Transition",
                    "share": 0.32,
                    "volume": 480.0,
                    "games_played": 60,
                    "volume_unit": "possessions",
                    "provider": "nba_synergy",
                }
            ]
        },
        publication_id="pub-play-types",
        created_at=m.NOW - timedelta(minutes=10),
        cutoff=datetime(2026, 1, 15, 8, tzinfo=UTC),
    )
    PublicationService(engine, clock=lambda: m.NOW).register_stream(
        "player_per36",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
        freshness_rule="daily_recheck",
    )
    matchups.player_diets = PlayerDietRepository(engine, publication_reader=matchups.publication_reader)
    matchup = matchups.get_matchup(game_id=m.GAME_ID)
    slates = _slate_service(matchups)
    slate = slates.get_slate("2026-01-15")
    edges = SlateEdgesService(slates=slates, matchups=matchups).get_edges("2026-01-15")
    engine.dispose()
    return _numbered({"matchup": matchup, "slate": slate, "edges": edges})


def _numbered(responses: dict) -> dict:
    encoded = json.dumps(responses, sort_keys=True, default=str)
    ids: dict[str, str] = {}
    encoded = _UUID.sub(
        lambda match: ids.setdefault(match.group(0), f"<publication-{len(ids) + 1}>"), encoded
    )
    numbered = json.loads(encoded)
    # An unregistered Diet stream is stamped with the wall clock (trunk too).
    for surface in numbered["matchup"]["freshness"]["player_diets"]["surfaces"].values():
        if surface["unavailable_reason"] == "stream_not_registered":
            surface["retrieved_at"] = "<request time>"
    return numbered


def _without_serving_season_keys(responses: dict) -> dict:
    stripped = json.loads(json.dumps(responses))
    for body in (stripped["matchup"], stripped["slate"], stripped["edges"]):
        body.pop("trailing", None)
    stripped["matchup"].pop("as_of", None)
    # Added after the trunk golden was recorded (crf04/statsplus#466).
    stripped["matchup"].pop("game_context", None)
    for player in stripped["matchup"]["players"]:
        player.pop("last_five_games", None)
        player.pop("season_averages", None)
        for window in (window for windows in player["scores"].values() for window in windows.values()):
            window["missing_inputs"] = [
                name for name in window["missing_inputs"] if name != "team_defense:traditional"
            ]
    window_states = [
        stripped["matchup"]["experience"]["sections"][section]
        for section in ("season_defense", "last_15_defense")
    ]
    for windows in stripped["matchup"]["league"]["surface_availability"].values():
        window_states.extend(windows.values())
    for state in window_states:
        for key in ("season", "window_games"):
            state.pop(key, None)
    return stripped


_IMPOSSIBLE_ROWS = frozenset({"Less Than 10 ft:FG3A", "Less Than 10 ft:FG3M"})


def _shot_type_sheets(matchup: dict) -> list[list[dict]]:
    return [body["defense_sheet"]["shot_types"] for body in (matchup["league"], *matchup["teams"])]


def _golden_without_impossible_rows() -> dict:
    golden = json.loads(GOLDEN.read_text())
    for rows in _shot_type_sheets(golden["matchup"]):
        rows[:] = [row for row in rows if row["key"] not in _IMPOSSIBLE_ROWS]
    return golden


def test_before_a_cutover_matchup_slate_and_edges_equal_trunk_but_for_the_serving_season_keys(tmp_path):
    responses = _responses(tmp_path)

    assert _without_serving_season_keys(responses) == _golden_without_impossible_rows()
    assert all(
        row["key"] not in _IMPOSSIBLE_ROWS for rows in _shot_type_sheets(responses["matchup"]) for row in rows
    )
    sections = responses["matchup"]["experience"]["sections"]
    assert {
        section: (sections[section]["season"], sections[section]["window_games"])
        for section in ("season_defense", "last_15_defense")
    } == {"season_defense": ("2025-26", None), "last_15_defense": ("2025-26", None)}
    assert {
        base: {window: (state["season"], state["window_games"]) for window, state in windows.items()}
        for base, windows in responses["matchup"]["league"]["surface_availability"].items()
    } == {
        "assist_locations": {"season": ("2025-26", None), "last_15": ("2025-26", None)},
        "play_types": {"season": ("2025-26", None), "last_15": (None, None)},
        "rebound_types": {"season": ("2025-26", None), "last_15": ("2025-26", None)},
        "shot_types": {"season": ("2025-26", None), "last_15": ("2025-26", None)},
        "shot_zones": {"season": ("2025-26", None), "last_15": ("2025-26", None)},
        "traditional": {"season": ("2025-26", None), "last_15": ("2025-26", None)},
    }
    assert responses["matchup"]["as_of"] == {
        "season": "2025-26",
        "season_defense": "2026-01-15",
        "last_15_defense": "2026-01-15",
        "player_diets": "2026-01-15",
    }
    (player,) = responses["matchup"]["players"]
    # This service has no Statistic Catalog, so the game line names no stats.
    assert player["last_five_games"] == [
        {
            "game_id": "0022500584",
            "game_date": "2026-01-14",
            "matchup": "LAL @ BOS",
            "minutes": 34.0,
            "stats": {},
        }
    ]
    assert player["season_averages"] == {"PTS": 31.0, "FGA": 18.0}
    assert {
        market: [
            window_name
            for window_name, window in windows.items()
            if "team_defense:traditional" in window["missing_inputs"]
        ]
        for market, windows in player["scores"].items()
    } == {"PTS": ["last_15", "season"], "FGA": ["last_15", "season"]}
    assert responses["matchup"]["trailing"] == []
    assert responses["slate"]["trailing"] == []
    assert responses["edges"]["trailing"] == []


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        captured = _without_serving_season_keys(_responses(Path(directory)))
    json.dump(captured, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
