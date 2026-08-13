"""Semantic parity evidence between PBP-derived and legacy provider facts."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.services.canonical_game_ledger import CanonicalGame, PlayerGameFact, validate_canonical_season


@dataclass(frozen=True, slots=True)
class SemanticDifference:
    identity: str
    field: str
    pbp_value: object
    legacy_value: object
    classification: str


@dataclass(frozen=True, slots=True)
class LedgerParityReport:
    season: str
    game_count: int
    compared_count: int
    differences: tuple[SemanticDifference, ...]
    adjudication_required: bool

    @property
    def exact(self) -> bool:
        return not self.differences


def compare_ledger_to_legacy(
    games: Iterable[CanonicalGame],
    legacy_rows: Iterable[Mapping[str, object]],
    *,
    season: str,
    tolerance: float = 1e-9,
) -> LedgerParityReport:
    """Compare only shared count semantics; report known provider differences.

    Legacy rows can carry percentages/rates and provider aggregates.  Those
    values are never compared as if they were ledger count primitives.  A
    difference is evidence for adjudication, not a reason to rewrite the PBP
    observation or to claim byte-level parity.
    """

    canonical_season = validate_canonical_season(season)
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be finite and non-negative")
    games_tuple = tuple(game for game in games if game.season == canonical_season)
    ledger: dict[tuple[str, int], PlayerGameFact] = {
        (game.game_id, player.player_id): player
        for game in games_tuple
        for player in game.player_facts
    }
    differences: list[SemanticDifference] = []
    compared = 0
    fields = {
        "points": ("points", "PTS"),
        "rebounds": ("rebounds", "REB"),
        "assists": ("assists", "AST"),
        "turnovers": ("turnovers", "TOV"),
        "steals": ("steals", "STL"),
        "blocks": ("blocks", "BLK"),
        "personal_fouls": ("personal_fouls", "PF"),
        "minutes": ("minutes", "MIN"),
    }
    for legacy in legacy_rows:
        game_identity = str(legacy.get("game_id") or legacy.get("GAME_ID") or "")
        raw_player_id = legacy.get("player_id") or legacy.get("PLAYER_ID")
        try:
            player_identity = int(raw_player_id or 0)
        except (TypeError, ValueError):
            differences.append(
                SemanticDifference(
                    f"{game_identity}:invalid",
                    "identity",
                    "present",
                    raw_player_id,
                    "invalid_legacy_identity",
                )
            )
            continue
        identity = (game_identity, player_identity)
        current = ledger.get(identity)
        if current is None:
            differences.append(SemanticDifference(f"{identity[0]}:{identity[1]}", "identity", None, "present", "missing_ledger_identity"))
            continue
        compared += 1
        for field_name, (ledger_name, legacy_name) in fields.items():
            legacy_value = legacy.get(legacy_name, legacy.get(ledger_name))
            if legacy_value is None:
                continue
            current_value = getattr(current, ledger_name)
            try:
                equal = math.isclose(float(current_value), float(legacy_value), rel_tol=tolerance, abs_tol=tolerance)
            except (TypeError, ValueError):
                equal = current_value == legacy_value
            if not equal:
                differences.append(SemanticDifference(f"{identity[0]}:{identity[1]}", field_name, current_value, legacy_value, "semantic_difference"))
    return LedgerParityReport(
        season=canonical_season,
        game_count=len(games_tuple),
        compared_count=compared,
        differences=tuple(differences),
        adjudication_required=bool(differences),
    )


def generate_semantic_difference_report(
    games: Iterable[CanonicalGame],
    legacy_rows: Iterable[Mapping[str, object]],
    *,
    season: str,
) -> LedgerParityReport:
    """Named report seam used by backfill verification and offline fixtures."""

    return compare_ledger_to_legacy(games, legacy_rows, season=season)


__all__ = [
    "LedgerParityReport",
    "SemanticDifference",
    "compare_ledger_to_legacy",
    "generate_semantic_difference_report",
]
