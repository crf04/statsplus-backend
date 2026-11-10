"""Publish NBA's official box score in the Canonical Game Ledger.

PBP Stats and NBA LiveData keep counts NBA corrects in its official box score
(Darryn Peterson's sixth assist on 2026-11-02 is five in
``boxscoretraditionalv3``).  From 2026-27 every accepted game's player rows take
the official counts before normalization, the side's ``team_results`` totals
move with them, and every count that differed is reported
(crf04/statsplus-backend#763).

A changed rebound count is reported but not applied when the game carries the
miss-type rebound split: the split must add up to the rebounds it divides, and
which miss type NBA took a rebound from is unknown.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from app.providers.nba_tracking import BOX_SCORE_STATS
from app.services.canonical_game_ledger import row_observes_rebound_types

BOX_SCORE_RECONCILED_SEASON = "2026-27"

_REBOUND_STATS = frozenset({"REB", "OREB", "DREB"})


@dataclass(frozen=True, slots=True)
class BoxScoreDifference:
    player_id: int
    player_name: str
    stat: str
    ledger_value: int
    box_score_value: int
    applied: bool


def reconcile_to_box_score(
    observation: Mapping[str, Any], box_score: Mapping[int, Mapping[str, int]]
) -> tuple[dict[str, Any], tuple[BoxScoreDifference, ...]]:
    """Return the observation with the official counts and the differences found.

    Players the box score does not list keep their provider counts.
    """

    observation = deepcopy(dict(observation))
    rows = {side: _player_rows(observation, side) for side in ("Home", "Away")}
    applies_rebounds = not any(row_observes_rebound_types(row) for side in rows.values() for row in side)
    differences: list[BoxScoreDifference] = []
    for side, side_rows in rows.items():
        total = _team_total(observation, side)
        for row in side_rows:
            official = box_score.get(int(row["EntityId"]))
            if official is None:
                continue
            recorded = _recorded_counts(row)
            player_differences = [
                BoxScoreDifference(
                    player_id=int(row["EntityId"]),
                    player_name=str(row.get("Name") or ""),
                    stat=stat,
                    ledger_value=recorded[stat],
                    box_score_value=official[stat],
                    applied=applies_rebounds or stat not in _REBOUND_STATS,
                )
                for stat in BOX_SCORE_STATS
                if recorded[stat] != official[stat]
            ]
            differences.extend(player_differences)
            if not any(difference.applied for difference in player_differences):
                continue
            for wire, value in _official_wire(official, row, rebounds=applies_rebounds).items():
                delta = value - int(row.get(wire) or 0)
                if delta:
                    row[wire] = value
                    if total is not None:
                        total[wire] = int(total.get(wire) or 0) + delta
    applied = [difference for difference in differences if difference.applied]
    if applied:
        observation["_ledger_box_score_corrections"] = [
            {
                "player_id": difference.player_id,
                "stat": difference.stat,
                "provider": difference.ledger_value,
                "box_score": difference.box_score_value,
            }
            for difference in applied
        ]
    return observation, tuple(differences)


def _player_rows(observation: Mapping[str, Any], side: str) -> list[dict[str, Any]]:
    stats = observation.get("stats")
    side_value = stats.get(side) if isinstance(stats, Mapping) else None
    rows = side_value.get("FullGame") if isinstance(side_value, Mapping) else None
    return [
        row
        for row in rows or []
        if isinstance(row, dict) and str(row.get("EntityId") or "0") != "0" and row.get("Name") != "Team"
    ]


def _team_total(observation: Mapping[str, Any], side: str) -> dict[str, Any] | None:
    results = observation.get("team_results")
    side_value = results.get(side) if isinstance(results, Mapping) else None
    total = side_value.get("FullGame") if isinstance(side_value, Mapping) else None
    return total if isinstance(total, dict) else None


def _count(row: Mapping[str, Any], key: str) -> int:
    return int(row.get(key) or 0)


def _recorded_counts(row: Mapping[str, Any]) -> dict[str, int]:
    """The counts the ledger would read from a sparse provider row."""

    three_made, three_attempted = _count(row, "FG3M"), _count(row, "FG3A")
    offensive, defensive = _count(row, "OffRebounds"), _count(row, "DefRebounds")
    return {
        "PTS": _count(row, "Points"),
        "REB": offensive + defensive if row.get("Rebounds") is None else _count(row, "Rebounds"),
        "OREB": offensive,
        "DREB": defensive,
        "AST": _count(row, "Assists"),
        "STL": _count(row, "Steals"),
        "BLK": _count(row, "Blocks"),
        "TOV": _count(row, "Turnovers"),
        "FGM": _count(row, "FG2M") + three_made if row.get("FGM") is None else _count(row, "FGM"),
        "FGA": _count(row, "FG2A") + three_attempted if row.get("FGA") is None else _count(row, "FGA"),
        "FG3M": three_made,
        "FG3A": three_attempted,
        "FTM": _count(row, "FtPoints"),
        "FTA": _count(row, "FTA"),
        "PF": _count(row, "Fouls"),
    }


def _official_wire(official: Mapping[str, int], row: Mapping[str, Any], *, rebounds: bool) -> dict[str, int]:
    wire = {
        "Points": official["PTS"],
        "FG2M": official["FGM"] - official["FG3M"],
        "FG2A": official["FGA"] - official["FG3A"],
        "FG3M": official["FG3M"],
        "FG3A": official["FG3A"],
        "FtPoints": official["FTM"],
        "FTA": official["FTA"],
        "Assists": official["AST"],
        "Steals": official["STL"],
        "Blocks": official["BLK"],
        "Turnovers": official["TOV"],
        "Fouls": official["PF"],
    }
    if row.get("FGM") is not None:
        wire["FGM"] = official["FGM"]
    if row.get("FGA") is not None:
        wire["FGA"] = official["FGA"]
    if rebounds:
        wire.update(OffRebounds=official["OREB"], DefRebounds=official["DREB"], Rebounds=official["REB"])
    return wire


__all__ = ["BOX_SCORE_RECONCILED_SEASON", "BoxScoreDifference", "reconcile_to_box_score"]
