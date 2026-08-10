"""Small closed NBA event facts shared by normalization and slate reads."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class EventClassification:
    """Canonical event kind beside its independently displayable label."""

    kind: str
    display: str


def _normalized_words(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def is_all_star_kind(value: str) -> bool:
    """Whether a canonical kind names an All-Star event."""

    return "all star" in _normalized_words(value)


def is_preseason_kind(value: str) -> bool:
    """Whether a canonical kind names a preseason event."""

    return _normalized_words(value) in {"preseason", "pre season"}


def is_ordinary_classification(value: str) -> bool:
    """Whether a display classification needs no unusual-event badge."""

    return _normalized_words(value) in {"regular season", "unknown"}


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
    if not is_ordinary_classification(classification):
        return classification
    if sublabel:
        return sublabel
    if classification != "unknown":
        return classification
    return canonical_event_kind(game_id)


def resolve_stored_event_classification(
    game_id: str, stored_classification: str = ""
) -> EventClassification:
    """Resolve one catalog row without letting its badge override a known kind."""

    stored_display = _known_classification(stored_classification)
    kind = canonical_event_kind(game_id, stored_display)
    return EventClassification(
        kind=kind,
        display=kind if stored_display == "unknown" else stored_display,
    )


def _display_sublabel(value: str) -> str:
    sublabel = value.strip()
    normalized = _normalized_words(sublabel)
    if sublabel and not (
        "postpon" in normalized
        or re.match(r"^series (?:tied|leads?)\b", normalized)
        or re.match(r"^.+ (?:leads?|wins)(?: series)? \d+ \d+$", normalized)
        or re.match(r"^game [0-9]+\b", normalized)
    ):
        return sublabel
    return ""


def _known_classification(value: str) -> str:
    classification = value.strip()
    if classification and classification.casefold() != "unknown":
        return classification
    return "unknown"


__all__ = [
    "EventClassification",
    "NBAGameStatus",
    "canonical_event_kind",
    "display_event_classification",
    "is_all_star_kind",
    "is_ordinary_classification",
    "is_preseason_kind",
    "resolve_stored_event_classification",
]
