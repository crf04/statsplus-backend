#!/usr/bin/env python3
"""Generate the deterministic representative fixture used by the Matchups benchmark."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


UTC = timezone.utc
SEASON = "2025-26"


def _team(team_id: int) -> tuple[str, str]:
    return team_id, f"T{team_id:02d}"


def build_fixture(*, source_commit: str = "working-tree") -> dict:
    observed_at = datetime.now(UTC).replace(microsecond=0)
    teams = [_team(team_id) for team_id in range(1, 31)]
    events = []
    for index in range(15):
        home_id, home_code = teams[index * 2]
        away_id, away_code = teams[index * 2 + 1]
        scheduled = observed_at - timedelta(hours=2 + index)
        events.append(
            {
                "nba_game_id": f"bench-game-{index:02d}",
                "season": SEASON,
                "home_team_id": home_id,
                "home_team_name": f"Team {home_id}",
                "home_team_tricode": home_code,
                "away_team_id": away_id,
                "away_team_name": f"Team {away_id}",
                "away_team_tricode": away_code,
                "scheduled_at": scheduled.isoformat(),
                "status_text": "Final",
                "classification": "Regular Season",
                "first_seen_at": scheduled.isoformat(),
                "last_seen_at": observed_at.isoformat(),
            }
        )

    logs = []
    for player_number in range(1, 101):
        team_id, team_code = teams[(player_number - 1) % len(teams)]
        opponent_id = team_id + 1 if team_id % 2 else team_id - 1
        opponent_code = teams[opponent_id - 1][1]
        for offset in range(5):
            game_index = (player_number + offset) % len(events)
            event = events[game_index]
            logs.append(
                {
                    "season": SEASON,
                    "season_type": "Regular Season",
                    "player_id": player_number,
                    "game_id": event["nba_game_id"],
                    "player_name": f"Player {player_number}",
                    "game_date": event["scheduled_at"][:10],
                    "team_id": team_id,
                    "team_tricode": team_code,
                    "opponent_team_id": opponent_id,
                    "opponent_team_tricode": opponent_code,
                    "is_home": team_id % 2 == 1,
                    "minutes": 24.0 + (player_number % 6),
                    "points": 10 + (player_number % 15),
                    "rebounds": 4 + (player_number % 5),
                    "assists": 2 + (player_number % 4),
                    "field_goals_made": 4 + (player_number % 5),
                    "field_goals_attempted": 10 + (player_number % 8),
                    "three_pointers_made": 1 + (player_number % 3),
                    "three_pointers_attempted": 3 + (player_number % 5),
                    "free_throws_made": 1 + (player_number % 4),
                    "free_throws_attempted": 2 + (player_number % 5),
                    "offensive_rebounds": 1 + (player_number % 2),
                    "defensive_rebounds": 3 + (player_number % 3),
                    "turnovers": player_number % 3,
                    "steals": player_number % 2,
                    "blocks": player_number % 2,
                    "personal_fouls": 1 + (player_number % 4),
                }
            )

    players = [
        {
            "canonical_player_id": player_number,
            "name": f"Player {player_number}",
            "team_id": teams[(player_number - 1) % len(teams)][0],
            "market_categories": ["PTS", "REB", "AST", "PRA"],
            "provenance": {"benchmark_fixture": ["PTS", "REB", "AST", "PRA"]},
        }
        for player_number in range(1, 101)
    ]
    pool_payload = {
        "players": players,
        "team_counts": {str(team_id): 1 for team_id, _ in teams},
        "freshness": {
            "status": "fresh",
            "retrieved_at": observed_at.isoformat(),
            "providers": {
                "board": {"status": "fresh", "retrieved_at": observed_at.isoformat()},
                "mapping": {"status": "fresh", "retrieved_at": observed_at.isoformat()},
            },
        },
    }

    diet_rows = []
    diet_specs = (
        ("play_types", "Isolation", "possessions", "nba_synergy"),
        ("shot_types", "catch_and_shoot", "field_goal_attempts", "nba_stats"),
        ("shot_zones", "Restricted Area", "field_goal_attempts", "nba_stats"),
        ("assist_locations", "Arc3Assists", "assists", "pbp_stats"),
    )
    for base, slice_key, unit, provider in diet_specs:
        for player_number in range(1, 101):
            diet_rows.append(
                {
                    "season": SEASON,
                    "player_id": player_number,
                    "base": base,
                    "slice_key": slice_key,
                    "share": 1.0,
                    "volume": 10.0 + (player_number % 5),
                    "games_played": 5,
                    "volume_unit": unit,
                    "provider": provider,
                    "retrieved_at": observed_at.isoformat(),
                }
            )

    team_rows = []
    for team_id, team_code in teams:
        for stat_key in ("OPP_TOV", "OPP_STL", "OPP_BLK"):
            team_rows.append(
                {
                    "season": SEASON,
                    "as_of_date": observed_at.date().isoformat(),
                    "window_kind": "season",
                    "window_games": 0,
                    "team_id": team_id,
                    "base": "traditional",
                    "slice_key": stat_key,
                    "stat_key": stat_key,
                    "raw_value": float(20 + team_id),
                    "denominator_value": 240.0,
                    "denominator_unit": "minutes",
                    "provider": "benchmark_fixture",
                    "window_end_date": observed_at.date().isoformat(),
                    "retrieved_at": observed_at.isoformat(),
                }
            )

    log_payload = {"rows": logs}
    publications = {
        "streams": [
            {
                "stream_key": "player_game_logs",
                "provider": "ledger",
                "owner": "benchmark",
                "required_observations": [],
                "supported_windows": ["season"],
                "enabled": True,
            }
        ],
        "versions": [
            {
                "publication_id": "benchmark-publication-player-logs",
                "stream_key": "player_game_logs",
                "season": SEASON,
                "cutoff": observed_at.isoformat(),
                "version": 1,
                "status": "active",
                "payload": log_payload,
                "reason": "representative benchmark fixture",
                "fence": 1,
            }
        ],
        "pointers": [
            {
                "stream_key": "player_game_logs",
                "active_publication_id": "benchmark-publication-player-logs",
                "previous_publication_id": None,
                "fence": 1,
            }
        ],
    }
    return {
        "fixture_version": 1,
        "fixture_kind": "representative_fixture",
        "production_claim": False,
        "season": SEASON,
        "game_id": events[0]["nba_game_id"],
        "captured_at": observed_at.isoformat(),
        "source_commit": source_commit,
        "seeded_fixture": {
            "event_catalog": events,
            "player_pool": [
                {
                    "season": SEASON,
                    "game_ids": [event["nba_game_id"] for event in events],
                    "payload": pool_payload,
                    "retrieved_at": observed_at.isoformat(),
                    "updated_at": observed_at.isoformat(),
                    "refresh_version": 1,
                    "refresh_outcome": "success",
                }
            ],
            "player_game_logs": logs,
            "player_diets": diet_rows,
            "team_matchups": {"facts": team_rows},
            "publications": publications,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    source_commit = args.source_commit
    if not source_commit:
        source_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build_fixture(source_commit=source_commit), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
