"""Canonical classification and cohort vocabulary for matchup parity.

The legacy comparator and the activation gate must agree on which findings
are non-adjudicable.  Keep this module dependency-free so both sides consume
the same contract without importing one another.
"""

from __future__ import annotations


CLASSIFICATION_LEAGUE_INCOMPLETE = "league_incomplete"
CLASSIFICATION_MISSING_LEGACY_TEAM = "missing_legacy_team"
CLASSIFICATION_MISSING_LEDGER_TEAM = "missing_ledger_team"
CLASSIFICATION_EXTRA_TEAM = "extra_team"
CLASSIFICATION_GAME_SET_MISMATCH = "game_set_mismatch"
CLASSIFICATION_INTEGER_COUNT_DIFFERENCE = "integer_count_difference"
CLASSIFICATION_NON_INTEGER_COUNT = "non_integer_count"
CLASSIFICATION_AVAILABILITY_DIFFERENCE = "availability_difference"
CLASSIFICATION_CUTOFF_MISMATCH = "cutoff_mismatch"
CLASSIFICATION_SCOPE_MISMATCH = "scope_mismatch"
CLASSIFICATION_MISSING_SURFACE = "missing_surface"
CLASSIFICATION_MISSING_METRIC = "missing_metric"
CLASSIFICATION_EXTRA_METRIC = "extra_metric"
CLASSIFICATION_DUPLICATE_METRIC = "duplicate_metric"
CLASSIFICATION_L15_GAME_COUNT_MISMATCH = "l15_game_count_mismatch"
CLASSIFICATION_AUTHORITY_MISMATCH = "authority_mismatch"
CLASSIFICATION_INVALID_DENOMINATOR = "invalid_denominator"
CLASSIFICATION_RANKING_DIFFERENCE = "ranking_difference"

CLASSIFICATION_DENOMINATOR_TOLERANCE_EXCEEDED = (
    "denominator_tolerance_exceeded"
)
CLASSIFICATION_DERIVED_RATE_DIFFERENCE = "derived_rate_difference"
CLASSIFICATION_SERVED_RATE_MISMATCH = "served_rate_mismatch"
CLASSIFICATION_SERVED_RANK_MISMATCH = "served_rank_mismatch"

HARD_CLASSIFICATIONS = frozenset({
    CLASSIFICATION_LEAGUE_INCOMPLETE,
    CLASSIFICATION_MISSING_LEGACY_TEAM,
    CLASSIFICATION_MISSING_LEDGER_TEAM,
    CLASSIFICATION_EXTRA_TEAM,
    CLASSIFICATION_GAME_SET_MISMATCH,
    CLASSIFICATION_INTEGER_COUNT_DIFFERENCE,
    CLASSIFICATION_NON_INTEGER_COUNT,
    CLASSIFICATION_AVAILABILITY_DIFFERENCE,
    CLASSIFICATION_CUTOFF_MISMATCH,
    CLASSIFICATION_SCOPE_MISMATCH,
    CLASSIFICATION_MISSING_SURFACE,
    CLASSIFICATION_MISSING_METRIC,
    CLASSIFICATION_EXTRA_METRIC,
    CLASSIFICATION_DUPLICATE_METRIC,
    CLASSIFICATION_L15_GAME_COUNT_MISMATCH,
    CLASSIFICATION_AUTHORITY_MISMATCH,
    CLASSIFICATION_INVALID_DENOMINATOR,
    CLASSIFICATION_RANKING_DIFFERENCE,
    CLASSIFICATION_SERVED_RATE_MISMATCH,
    CLASSIFICATION_SERVED_RANK_MISMATCH,
})

SOFT_CLASSIFICATIONS = frozenset({
    CLASSIFICATION_DENOMINATOR_TOLERANCE_EXCEEDED,
    CLASSIFICATION_DERIVED_RATE_DIFFERENCE,
})

APPROVED_SEMANTIC_RULES = frozenset({
    "parent.matchup.denominator-rate.v1",
})


def semantic_rule_is_approved(rule: object, reason: object) -> bool:
    """Accept only a bounded, parent-owned semantic-rule identifier.

    Provider rounding is diagnostic context, not authority to soften public
    team minutes or derived rates.  Free-form operator prose is deliberately
    excluded from this gate.
    """

    return rule in APPROVED_SEMANTIC_RULES and reason is None

MATCHUP_REQUIRED_STREAMS = frozenset({
    "traditional_opponent_season",
    "traditional_opponent_l15",
    "assist_locations_season",
    "assist_locations_l15",
})


__all__ = [
    name for name in globals()
    if name.startswith("CLASSIFICATION_")
] + [
    "HARD_CLASSIFICATIONS",
    "SOFT_CLASSIFICATIONS",
    "APPROVED_SEMANTIC_RULES",
    "semantic_rule_is_approved",
    "MATCHUP_REQUIRED_STREAMS",
]
