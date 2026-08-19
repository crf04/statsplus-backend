"""Independent NBA provider capture for player Season per-36 parity."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

import pandas as pd

from app.services.ledger_lineage import LedgerLineage
from app.services.ledger_parity import PER36_RAW_FIELDS, PER36_RATE_FIELDS
from app.services.matchup_authority import UniqueMatchupAuthority
from app.services.nba_stats_adapter import (
    player_per36_request_descriptor,
    player_totals_request_descriptor,
)


_PROVIDER_FIELDS = {
    "points": "PTS", "rebounds": "REB", "assists": "AST",
    "field_goals_made": "FGM", "field_goals_attempted": "FGA",
    "three_pointers_made": "FG3M", "three_pointers_attempted": "FG3A",
    "free_throws_made": "FTM", "free_throws_attempted": "FTA",
    "turnovers": "TOV", "steals": "STL", "blocks": "BLK",
    "personal_fouls": "PF",
}


class Per36ProviderEvidenceCollector:
    """Collect one checksum-verifiable envelope through an injected adapter."""

    def __init__(self, adapter) -> None:
        self._adapter = adapter

    def collect(
        self, *, season: str, authority: UniqueMatchupAuthority,
    ) -> Mapping[str, object]:
        authority.require_completed_regular_season()
        if season != authority.season:
            raise ValueError("per36 provider season does not match authority")
        rates = self._adapter.fetch_player_per36_stats(season=season)
        totals = self._adapter.fetch_player_totals_stats(season=season)
        requests = {
            "per36": self._adapter.transport_request_descriptor(
                "player_per36_stats"
            ),
            "totals": self._adapter.transport_request_descriptor(
                "player_totals_stats"
            ),
        }
        if requests != {
            "per36": player_per36_request_descriptor(season=season),
            "totals": player_totals_request_descriptor(season=season),
        }:
            raise ValueError("per36 provider transport request is invalid")
        rows = self._rows(rates, totals)
        request_checksum = hashlib.sha256(json.dumps(
            requests, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        game_ids = sorted(authority.expected_game_ids)
        identity = {
            "season": season,
            "window": "season",
            "cutoff": authority.cutoff.isoformat(),
            "provider_start_date": authority.provider_start_date,
            "provider_end_date": authority.provider_end_date,
            "transport_requests": requests,
            "request_checksum": request_checksum,
            "game_ids": game_ids,
            "returned_row_count": len(rows),
            "returned_game_count": len(game_ids),
            "event_catalog_mapping_trace": {
                game_id: game_id for game_id in game_ids
            },
        }
        return {
            "rows": rows,
            "provider_window_identity": identity,
            "request_checksum": request_checksum,
            "game_set_checksum": LedgerLineage.for_game_ids(game_ids),
        }

    @staticmethod
    def _rows(rates: pd.DataFrame, totals: pd.DataFrame) -> list[dict[str, object]]:
        required = {
            "PLAYER_ID", "TEAM_ID", "GP", "MIN", *_PROVIDER_FIELDS.values(),
        }
        if not required.issubset(totals.columns) or not {
            "PLAYER_ID", *_PROVIDER_FIELDS.values(),
        }.issubset(rates.columns):
            raise ValueError("per36 provider response is incomplete")
        if totals["PLAYER_ID"].duplicated().any() or rates["PLAYER_ID"].duplicated().any():
            raise ValueError("per36 provider response repeats a player")
        rates_by_id = rates.set_index("PLAYER_ID")
        result = []
        for source in totals.sort_values("PLAYER_ID").to_dict("records"):
            player_id = int(source["PLAYER_ID"])
            if player_id not in rates_by_id.index:
                raise ValueError("per36 provider response identity mismatch")
            rate = rates_by_id.loc[player_id]
            row: dict[str, object] = {
                "player_id": player_id,
                "minutes": float(source["MIN"]),
                "game_count": int(source["GP"]),
                "team_ids_at_game": [int(source["TEAM_ID"])],
            }
            for field in PER36_RAW_FIELDS:
                value = source[_PROVIDER_FIELDS[field]]
                if isinstance(value, bool) or int(value) != value or int(value) < 0:
                    raise ValueError("per36 provider raw count is invalid")
                row[field] = int(value)
                row[f"{field}_per36"] = float(rate[_PROVIDER_FIELDS[field]])
            if set(field for field in row if field.endswith("_per36")) != set(
                PER36_RATE_FIELDS
            ):
                raise ValueError("per36 provider rate response is incomplete")
            result.append(row)
        if not result:
            raise ValueError("per36 provider response is empty")
        return result


__all__ = ["Per36ProviderEvidenceCollector"]
