"""Application service for Dabble competition and line data."""

from __future__ import annotations

from typing import Any

from app.errors import InvalidInputError
from app.providers.dabble import DabbleAdapter, canonical_stat_filter


class DabbleService:
    """Bound Dabble fixture fan-out and apply line filters."""

    def __init__(
        self,
        provider: DabbleAdapter,
        *,
        max_fixtures_per_request: int,
    ) -> None:
        self.provider = provider
        self.max_fixtures_per_request = max_fixtures_per_request

    def list_competitions(
        self,
        *,
        sport: str | None = None,
        sport_id: str | None = None,
    ) -> dict[str, Any]:
        rows = self.provider.list_competitions(sport=sport, sport_id=sport_id)
        return {"provider": "dabble", "count": len(rows), "competitions": rows}

    def get_lines(
        self,
        *,
        competition: str | None = None,
        competition_id: str | None = None,
        fixture_id: str | None = None,
        player: str | None = None,
        stat: str | None = None,
        fixture_limit: int = 3,
        include_in_play: bool = False,
    ) -> dict[str, Any]:
        selectors = [competition, competition_id, fixture_id]
        if sum(bool(value and str(value).strip()) for value in selectors) != 1:
            raise InvalidInputError(
                "Provide exactly one of competition, competition_id, or fixture_id."
            )
        if not 1 <= fixture_limit <= self.max_fixtures_per_request:
            raise InvalidInputError(
                f"limit must be between 1 and {self.max_fixtures_per_request}."
            )

        lines = self.provider.fetch_lines(
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
        return {"provider": "dabble", "count": len(lines), "lines": lines}


__all__ = ["DabbleService"]
