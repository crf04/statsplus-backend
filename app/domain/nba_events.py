"""Small closed NBA event facts shared by normalization and slate reads."""

from __future__ import annotations

from enum import IntEnum


class NBAGameStatus(IntEnum):
    """Status codes published by the NBA schedule feed."""

    FINAL = 3


_GAME_TYPE_BY_ID_PREFIX = {
    "001": "Preseason",
    "002": "Regular Season",
    "003": "All-Star",
    "004": "Playoffs",
}


def event_classification(game_id: str, provider_classification: str = "") -> str:
    """Return provider display evidence, or infer it from a real game ID."""

    classification = provider_classification.strip()
    if classification and classification.casefold() != "unknown":
        return classification
    return event_kind(game_id)


def event_kind(game_id: str, provider_classification: str = "") -> str:
    """Return canonical event kind, with a real game ID as authority."""

    if len(game_id) == 10 and game_id.isdigit():
        game_type = _GAME_TYPE_BY_ID_PREFIX.get(game_id[:3])
        if game_type is not None:
            return game_type
    classification = provider_classification.strip()
    if classification and classification.casefold() != "unknown":
        return classification
    return "unknown"


__all__ = ["NBAGameStatus", "event_classification", "event_kind"]
