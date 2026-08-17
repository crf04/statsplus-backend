"""Compatibility import for the public provider-normalization seam."""

# Re-export the stable normalizer names for older rehearsal harnesses.
# ruff: noqa: F401

from .normalizers import (
    PLAY_TYPES,
    SHOT_TYPES,
    SHOT_ZONES,
    normalize_grouped_shot_response,
    normalize_opponent_grouped_shot_response,
    normalize_opponent_synergy_response,
    normalize_opponent_zone_response,
    normalize_roster_response,
    normalize_schedule_response,
    normalize_shot_type_response,
    normalize_shot_zone_response,
    normalize_synergy_response,
    normalize_zone_response,
)
