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
    """Return provider evidence, or infer the official type from a real game ID."""

    classification = provider_classification.strip()
    if classification and classification.casefold() != "unknown":
        return classification
    if len(game_id) == 10:
        return _GAME_TYPE_BY_ID_PREFIX.get(game_id[:3], "unknown")
    return "unknown"


__all__ = ["NBAGameStatus", "event_classification"]
