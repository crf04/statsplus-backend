"""Small closed NBA event facts shared by normalization and slate reads."""

from __future__ import annotations

from enum import IntEnum
import re


class NBAGameStatus(IntEnum):
    """Status codes published by the NBA schedule feed."""

    FINAL = 3


_GAME_TYPE_BY_ID_PREFIX = {
    "001": "Preseason",
    "002": "Regular Season",
    "003": "All-Star",
    "004": "Playoffs",
}


def canonical_event_kind(game_id: str, provider_classification: str = "") -> str:
    """Return canonical event kind, with a real game ID as authority."""

    if len(game_id) == 10 and game_id.isdigit():
        game_type = _GAME_TYPE_BY_ID_PREFIX.get(game_id[:3])
        if game_type is not None:
            return game_type
    return _known_classification(provider_classification)


def display_event_classification(
    game_id: str,
    provider_classification: str = "",
    provider_sublabel: str = "",
) -> str:
    """Return meaningful display evidence, then fall back to canonical kind."""

    classification = _known_classification(provider_classification)
    sublabel = _display_sublabel(provider_sublabel)
    if classification.casefold() not in {"regular season", "unknown"}:
        return classification
    if sublabel:
        return sublabel
    if classification != "unknown":
        return classification
    return canonical_event_kind(game_id)


def _display_sublabel(value: str) -> str:
    sublabel = value.strip()
    normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", sublabel.casefold()).split())
    if sublabel and not (
        "postpon" in normalized
        or "series" in normalized
        or re.fullmatch(r"game [0-9]+", normalized)
    ):
        return sublabel
    return ""


def _known_classification(value: str) -> str:
    classification = value.strip()
    if classification and classification.casefold() != "unknown":
        return classification
    return "unknown"


__all__ = [
    "NBAGameStatus",
    "canonical_event_kind",
    "display_event_classification",
]
