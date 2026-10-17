"""The Rebound Types identity, shared by activation and the evidence script.

In every window each team's six miss-type slices must add up to the
player-credited ``OPP_REB`` count the ACTIVE ``traditional_opponent_*``
publication serves for the same team and games, over the same 30 teams.  The
activation transaction and ``scripts/rebound_types_activation_evidence.py``
call this one validator, so the evidence an operator records is exactly the
gate activation applies.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from app.domain.rebound_types import REBOUND_TYPE_WIRE_NAMES

LEAGUE_SIZE = 30
PER48_TOLERANCE = 1e-6


def payload_rows(payload: Any) -> list[Mapping[str, Any]]:
    rows = payload.get("rows") if isinstance(payload, Mapping) else payload
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def check_identity(
    rebound_rows: Sequence[Mapping[str, Any]],
    traditional_rows: Sequence[Mapping[str, Any]],
    *,
    window: str,
) -> tuple[int, list[dict[str, Any]]]:
    """Return ``(teams_checked, failures)`` for one window's two payloads."""

    failures: list[dict[str, Any]] = []
    rebound_by_team = {row.get("team_id"): row for row in rebound_rows}
    traditional_by_team = {row.get("team_id"): row for row in traditional_rows}
    if len(rebound_by_team) != LEAGUE_SIZE or len(traditional_by_team) != LEAGUE_SIZE:
        failures.append(
            {
                "window": window,
                "failure": "league_not_30_teams",
                "rebound_types_teams": len(rebound_by_team),
                "traditional_teams": len(traditional_by_team),
            }
        )
    if set(rebound_by_team) != set(traditional_by_team):
        failures.append(
            {
                "window": window,
                "failure": "team_sets_differ",
                "only_rebound_types": sorted(set(rebound_by_team) - set(traditional_by_team)),
                "only_traditional": sorted(set(traditional_by_team) - set(rebound_by_team)),
            }
        )
    checked = 0
    for team_id in sorted(set(rebound_by_team) & set(traditional_by_team)):
        rebound = rebound_by_team[team_id]
        traditional = traditional_by_team[team_id]
        checked += 1
        if sorted(rebound.get("game_ids") or ()) != sorted(traditional.get("game_ids") or ()):
            failures.append(
                {
                    "window": window,
                    "team_id": team_id,
                    "failure": "game_set_mismatch",
                    "rebound_types_games": len(rebound.get("game_ids") or ()),
                    "traditional_games": len(traditional.get("game_ids") or ()),
                }
            )
            continue
        try:
            slice_sum = sum(rebound["counts"][name] for name in REBOUND_TYPE_WIRE_NAMES)
            opp_reb = traditional["counts"]["rebounds"]
            per48_sum = sum(rebound["per48"][name] for name in REBOUND_TYPE_WIRE_NAMES)
            traditional_per48 = traditional["per48"]["rebounds"]
        except (KeyError, TypeError):
            failures.append(
                {
                    "window": window,
                    "team_id": team_id,
                    "failure": "row_malformed",
                }
            )
            continue
        if slice_sum != opp_reb:
            failures.append(
                {
                    "window": window,
                    "team_id": team_id,
                    "failure": "slices_do_not_add_up_to_opp_reb",
                    "slice_sum": slice_sum,
                    "opp_reb": opp_reb,
                }
            )
        if not math.isclose(per48_sum, traditional_per48, rel_tol=PER48_TOLERANCE, abs_tol=PER48_TOLERANCE):
            failures.append(
                {
                    "window": window,
                    "team_id": team_id,
                    "failure": "per48_does_not_add_up_to_opp_reb",
                    "slice_sum_per48": per48_sum,
                    "opp_reb_per48": traditional_per48,
                }
            )
    return checked, failures


def rebound_identity_failures(
    rebound_payload: Any, traditional_payload: Any, *, window: str
) -> list[dict[str, Any]]:
    """Failures of the identity between two decoded-JSON publication payloads."""

    return check_identity(payload_rows(rebound_payload), payload_rows(traditional_payload), window=window)[1]


__all__ = ["check_identity", "payload_rows", "rebound_identity_failures"]
