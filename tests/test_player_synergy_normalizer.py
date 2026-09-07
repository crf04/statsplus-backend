"""Source retention only: these synthetic rows do not claim repaired players."""
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from app.collector.contracts import ProviderContractError
from app.collector.normalizers import normalize_synergy_response
from app.collector.provider import ResidentialScopeExecutor, ScopeWork

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def _row(**changes):
    return dict(PLAYER_ID=1, TEAM_ID=1610612737, GP=12, POSS=21,
                POSS_PCT=.125, FGA=17, PLAY_TYPE="Isolation", TYPE_GROUPING="Offensive", **changes)


def _normalize(rows, **scope):
    return normalize_synergy_response(rows, season="2025-26", cutoff=NOW,
        scope={"window": "season", "phase": "Regular Season", "play_type": "Isolation", **scope})


def test_synergy_retains_distinct_team_sources_and_possessions_not_fga():
    second = _row()
    second.update(TEAM_ID=1610612738, POSS=9, POSS_PCT=.09, GP=5)
    result = _normalize([second, _row()])
    assert result.complete
    assert result.payload["records"] == [
        {"player_id": 1, "team_id": 1610612737, "games_played": 12,
         "possessions": 21, "possession_share": .125, "category": "Isolation"},
        {"player_id": 1, "team_id": 1610612738, "games_played": 5,
         "possessions": 9, "possession_share": .09, "category": "Isolation"},
    ]


def test_synergy_duplicate_same_player_team_category_is_rejected():
    with pytest.raises(ProviderContractError, match="duplicate_identity"):
        _normalize([_row(), _row()])


@pytest.mark.parametrize("field,value", [
    ("TEAM_ID", None), ("PLAYER_ID", 1.5), ("GP", 0), ("GP", 1.5),
    ("POSS", None), ("POSS", -1), ("POSS", float("nan")), ("POSS_PCT", None),
    ("POSS_PCT", 1.1), ("POSS_PCT", float("inf")), ("TYPE_GROUPING", "Defensive"),
])
def test_synergy_invalid_source_evidence_is_rejected(field, value):
    row = _row()
    row[field] = value
    with pytest.raises(ProviderContractError):
        _normalize([row])


def test_synergy_preserves_zero_and_sparse_rounded_shares_without_normalizing():
    first, second = _row(), _row()
    first.update(POSS=0, POSS_PCT=0)
    second.update(PLAYER_ID=2, POSS_PCT=.005)
    assert [r["possession_share"] for r in _normalize([first, second]).payload["records"]] == [0, .005]


def test_player_synergy_executor_requests_offensive_player_totals():
    provider = Mock()
    provider.fetch_synergy_play_types.return_value = [_row()]
    executor = ResidentialScopeExecutor(provider, clock=lambda: NOW)
    work = ScopeWork(scope="synergy_play_types", observation_type="synergy_play_types",
                     instruction_id="test", manifest_id="manifest", season="2025-26", cutoff=NOW.isoformat(),
                     parameters={"window": "season", "play_type": "Isolation"})
    result = executor.execute_scope(work, collector_id="test", environment="testing", retrieved_at=NOW)
    provider.fetch_synergy_play_types.assert_called_once_with(
        "Isolation", player_or_team_abbreviation="P", type_grouping="Offensive",
        per_mode_simple="Totals", season="2025-26", season_type="Regular Season",
    )
    assert result[0].payload["records"][0]["possessions"] == 21


@pytest.mark.parametrize("scope", [
    {"window": "l15"}, {"phase": "Playoffs"}, {"subject": "opponent"},
    {"type_grouping": "Defensive"}, {"value_mode": "per_game"},
    {"play_type": "Transition"},
])
def test_player_synergy_rejects_mislabelled_scope(scope):
    with pytest.raises(ProviderContractError):
        _normalize([_row()], **scope)


def test_player_synergy_scope_records_actual_request_semantics():
    result = _normalize([_row()])
    assert result.scope == {
        "window": "season", "phase": "Regular Season", "play_type": "Isolation",
        "subject": "player", "type_grouping": "Offensive", "value_mode": "totals",
    }
    assert result.payload["coverage"]["scope"] == result.scope
