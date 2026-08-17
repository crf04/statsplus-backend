"""Closed NBA event-kind and display-classification vocabulary."""

import pytest

from app.domain.nba_events import (
    NBAGameStatus,
    canonical_event_kind,
    is_all_star_kind,
    is_final_event,
    l15_game_ids_by_team,
    is_ordinary_classification,
    is_postponed_event,
    is_preseason_kind,
    player_game_log_season_type,
    resolve_stored_event_classification,
)


def test_l15_game_selection_uses_the_latest_fifteen_chronological_events():
    events = tuple(
        {
            "nba_game_id": f"game-{index:02d}",
            "home_team_id": 1,
            "away_team_id": index + 2,
        }
        for index in range(16)
    )

    selected = l15_game_ids_by_team(events)

    assert selected[1] == frozenset(
        f"game-{index:02d}" for index in range(1, 16)
    )
    assert selected[2] == frozenset({"game-00"})


@pytest.mark.parametrize(
    "event",
    [
        {"status_code": NBAGameStatus.FINAL, "status_text": "Scheduled"},
        {"status_code": 1, "status_text": "Final/OT"},
    ],
)
def test_final_event_accepts_governed_code_and_terminal_text(event):
    assert is_final_event(event)


@pytest.mark.parametrize(
    "status", ["Finished", "Completed", "Closed", "Game Over", "Game Finished"]
)
def test_final_event_accepts_exact_provider_terminal_aliases(status):
    assert is_final_event({"status": status})


def test_final_event_rejects_nonterminal_schedule_status():
    assert not is_final_event({"status_code": 1, "status_text": "7:00 pm ET"})


@pytest.mark.parametrize(
    ("event", "season_type"),
    [
        ({"nba_game_id": "0022500001", "classification": "Playoffs"}, "Regular Season"),
        ({"nba_game_id": "0042500001", "classification": "Regular Season"}, "Playoffs"),
        ({"nba_game_id": "provider-id", "classification": " PLAY_OFFS "}, "Playoffs"),
        ({"nba_game_id": "0032500001", "classification": "Playoffs"}, None),
    ],
)
def test_player_game_log_phase_uses_game_id_authority_then_normalized_fallback(
    event, season_type
):
    assert player_game_log_season_type(event) == season_type


def test_canonical_event_kind_preserves_fallback_label_spelling():
    assert canonical_event_kind("", " regular season ") == "regular season"


def test_play_in_game_id_overrides_a_misleading_regular_season_label():
    event = {
        "nba_game_id": "0052500001",
        "classification": "Regular Season",
    }

    assert canonical_event_kind("0052500001", "Regular Season") == "Play-In"
    assert player_game_log_season_type(event) is None
    resolved = resolve_stored_event_classification(
        "0052500001", "Regular Season"
    )
    assert resolved.kind == "Play-In"
    assert resolved.display == "Play-In"


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
    resolved = resolve_stored_event_classification("0062500001", "All-Star Showcase")

    assert resolved.kind == "All-Star Showcase"
    assert resolved.display == "All-Star Showcase"


@pytest.mark.parametrize(
    "event",
    [
        {"is_postponed": True},
        {"postponed_status": "Postponed"},
        {"postponement_evidence": {"reason": "weather"}},
        {"postponement_evidence": ["provider flag"]},
        {"status": "Postponed"},
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


def test_postponement_truth_does_not_accept_arbitrary_truthy_flag():
    assert not is_postponed_event({"is_postponed": "false"})
