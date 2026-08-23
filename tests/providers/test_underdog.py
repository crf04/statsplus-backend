"""Offline tests for the Underdog DFS snapshot adapter."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from app.errors import ProviderUnavailableError
from app.utils import telemetry
from app.providers.dfs import (
    AppearanceEvidence,
    CoverageCode,
    MalformedProviderResponseError,
    MarketVariant,
    NBAMarketQuery,
    RetrievalContext,
    ScoringPeriod,
    SelectionDirection,
    SnapshotStatus,
)
from app.providers.underdog import UnderdogAdapter


FIXTURES = Path(__file__).parents[1] / "fixtures" / "underdog"


@pytest.fixture(autouse=True)
def _clean_telemetry():
    telemetry.clear_recorded_provider_events()
    yield
    telemetry.clear_recorded_provider_events()


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self.payload


class FakeSession:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, tuple[float, float]]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, *, timeout):
        self.calls.append((url, timeout))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class ClockAdvancingSession(FakeSession):
    def __init__(self, response: object, clock: list[datetime], after: datetime) -> None:
        super().__init__(response)
        self.clock = clock
        self.after = after

    def get(self, url: str, *, timeout):
        response = super().get(url, timeout=timeout)
        self.clock[0] = self.after
        return response


def _payload() -> dict[str, object]:
    return json.loads((FIXTURES / "over_under_lines.valid.json").read_text())


def _context(request_id: str | None = None) -> RetrievalContext:
    return RetrievalContext(deadline="2030-01-01T00:00:00Z", request_id=request_id)


def _query() -> NBAMarketQuery:
    return NBAMarketQuery()


def test_get_snapshot_joins_underdog_resources_and_preserves_modifiers() -> None:
    session = FakeSession(FakeResponse(_payload()))

    snapshot = UnderdogAdapter(session=session).get_snapshot(
        _query(), _context("underdog-request")
    )

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert len(snapshot.markets) == 1
    market = snapshot.markets[0]
    assert market.market_id == "line-1"
    assert market.athlete is not None
    assert market.athlete.provider_id == "player-1"
    assert market.athlete.name == "Nikola Jokic"
    assert isinstance(market.appearance, AppearanceEvidence)
    assert market.appearance.provider_id == "appearance-1"
    assert market.appearance.appearance_type == "Player"
    assert market.appearance.label == "Nikola Jokic"
    assert market.team is not None
    assert market.team.provider_id == "team-den"
    assert market.team.abbreviation == "DEN"
    assert market.event is not None
    assert market.event.provider_id == "101"
    assert market.event.label == "DEN @ OKC"
    assert market.event.status_label == "scheduled"
    assert market.event.starts_at == datetime(2026, 8, 10, 6, tzinfo=timezone.utc)
    assert market.starts_at == market.event.starts_at
    assert market.updated_at == datetime(2026, 8, 10, 0, tzinfo=timezone.utc)
    assert market.statistic is not None
    assert market.statistic.label == "Rebounds"
    # Underdog sends no scoring-period label on its standard markets, so the
    # absent label resolves to a full-game prop while the raw evidence (no
    # label) is retained as None.
    assert market.scoring_period is ScoringPeriod.FULL_GAME
    assert market.scoring_period_label is None
    assert market.threshold is not None
    assert str(market.threshold.value) == "12.500"
    assert market.variant_label == "balanced"
    assert len(market.selections) == 2
    higher, lower = market.selections
    assert higher.selection_id == "selection-higher"
    assert higher.direction is SelectionDirection.HIGHER
    assert higher.direction_label == "higher"
    assert higher.american_price == -112
    assert str(higher.decimal_price) == "1.900"
    assert higher.modifiers[0].value == higher.modifiers[0].value.__class__("1.000")
    assert higher.modifiers[0].kind == "payout_multiplier"
    assert higher.modifiers[0].scope == "selection"
    assert higher.modifiers[0].label is None
    assert lower.direction is SelectionDirection.LOWER
    assert snapshot.coverage.fanout_complete is True
    assert session.calls
    events = telemetry.get_recorded_provider_events()
    assert len(events) == 1
    assert events[0]["provider"] == telemetry.PROVIDER_UNDERDOG
    assert events[0]["operation"] == "get_snapshot"
    assert events[0]["request_id"] == "underdog-request"


def test_missing_underdog_variant_label_remains_missing() -> None:
    payload = _payload()
    rows = payload["over_under_lines"]
    assert isinstance(rows, list)
    rows[0].pop("line_type", None)

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    market = snapshot.markets[0]
    assert market.variant is MarketVariant.UNKNOWN
    assert market.variant_label is None


def test_underdog_canonical_scoring_period_resolves_with_label_evidence() -> None:
    payload = _payload()
    rows = payload["over_under_lines"]
    assert isinstance(rows, list)
    rows[0]["over_under"]["appearance_stat"]["scoring_period"] = "full_game"

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    market = snapshot.markets[0]
    assert market.scoring_period is ScoringPeriod.FULL_GAME
    assert market.scoring_period_label == "full_game"


def test_underdog_period_scoped_label_resolves_to_its_specific_period() -> None:
    payload = _payload()
    rows = payload["over_under_lines"]
    assert isinstance(rows, list)
    rows[0]["over_under"]["appearance_stat"]["scoring_period"] = "first_half"

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    market = snapshot.markets[0]
    # A genuinely period-scoped label keeps its specific period -- it is never
    # promoted to full game -- and stays non-targetable.
    assert market.scoring_period is ScoringPeriod.FIRST_HALF
    assert market.scoring_period_label == "first_half"


def test_underdog_unrecognized_present_label_stays_unknown_not_full_game() -> None:
    payload = _payload()
    rows = payload["over_under_lines"]
    assert isinstance(rows, list)
    rows[0]["over_under"]["appearance_stat"]["scoring_period"] = "overtime"

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    market = snapshot.markets[0]
    # An unrecognized present label is NOT an absent label: it stays UNKNOWN
    # (non-targetable) and never defaults to full game, and the raw label is
    # retained verbatim as evidence.
    assert market.scoring_period is ScoringPeriod.UNKNOWN
    assert market.scoring_period_label == "overtime"


def test_underdog_present_non_textual_period_stays_unknown_not_full_game() -> None:
    payload = _payload()
    rows = payload["over_under_lines"]
    assert isinstance(rows, list)
    # A present but non-textual value is present evidence, not absence.
    rows[0]["over_under"]["appearance_stat"]["scoring_period"] = 2

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    assert snapshot.markets[0].scoring_period is ScoringPeriod.UNKNOWN


def test_underdog_whitespace_only_period_stays_unknown_not_full_game() -> None:
    payload = _payload()
    rows = payload["over_under_lines"]
    assert isinstance(rows, list)
    rows[0]["over_under"]["appearance_stat"]["scoring_period"] = "   "

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    assert snapshot.markets[0].scoring_period is ScoringPeriod.UNKNOWN


def test_underdog_null_scoring_period_falls_through_to_present_period_label() -> None:
    payload = _payload()
    rows = payload["over_under_lines"]
    assert isinstance(rows, list)
    # A null scoring_period is not absence when a legacy period label is present.
    rows[0]["over_under"]["appearance_stat"]["scoring_period"] = None
    rows[0]["over_under"]["appearance_stat"]["period"] = "first_half"

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    market = snapshot.markets[0]
    assert market.scoring_period is ScoringPeriod.FIRST_HALF
    assert market.scoring_period_label == "first_half"


def test_underdog_excludes_team_non_nba_and_closed_markets_with_coverage() -> None:
    payload = _payload()
    players = payload["players"]
    appearances = payload["appearances"]
    rows = payload["over_under_lines"]
    assert isinstance(players, list)
    assert isinstance(appearances, list)
    assert isinstance(rows, list)
    players.append(
        {"id": "player-nfl", "first_name": "Other", "last_name": "Sport", "sport_id": "NFL"}
    )
    appearances.extend(
        [
            {"id": "team-appearance", "type": "Team", "player_id": None, "match_id": 101},
            {"id": "nfl-appearance", "type": "Player", "player_id": "player-nfl", "match_id": 101},
        ]
    )
    rows.extend(
        [
            copy.deepcopy(rows[0]) | {
                "id": "line-team",
                "over_under": {"appearance_stat": {"appearance_id": "team-appearance", "display_stat": "To Win"}},
            },
            copy.deepcopy(rows[0]) | {
                "id": "line-nfl",
                "over_under": {"appearance_stat": {"appearance_id": "nfl-appearance", "display_stat": "Yards"}},
            },
            copy.deepcopy(rows[0]) | {"id": "line-closed", "status": "settled"},
        ]
    )

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    assert [market.market_id for market in snapshot.markets] == ["line-1"]
    assert snapshot.coverage.skipped_count == 3
    assert "non_player_market" in snapshot.coverage.skipped_reasons
    assert "non_nba_market" in snapshot.coverage.skipped_reasons
    assert "ineligible_status" in snapshot.coverage.skipped_reasons


def test_underdog_excludes_live_closed_settled_match_future_and_entry_markets() -> None:
    payload = _payload()
    appearances = payload["appearances"]
    rows = payload["over_under_lines"]
    assert isinstance(appearances, list)
    assert isinstance(rows, list)

    invalid_appearances = [
        ("match", "Match"),
        ("future", "Player"),
        ("entry-placement", "Entry-placement"),
    ]
    for suffix, appearance_type in invalid_appearances:
        appearance = copy.deepcopy(appearances[0]) | {"id": f"appearance-{suffix}"}
        if suffix == "future":
            appearance["match_type"] = "Future"
            appearance["match_id"] = None
        else:
            appearance["type"] = appearance_type
        appearances.append(appearance)
        rows.append(
            copy.deepcopy(rows[0])
            | {
                "id": f"line-{suffix}",
                "over_under": {
                    "appearance_stat": {
                        "appearance_id": f"appearance-{suffix}",
                    }
                },
            }
        )

    for status in ("live", "closed", "settled"):
        rows.append(copy.deepcopy(rows[0]) | {"id": f"line-{status}", "status": status})

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    assert [market.market_id for market in snapshot.markets] == ["line-1"]
    assert snapshot.coverage.skipped_count == 6
    assert "non_player_market" in snapshot.coverage.skipped_reasons
    assert "non_game_market" in snapshot.coverage.skipped_reasons
    assert "ineligible_status" in snapshot.coverage.skipped_reasons


def test_missing_underdog_match_is_partial_without_fabricating_an_event() -> None:
    payload = _payload()
    payload["appearances"].append(
        copy.deepcopy(payload["appearances"][0])
        | {"id": "appearance-missing-match", "match_id": "missing-match"}
    )
    payload["over_under_lines"].append(
        copy.deepcopy(payload["over_under_lines"][0])
        | {
            "id": "line-missing-match",
            "over_under": {
                "appearance_stat": {
                    "appearance_id": "appearance-missing-match",
                    "display_stat": "Rebounds",
                }
            },
        }
    )

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    assert [market.market_id for market in snapshot.markets] == ["line-1"]
    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.coverage.fanout_complete is False
    assert snapshot.coverage.skipped_count == 1
    assert "missing_match_relationship" in snapshot.coverage.skipped_reasons


def test_missing_underdog_match_type_is_partial_with_coverage() -> None:
    payload = _payload()
    payload["appearances"].append(
        copy.deepcopy(payload["appearances"][0])
        | {"id": "appearance-missing-match-type"}
    )
    payload["over_under_lines"].append(
        copy.deepcopy(payload["over_under_lines"][0])
        | {
            "id": "line-missing-match-type",
            "over_under": {
                "appearance_stat": {
                    "appearance_id": "appearance-missing-match-type",
                    "display_stat": "Rebounds",
                }
            },
        }
    )
    payload["appearances"][-1].pop("match_type")

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    assert [market.market_id for market in snapshot.markets] == ["line-1"]
    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.coverage.fanout_complete is False
    assert "missing_match_type" in snapshot.coverage.skipped_reasons


def test_underdog_excludes_linked_final_game_status() -> None:
    payload = _payload()
    payload["games"][0]["status"] = "final"

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    assert snapshot.markets == ()
    assert "ineligible_event_status" in snapshot.coverage.skipped_reasons


def test_malformed_underdog_row_is_partial_when_another_row_is_valid() -> None:
    payload = _payload()
    rows = payload["over_under_lines"]
    assert isinstance(rows, list)
    rows.append(
        {
            "id": "line-missing-appearance",
            "stat_value": "8.5",
            "status": "active",
            "over_under": {
                "appearance_stat": {
                    "appearance_id": "missing",
                    "display_stat": "Assists",
                }
            },
            "options": [],
        }
    )

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert len(snapshot.markets) == 1
    assert snapshot.coverage.skipped_count == 1
    assert snapshot.coverage.fanout_complete is False
    assert CoverageCode.MALFORMED_RECORD in snapshot.coverage.skipped_reasons
    assert "line appearance could not be resolved" in snapshot.coverage.diagnostic_details


@pytest.mark.parametrize(
    "price",
    [
        -112.5,
        "-112.5",
        float("inf"),
        float("-inf"),
        float("nan"),
        "Infinity",
        "NaN",
        1e400,
        "1E+400",
        True,
    ],
)
def test_an_unusable_underdog_american_price_is_one_typed_malformed_record(price):
    payload = _payload()
    row = copy.deepcopy(payload["over_under_lines"][0])
    row["id"] = "line-2"
    row["over_under"]["appearance_stat"]["display_stat"] = "Assists"
    row["options"] = [
        dict(row["options"][0], id="selection-higher-2", american_price=price)
    ]
    payload["over_under_lines"].append(row)

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert [market.market_id for market in snapshot.markets] == ["line-1"]
    assert snapshot.coverage.skipped_count == 1
    assert CoverageCode.MALFORMED_RECORD in snapshot.coverage.skipped_reasons
    assert all(
        str(price) not in detail for detail in snapshot.coverage.diagnostic_details
    )


@pytest.mark.parametrize(
    ("price", "expected"), [(-112, -112), ("-112", -112), (-112.0, -112)]
)
def test_an_integral_underdog_american_price_is_kept_exactly(price, expected):
    payload = _payload()
    payload["over_under_lines"][0]["options"][0]["american_price"] = price

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    assert snapshot.markets[0].selections[0].american_price == expected


def test_all_malformed_underdog_records_emit_one_malformed_event() -> None:
    payload = _payload()
    row = payload["over_under_lines"][0]
    row["over_under"]["appearance_stat"]["appearance_id"] = "missing"

    with pytest.raises(ProviderUnavailableError) as raised:
        UnderdogAdapter(
            session=FakeSession(FakeResponse(payload))
        ).get_snapshot(_query(), _context("all-malformed-underdog"))

    assert raised.value.code == "provider_unavailable"
    events = telemetry.get_recorded_provider_events()
    assert len(events) == 1
    assert events[0]["provider"] == telemetry.PROVIDER_UNDERDOG
    assert events[0]["operation"] == "get_snapshot"
    assert events[0]["request_id"] == "all-malformed-underdog"
    assert events[0]["outcome"] == telemetry.OUTCOME_MALFORMED
    metrics = telemetry.snapshot_metrics()
    assert metrics["provider_failures"][telemetry.PROVIDER_UNDERDOG][
        telemetry.OUTCOME_MALFORMED
    ] == 1


def test_underdog_conflicting_duplicate_identity_is_malformed() -> None:
    first = _payload()
    conflict = copy.deepcopy(first)
    conflict["over_under_lines"][0]["stat_value"] = "13.500"
    first["over_under_lines"].append(conflict["over_under_lines"][0])

    with pytest.raises((ProviderUnavailableError, MalformedProviderResponseError)):
        UnderdogAdapter(
            session=FakeSession(FakeResponse(first))
        ).get_snapshot(_query(), _context())


def test_underdog_conflict_is_counted_once_after_identity_acceptance() -> None:
    payload = _payload()
    rows = payload["over_under_lines"]
    assert isinstance(rows, list)
    conflict = copy.deepcopy(rows[0])
    conflict["stat_value"] = "13.500"
    retained = copy.deepcopy(rows[0])
    retained["id"] = "line-retained"
    rows.extend([conflict, retained])

    snapshot = UnderdogAdapter(
        session=FakeSession(FakeResponse(payload))
    ).get_snapshot(_query(), _context())

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert [market.market_id for market in snapshot.markets] == ["line-retained"]
    assert snapshot.coverage.fetched_count == 3
    assert snapshot.coverage.eligible_count == 2
    assert snapshot.coverage.normalized_count == 2
    assert snapshot.coverage.skipped_count == 1


def test_underdog_timeout_is_typed_provider_error() -> None:
    with pytest.raises(ProviderUnavailableError):
        UnderdogAdapter(
            session=FakeSession(requests.ReadTimeout("timed out"))
        ).get_snapshot(_query(), _context())


def test_underdog_rejects_response_returned_after_absolute_deadline() -> None:
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    deadline = start + timedelta(seconds=1)
    clock = [start]
    session = ClockAdvancingSession(FakeResponse(_payload()), clock, deadline)

    with pytest.raises(ProviderUnavailableError, match="deadline"):
        UnderdogAdapter(
            session=session,
            timeout=(10.0, 30.0),
            now=lambda: clock[0],
        ).get_snapshot(
            _query(), RetrievalContext(deadline=deadline, request_id="late-underdog")
        )

    assert session.calls[0][1] == pytest.approx((1.0, 1.0))
    event = telemetry.get_recorded_provider_events()[0]
    assert event["request_id"] == "late-underdog"
    assert event["outcome"] == telemetry.OUTCOME_TIMEOUT


def test_underdog_does_not_start_after_absolute_deadline() -> None:
    deadline = datetime(2030, 1, 1, tzinfo=timezone.utc)
    session = FakeSession(FakeResponse(_payload()))

    with pytest.raises(ProviderUnavailableError, match="deadline"):
        UnderdogAdapter(
            session=session,
            now=lambda: deadline,
        ).get_snapshot(
            _query(), RetrievalContext(deadline=deadline, request_id="expired-underdog")
        )

    assert session.calls == []
    assert telemetry.get_recorded_provider_events() == []


def test_malformed_underdog_payload_is_recorded_as_provider_failure() -> None:
    with pytest.raises(ProviderUnavailableError):
        UnderdogAdapter(
            session=FakeSession(FakeResponse({}))
        ).get_snapshot(_query(), _context("malformed-underdog"))

    event = telemetry.get_recorded_provider_events()[0]
    assert event["outcome"] == telemetry.OUTCOME_MALFORMED
    assert event["request_id"] == "malformed-underdog"


def test_underdog_does_not_hide_implementation_defects(monkeypatch) -> None:
    session = FakeSession(FakeResponse(_payload()))
    adapter = UnderdogAdapter(session=session)

    def broken_parser(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("adapter bug")

    monkeypatch.setattr(adapter, "_normalize_payload", broken_parser)

    with pytest.raises(RuntimeError, match="adapter bug"):
        adapter.get_snapshot(_query(), _context())


def test_underdog_does_not_hide_value_error_implementation_defects(monkeypatch) -> None:
    session = FakeSession(FakeResponse(_payload()))
    adapter = UnderdogAdapter(session=session)

    def broken_line(*args, **kwargs):
        del args, kwargs
        raise ValueError("adapter bug")

    monkeypatch.setattr(UnderdogAdapter, "_normalize_line", broken_line)

    with pytest.raises(ValueError, match="adapter bug"):
        adapter.get_snapshot(_query(), _context())
