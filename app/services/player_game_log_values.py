"""Governed statistic-component values available from durable player logs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.services.statistic_catalog import CanonicalStatistic


def _two_pointer_attempts(record: Any) -> float:
    return float(record.field_goals_attempted - record.three_pointers_attempted)


_DERIVED_COMPONENT_VALUES = {
    "two_pointers_attempted": _two_pointer_attempts,
}


def validate_player_game_log_components(
    components: Iterable[str], *, stored_components: Iterable[str]
) -> None:
    """Fail startup when a governed statistic cannot be derived from stored logs."""

    supported = {*stored_components, *_DERIVED_COMPONENT_VALUES}
    unsupported = sorted(set(components) - supported)
    if unsupported:
        raise ValueError(
            "player game logs cannot derive governed statistic components: "
            f"{unsupported}"
        )


def player_game_log_component_value(record: Any, component: str) -> float:
    derived = _DERIVED_COMPONENT_VALUES.get(component)
    if derived is not None:
        return derived(record)
    return float(getattr(record, component))


def player_game_log_market_values(
    record: Any,
    statistics: Iterable[CanonicalStatistic],
) -> dict[str, float]:
    return {
        statistic.market_category: sum(
            player_game_log_component_value(record, component)
            for component in statistic.components
        )
        for statistic in statistics
        if statistic.market_category is not None
    }


def selected_player_game_log_market_values(
    record: Any,
    markets: Iterable[str],
    statistics: Mapping[str, CanonicalStatistic],
) -> dict[str, float]:
    return {
        market: sum(
            player_game_log_component_value(record, component)
            for component in statistics[market].components
        )
        for market in markets
    }


def player_game_log_focal_line(
    record: Any,
    markets: Iterable[str],
    statistics: Mapping[str, CanonicalStatistic],
    *,
    precision: int = 6,
) -> dict[str, Any]:
    """Return one game's display-only stat line for a historical participant."""

    separator = "vs." if record.is_home else "@"
    values = selected_player_game_log_market_values(record, markets, statistics)
    return {
        "game_id": str(record.game_id),
        "game_date": record.game_date.isoformat(),
        "matchup": (
            f"{record.team_tricode} {separator} {record.opponent_team_tricode}"
        ),
        "minutes": round(float(record.minutes), precision),
        "stats": {
            market: round(float(value), precision)
            for market, value in values.items()
        },
    }


__all__ = [
    "player_game_log_component_value",
    "player_game_log_focal_line",
    "player_game_log_market_values",
    "selected_player_game_log_market_values",
    "validate_player_game_log_components",
]
