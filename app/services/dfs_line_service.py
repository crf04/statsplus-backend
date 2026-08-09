"""Provider-neutral orchestration for daily-fantasy lines."""

from __future__ import annotations

from typing import Any, Mapping

from app.errors import InvalidInputError
from app.providers.dfs import DFSLineProvider, canonical_stat_filter


class DFSLineService:
    """Select a DFS provider, bound fan-out, and apply normalized filters."""

    def __init__(
        self,
        providers: Mapping[str, DFSLineProvider],
        *,
        max_fixtures_per_request: int,
    ) -> None:
        self.providers = {name.casefold(): provider for name, provider in providers.items()}
        self.max_fixtures_per_request = max_fixtures_per_request

    def list_competitions(
        self,
        *,
        provider: str = "dabble",
        sport: str | None = None,
        sport_id: str | None = None,
    ) -> dict[str, Any]:
        provider_name, adapter = self._provider(provider)
        rows = adapter.list_competitions(sport=sport, sport_id=sport_id)
        return {"provider": provider_name, "count": len(rows), "competitions": rows}

    def get_lines(
        self,
        *,
        provider: str = "dabble",
        competition: str | None = None,
        competition_id: str | None = None,
        fixture_id: str | None = None,
        player: str | None = None,
        stat: str | None = None,
        fixture_limit: int = 3,
        include_in_play: bool = False,
    ) -> dict[str, Any]:
        provider_name, adapter = self._provider(provider)
        selectors = [competition, competition_id, fixture_id]
        if sum(bool(value and str(value).strip()) for value in selectors) != 1:
            raise InvalidInputError(
                "Provide exactly one of competition, competition_id, or fixture_id."
            )
        if not 1 <= fixture_limit <= self.max_fixtures_per_request:
            raise InvalidInputError(
                f"limit must be between 1 and {self.max_fixtures_per_request}."
            )

        lines = adapter.fetch_lines(
            competition=competition,
            competition_id=competition_id,
            fixture_id=fixture_id,
            fixture_limit=fixture_limit,
            include_in_play=include_in_play,
        )
        if player:
            player_filter = player.strip().casefold()
            lines = [
                line
                for line in lines
                if player_filter in str(line.get("player_name", "")).casefold()
            ]
        if stat:
            stat_filter = canonical_stat_filter(stat)
            lines = [
                line
                for line in lines
                if stat_filter == canonical_stat_filter(str(line.get("stat", "")))
            ]
        return {"provider": provider_name, "count": len(lines), "lines": lines}

    def _provider(self, provider: str) -> tuple[str, DFSLineProvider]:
        name = str(provider or "").strip().casefold()
        adapter = self.providers.get(name)
        if adapter is None:
            supported = ", ".join(sorted(self.providers))
            raise InvalidInputError(
                f"Unsupported DFS provider {provider!r}. Supported providers: {supported}."
            )
        return name, adapter


__all__ = ["DFSLineService"]
