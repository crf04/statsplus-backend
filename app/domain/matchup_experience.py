"""The one rule that decides a Matchup's experience, and its wire vocabulary.

Both the Matchup response and the Matchup selection response declare the same
experience, so the eligibility rule and the strings that carry it live here
rather than being restated at each seam.
"""

from collections.abc import Mapping, Sized
from typing import Any

from app.domain.nba_events import is_completed_non_postponed_event

HISTORICAL_MODE = "historical"
CURRENT_MODE = "current"
GAME_LOG_SOURCE = "game_logs"
PLAYER_POOL_SOURCE = "player_pool"


def is_historical_matchup(event: Mapping[str, Any], pool_players: Sized) -> bool:
    """Report whether this game opens in the Historical Matchup experience.

    A completed, non-postponed game whose governed Player Pool names nobody has
    no archived closing projections: a closing set with memberships always
    contributes players. Callers restrict the season kind before asking; the
    Matchup event read already refuses every non-Regular-Season game.
    """

    return is_completed_non_postponed_event(event) and not len(pool_players)


def experience_mode(historical: bool) -> str:
    return HISTORICAL_MODE if historical else CURRENT_MODE


def player_source(historical: bool) -> str:
    return GAME_LOG_SOURCE if historical else PLAYER_POOL_SOURCE


__all__ = [
    "CURRENT_MODE",
    "GAME_LOG_SOURCE",
    "HISTORICAL_MODE",
    "PLAYER_POOL_SOURCE",
    "experience_mode",
    "is_historical_matchup",
    "player_source",
]
