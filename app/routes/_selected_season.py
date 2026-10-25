"""An explicit Evidence season on a read route (crf04/statsplus#330).

``season`` on Matchup, selection, Slate, slate edges, profile and baselines
selects which season's evidence the response reads.  Omitted, the route reads
exactly as before.  A selected season reads through ``season_scoped``; one
that is not offered is ``503 season_unavailable``, never another season's
answer.  An anonymous caller never reads an Unpublished Season: one after the
Published Season is refused before anything is read or cached, as a game-log
preview is.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from flask import request

from ..dependencies import get_dependencies
from ..errors import InvalidInputError, SelectedSeasonUnavailableError
from ..services.database_first_activation import SeasonUnavailable
from ..services.nba_stats_adapter import validate_canonical_season
from ..services.research_season import refuse_after_published, season_scoped
from ..utils.auth import signed_in_user


def season_argument() -> str | None:
    """The request's canonical ``season``, ``None`` when omitted; ``invalid_input`` when malformed."""

    season = request.args.get("season")
    if season is None:
        return None
    try:
        if validate_canonical_season(season) != season:
            raise ValueError(season)
    except ValueError:
        raise InvalidInputError(
            "The season must be a canonical season such as 2025-26.",
            public_details={"parameters": ["season"]},
        ) from None
    return season


def selected(proxy: Any, name: str, season: str | None, *, anonymous: bool = False) -> Any:
    """``proxy`` without a selection, else the ``<name>_service`` scoped to ``season``.

    ``anonymous`` marks a route that also answers callers without a token;
    one of those may not select a season after the Published Season.
    """

    if season is None:
        return proxy
    injected = getattr(get_dependencies(), f"{name}_service")
    if anonymous and signed_in_user() is None:
        refuse_after_published(getattr(injected, "publication_reader", None), season)
    scoped = season_scoped(injected, season)
    return proxy if scoped is injected else scoped


@contextmanager
def season_refusals() -> Iterator[None]:
    """Answer a selection that is not offered with ``503 season_unavailable``."""

    try:
        yield
    except SeasonUnavailable as error:
        raise SelectedSeasonUnavailableError(
            selected_season=error.season, published_season=error.published_season
        ) from error
