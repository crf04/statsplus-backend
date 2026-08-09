"""Recorded CommonAllPlayers payload tests; no upstream access is allowed."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.nba_stats_adapter import parse_recorded_player_roster
from app.utils.telemetry import clear_recorded_provider_events, get_recorded_provider_events


FIXTURE = Path(__file__).parents[1] / "fixtures" / "nba_stats_player_roster.json"


def test_recorded_roster_payload_uses_production_parser_and_normalization():
    clear_recorded_provider_events()

    frame = parse_recorded_player_roster(
        json.loads(FIXTURE.read_text(encoding="utf-8")),
        season="2024-25",
    )

    assert frame[["player_id", "display_name", "roster_status"]].to_dict(
        orient="records"
    ) == [
        {
            "player_id": 23,
            "display_name": "LeBron James",
            "roster_status": "active",
        },
        {
            "player_id": 999,
            "display_name": "Retired Player",
            "roster_status": "historical",
        },
    ]
    events = get_recorded_provider_events()
    assert [event["operation"] for event in events] == ["player_roster_recorded"]
