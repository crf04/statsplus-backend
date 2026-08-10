"""Closed NBA event-kind and display-classification vocabulary."""

import pytest

from app.domain.nba_events import (
    NBAGameStatus,
    is_all_star_kind,
    is_final_event,
    is_ordinary_classification,
    is_postponed_event,
    is_preseason_kind,
    is_regular_season_event,
    resolve_stored_event_classification,
)


@pytest.mark.parametrize(
    "event",
    [
        {"status_code": NBAGameStatus.FINAL, "status_text": "Scheduled"},
        {"status_code": 1, "status_text": "Final/OT"},
    ],
)
def test_final_event_accepts_governed_code_and_terminal_text(event):
    assert is_final_event(event)


def test_final_event_rejects_nonterminal_schedule_status():
    assert not is_final_event({"status_code": 1, "status_text": "7:00 pm ET"})


def test_regular_season_event_uses_canonical_game_id_authority():
    assert is_regular_season_event(
        {"nba_game_id": "0022500001", "classification": "Preseason"}
    )


@pytest.mark.parametrize("game_id", ["0012500001", "0032500001", "0042500001"])
def test_regular_season_event_excludes_other_governed_phases(game_id):
    assert not is_regular_season_event(
        {"nba_game_id": game_id, "classification": "Regular Season"}
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


@pytest.mark.parametrize(
    "event",
    [
        {"is_postponed": True},
        {"postponed_status": "Postponed"},
        {"postponement_evidence": {"reason": "weather"}},
        {"postponement_evidence": ["provider flag"]},
    ],
)
def test_postponement_truth_accepts_normalized_status_or_structured_evidence(event):
    assert is_postponed_event(event)


@pytest.mark.parametrize(
    "event",
    [{}, {"is_postponed": False}, {"postponed_status": "", "postponement_evidence": {}}],
)
def test_postponement_truth_rejects_absent_or_empty_evidence(event):
    assert not is_postponed_event(event)
