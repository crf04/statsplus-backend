"""Before any Season Cutover, a Matchup and the Slate edges read as on trunk.

crf04/statsplus-backend#547 adds Serving Season fields to both responses.
Before a cutover nothing trails, so each response must equal trunk's apart
from those additive keys, which carry their resting values.  The golden file
is trunk's (ac194031) output of ``_responses``, captured by running this
module's ``__main__`` against a ``git archive`` of that commit; regenerate it
only from trunk.  Publication ids are random, so they are numbered in order
of appearance.
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
from tests.routes.test_response_provenance import _hand_seed
from tests.services import test_matchup_service as m

GOLDEN = Path(__file__).parent.parent / "fixtures" / "serving_season" / "pre_cutover_responses.json"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class _Slate:
    def get_slate(self, requested_date=None):
        return {
            "slate_date": "2026-01-15",
            "games": [
                {
                    "game_id": m.GAME_ID,
                    "status": {"state": "scheduled", "label": "7:30 pm ET"},
                    "scheduled_at": "2026-01-16T00:30:00+00:00",
                    "away_team": {"team_id": m.LAL, "tricode": "LAL", "name": "Los Angeles Lakers"},
                    "home_team": {"team_id": m.BOS, "tricode": "BOS", "name": "Boston Celtics"},
                }
            ],
        }


def _responses(tmp_path: Path) -> dict:
    """A Matchup and the Slate edges over real game-log and Diet Publications."""

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
    edges = SlateEdgesService(slates=_Slate(), matchups=matchups).get_edges("2026-01-15")
    engine.dispose()
    return _numbered({"matchup": matchup, "edges": edges})


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
    for body in (stripped["matchup"], stripped["edges"]):
        body.pop("trailing", None)
    for section in ("season_defense", "last_15_defense"):
        for key in ("season", "window_games"):
            stripped["matchup"]["experience"]["sections"][section].pop(key, None)
    return stripped


def test_before_a_cutover_matchup_and_slate_edges_equal_trunk_but_for_the_serving_season_keys(tmp_path):
    responses = _responses(tmp_path)

    assert _without_serving_season_keys(responses) == json.loads(GOLDEN.read_text())
    sections = responses["matchup"]["experience"]["sections"]
    assert {
        section: (sections[section]["season"], sections[section]["window_games"])
        for section in ("season_defense", "last_15_defense")
    } == {"season_defense": ("2025-26", None), "last_15_defense": ("2025-26", None)}
    assert responses["matchup"]["trailing"] == []
    assert responses["edges"]["trailing"] == []


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        captured = _without_serving_season_keys(_responses(Path(directory)))
    json.dump(captured, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
