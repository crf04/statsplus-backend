"""The one active Regular Season the daily jobs follow."""

from __future__ import annotations

from sqlalchemy import select

from app.models.collection_control import ActiveSeason


class AmbiguousActiveSeason(Exception):
    """More than one Regular Season is active; no job may guess between them."""

    def __init__(self, seasons: list[str]) -> None:
        super().__init__(f"multiple active seasons: {', '.join(seasons)}")
        self.seasons = seasons


def active_season(engine) -> str | None:
    """Season of the single ``active_seasons`` row with ``status='active'``.

    Returns ``None`` when no season is active and raises
    ``AmbiguousActiveSeason`` when several are.
    """
    with engine.connect() as connection:
        seasons = list(
            connection.execute(
                select(ActiveSeason.season)
                .where(
                    ActiveSeason.status == "active",
                    ActiveSeason.phase == "Regular Season",
                )
                .order_by(ActiveSeason.season)
            ).scalars()
        )
    if len(seasons) > 1:
        raise AmbiguousActiveSeason(seasons)
    return seasons[0] if seasons else None
