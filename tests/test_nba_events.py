"""Closed NBA event-kind and display-classification vocabulary."""

import pytest

from app.domain.nba_events import (
    is_all_star_kind,
    is_ordinary_classification,
    is_preseason_kind,
    resolve_stored_event_classification,
)


@pytest.mark.parametrize(
    "value", ["All-Star", "all star", "ALL_STAR", "All-Star Celebrity Game"]
)
def test_all_star_kind_predicate_normalizes_closed_vocabulary(value):
    assert is_all_star_kind(value)


@pytest.mark.parametrize("value", ["Preseason", "pre-season", "PRE_SEASON"])
def test_preseason_kind_predicate_normalizes_closed_vocabulary(value):
    assert is_preseason_kind(value)


@pytest.mark.parametrize("value", ["Regular Season", "regular-season", "unknown"])
def test_ordinary_classification_predicate_normalizes_closed_vocabulary(value):
    assert is_ordinary_classification(value)


@pytest.mark.parametrize("value", ["Playoffs", "NBA Cup", "Rising Stars"])
def test_event_predicates_do_not_expand_to_unrelated_labels(value):
    assert not is_all_star_kind(value)
    assert not is_preseason_kind(value)
    assert not is_ordinary_classification(value)


def test_stored_classification_cannot_override_a_known_game_id_kind():
    resolved = resolve_stored_event_classification("0022500001", "All-Star Showcase")

    assert resolved.kind == "Regular Season"
    assert resolved.display == "All-Star Showcase"


def test_unknown_game_id_prefix_uses_stored_classification_as_kind_fallback():
    resolved = resolve_stored_event_classification("0052500001", "All-Star Showcase")

    assert resolved.kind == "All-Star Showcase"
    assert resolved.display == "All-Star Showcase"
