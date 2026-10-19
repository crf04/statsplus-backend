"""``fallback=previous_game_day`` and ``next_game_day``: one rule for every read of a Slate Date.

The Slate, the slate edges and Target resolution each accept the same optional
parameter (crf04/statsplus#217), and all three resolve the day they answer
through ``read_slate`` so they cannot disagree about which day that is.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from app.errors import InvalidInputError

PREVIOUS_GAME_DAY = "previous_game_day"
NEXT_GAME_DAY = "next_game_day"


def read_slate(
    get_slate: Callable[[str | None], Mapping[str, Any]],
    requested_date: str | None,
    fallback: str | None,
) -> dict[str, Any]:
    """The Slate a read answers, as ``get_slate`` shapes it, plus ``fallback``.

    Without ``fallback`` this is ``get_slate(requested_date)``.  With
    ``previous_game_day`` and no date: when the current ET Slate Date lists no
    game and names a previous game date, the Slate of that date; otherwise the
    current one.  With ``next_game_day`` and no date: when the current ET Slate
    Date lists a game, the current Slate; else the Slate of its
    ``next_game_date``, else of its ``previous_game_date``, else the current
    one.  ``fallback`` on the result names the rule that moved the day
    (``"previous_game_day"`` or ``"next_game_day"``) and is ``None`` for the
    current Slate.  A date together with ``fallback``, or any other
    value, is ``invalid_input``.
    """

    if fallback is None:
        return {**get_slate(requested_date), "fallback": None}
    if fallback not in (PREVIOUS_GAME_DAY, NEXT_GAME_DAY) or requested_date is not None:
        raise InvalidInputError(
            "The fallback parameter must be previous_game_day or next_game_day and cannot be combined with date.",
            public_details={"parameters": ["fallback"]},
        )
    current = get_slate(None)
    if current["games"]:
        return {**current, "fallback": None}
    if fallback == NEXT_GAME_DAY and current.get("next_game_date") is not None:
        return {**get_slate(current["next_game_date"]), "fallback": NEXT_GAME_DAY}
    previous = current.get("previous_game_date")
    if previous is None:
        return {**current, "fallback": None}
    return {**get_slate(previous), "fallback": PREVIOUS_GAME_DAY}


__all__ = ["NEXT_GAME_DAY", "PREVIOUS_GAME_DAY", "read_slate"]
