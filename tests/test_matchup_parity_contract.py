"""The parent-approved semantic rule and its bounds."""

from app.domain.matchup_parity_contract import (
    APPROVED_SEMANTIC_RULES,
    CLASSIFICATION_OFFICIAL_SCOREKEEPER_CORRECTION,
    HARD_CLASSIFICATIONS,
    SEMANTIC_RULE_OFFICIAL_SCOREKEEPER_CORRECTION,
    SOFT_CLASSIFICATIONS,
    count_difference_within_correction_bound,
    minutes_difference_within_correction_bound,
    semantic_rule_is_approved,
)

REASON = "Official box-score corrections the PBP feed never received."


def test_only_the_parent_approved_rule_with_a_recorded_reason_is_approved():
    assert APPROVED_SEMANTIC_RULES == {SEMANTIC_RULE_OFFICIAL_SCOREKEEPER_CORRECTION}
    assert semantic_rule_is_approved(SEMANTIC_RULE_OFFICIAL_SCOREKEEPER_CORRECTION, REASON)
    assert not semantic_rule_is_approved(SEMANTIC_RULE_OFFICIAL_SCOREKEEPER_CORRECTION, "too short")
    assert not semantic_rule_is_approved(SEMANTIC_RULE_OFFICIAL_SCOREKEEPER_CORRECTION, None)
    assert not semantic_rule_is_approved("provider_rounding", REASON)
    assert not semantic_rule_is_approved(None, REASON)


def test_correction_classification_is_soft_and_bounds_are_exact():
    assert CLASSIFICATION_OFFICIAL_SCOREKEEPER_CORRECTION in SOFT_CLASSIFICATIONS
    assert CLASSIFICATION_OFFICIAL_SCOREKEEPER_CORRECTION not in HARD_CLASSIFICATIONS
    assert count_difference_within_correction_bound(649, 650)
    assert count_difference_within_correction_bound(5, 4)
    assert not count_difference_within_correction_bound(5, 7)
    assert not count_difference_within_correction_bound("x", 1)
    assert minutes_difference_within_correction_bound(1988.98, 1990.98)
    assert not minutes_difference_within_correction_bound(1988.98, 1991.0)
