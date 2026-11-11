"""Publish NBA's official box score in the Canonical Game Ledger.

PBP Stats and NBA LiveData keep counts NBA corrects in its official box score
(Darryn Peterson's sixth assist on 2026-11-02 is five in
``boxscoretraditionalv3``).  From 2026-27 every accepted game's player rows take
the official counts before normalization, the side's ``team_results`` totals
move with them, and every count that differed is reported
(crf04/statsplus-backend#763).

Nothing says which location or miss type a correction came from, so a
corrected assist count leaves that player's assist locations unknown, and a
corrected offensive or defensive rebound count in a game that carries the
miss-type split leaves that player's split unknown.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from app.domain.rebound_types import REBOUND_TYPE_WIRE_NAMES
from app.providers.nba_tracking import BOX_SCORE_STATS
from app.services.canonical_game_ledger import LEDGER_REBOUND_TYPES_UNKNOWN_KEY, row_observes_rebound_types
from app.utils.telemetry import ProviderResponseError

BOX_SCORE_RECONCILED_SEASON = "2026-27"
#: One request per game, never retried.  ``boxscoretraditionalv3`` answered in
#: 0.13-0.20 s on 2026-11-10, so 10 s is ~50x a healthy answer.
BOX_SCORE_TIMEOUT_SECONDS = 10.0
#: Seconds one run may spend on box-score checks before it accepts the rest of
#: its games unchecked.  A healthy run of ~25 rechecks spends ~5 s; with
#: stats.nba.com down, 120 s caps the cost at 12 timeouts plus the one in
#: flight (production runs one worker): about 2 minutes in a cron that runs
#: every 2 hours.
BOX_SCORE_BUDGET_SECONDS = 120.0

_ASSIST_LOCATION_WIRES = (
    "TwoPtAssists",
    "ThreePtAssists",
    "Arc3Assists",
    "Corner3Assists",
    "AtRimAssists",
    "ShortMidRangeAssists",
    "LongMidRangeAssists",
)


@dataclass(frozen=True, slots=True)
class BoxScoreDifference:
    player_id: int
    player_name: str
    stat: str
    ledger_value: int
    box_score_value: int


def reconcile_to_box_score(
    observation: Mapping[str, Any], box_score: Mapping[int, Mapping[str, int]]
) -> tuple[dict[str, Any], tuple[BoxScoreDifference, ...]]:
    """Return the observation with the official counts and the differences found.

    A box score that leaves out a player of the game, or credits anything to a
    player the game does not have, is no reference for it.
    """

    observation = deepcopy(dict(observation))
    rows = [row for side in ("Home", "Away") for row in _player_rows(observation, side)]
    player_ids = {int(row["EntityId"]) for row in rows}
    if not player_ids <= box_score.keys() or any(
        any(counts.values()) for player_id, counts in box_score.items() if player_id not in player_ids
    ):
        raise ProviderResponseError("boxscoretraditionalv3 does not cover the game's players")
    splits_rebounds = any(row_observes_rebound_types(row) for row in rows)
    differences: list[BoxScoreDifference] = []
    for side in ("Home", "Away"):
        total = _team_total(observation, side)
        for row in _player_rows(observation, side):
            official = box_score[int(row["EntityId"])]
            recorded = _recorded_counts(row)
            changed = [stat for stat in BOX_SCORE_STATS if recorded[stat] != official[stat]]
            differences.extend(
                BoxScoreDifference(
                    player_id=int(row["EntityId"]),
                    player_name=str(row.get("Name") or ""),
                    stat=stat,
                    ledger_value=recorded[stat],
                    box_score_value=official[stat],
                )
                for stat in changed
            )
            if not changed:
                continue
            for wire, value in _official_wire(official, row).items():
                delta = value - int(row.get(wire) or 0)
                if delta:
                    row[wire] = value
                    if total is not None:
                        total[wire] = int(total.get(wire) or 0) + delta
            if "AST" in changed:
                for wire in _ASSIST_LOCATION_WIRES:
                    if wire in row:
                        row[wire] = None
            if splits_rebounds and {"OREB", "DREB"} & set(changed):
                for wire in REBOUND_TYPE_WIRE_NAMES:
                    row.pop(wire, None)
                row[LEDGER_REBOUND_TYPES_UNKNOWN_KEY] = True
    if differences:
        observation["_ledger_box_score_corrections"] = [
            {
                "player_id": difference.player_id,
                "stat": difference.stat,
                "provider": difference.ledger_value,
                "box_score": difference.box_score_value,
            }
            for difference in differences
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


def _official_wire(official: Mapping[str, int], row: Mapping[str, Any]) -> dict[str, int]:
    wire = {
        "Points": official["PTS"],
        "FG2M": official["FGM"] - official["FG3M"],
        "FG2A": official["FGA"] - official["FG3A"],
        "FG3M": official["FG3M"],
        "FG3A": official["FG3A"],
        "FtPoints": official["FTM"],
        "FTA": official["FTA"],
        "OffRebounds": official["OREB"],
        "DefRebounds": official["DREB"],
        "Assists": official["AST"],
        "Steals": official["STL"],
        "Blocks": official["BLK"],
        "Turnovers": official["TOV"],
        "Fouls": official["PF"],
    }
    for composite, stat in (("FGM", "FGM"), ("FGA", "FGA"), ("Rebounds", "REB")):
        if row.get(composite) is not None:
            wire[composite] = official[stat]
    return wire


__all__ = [
    "BOX_SCORE_BUDGET_SECONDS",
    "BOX_SCORE_RECONCILED_SEASON",
    "BOX_SCORE_TIMEOUT_SECONDS",
    "BoxScoreDifference",
    "reconcile_to_box_score",
]
