"""Provider-neutral contracts for daily-fantasy line data."""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable


_STAT_ORDER = {
    "points": 0,
    "rebounds": 1,
    "assists": 2,
    "three-pointers-made": 3,
    "steals": 4,
    "blocks": 5,
    "turnovers": 6,
}
STAT_ALIASES = {
    "pts": "points",
    "reb": "rebounds",
    "ast": "assists",
    "pra": "points+rebounds+assists",
    "pa": "points+assists",
    "pr": "points+rebounds",
    "ra": "rebounds+assists",
    "stl": "steals",
    "blk": "blocks",
    "tov": "turnovers",
}


def canonical_stat_components(stats: Sequence[str]) -> list[str]:
    """Normalize provider components into one deterministic display order."""

    normalized = [stat.strip().casefold().replace("_", "-") for stat in stats]

    def sort_key(stat: str) -> tuple[str, int, str]:
        prefix = ""
        base = stat
        for period in ("first-half-", "second-half-", "first-quarter-"):
            if stat.startswith(period):
                prefix = period
                base = stat.removeprefix(period)
                break
        return prefix, _STAT_ORDER.get(base, len(_STAT_ORDER)), base

    return sorted(normalized, key=sort_key)


def canonical_stat_filter(value: str) -> str:
    """Accept common StatsPlus abbreviations and unordered stat combinations."""

    normalized = value.strip().casefold().replace(" ", "-").replace("_", "-")
    alias = STAT_ALIASES.get(normalized)
    if alias:
        return alias
    return "+".join(canonical_stat_components(normalized.split("+")))


@runtime_checkable
class DFSLineProvider(Protocol):
    """Read-only interface implemented by a DFS line source."""

    def list_competitions(
        self, *, sport: str | None = None, sport_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return normalized active competitions."""

    def fetch_lines(
        self,
        *,
        competition: str | None = None,
        competition_id: str | None = None,
        fixture_id: str | None = None,
        fixture_limit: int = 3,
        include_in_play: bool = False,
    ) -> list[dict[str, Any]]:
        """Return normalized line options for the selected fixtures."""


__all__ = [
    "DFSLineProvider",
    "STAT_ALIASES",
    "canonical_stat_components",
    "canonical_stat_filter",
]
