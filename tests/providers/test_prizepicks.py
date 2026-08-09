"""Offline tests for the PrizePicks DFS snapshot adapter."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from app.errors import ProviderUnavailableError
from app.utils import telemetry
from app.providers.dfs import (
    CoverageCode,
    MarketVariant,
    MalformedProviderResponseError,
    NBAMarketQuery,
    RetrievalContext,
    ScoringPeriod,
    SnapshotStatus,
)
from app.providers.prizepicks import PrizePicksAdapter


FIXTURES = Path(__file__).parents[1] / "fixtures" / "prizepicks"


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
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object], tuple[float, float]]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, *, params, timeout):
        self.calls.append((url, params, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _payload(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text())


def _context(request_id: str | None = None) -> RetrievalContext:
    return RetrievalContext(deadline="2030-01-01T00:00:00Z", request_id=request_id)


def _query() -> NBAMarketQuery:
    return NBAMarketQuery()


def test_get_snapshot_paginates_and_keeps_typed_prizepicks_evidence() -> None:
    session = FakeSession(
        [
            FakeResponse(_payload("projections.page1.valid.json")),
            FakeResponse(_payload("projections.page2.valid.json")),
        ]
    )

    snapshot = PrizePicksAdapter(session=session).get_snapshot(
        _query(), _context("prizepicks-request")
    )

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert len(snapshot.markets) == 1
    market = snapshot.markets[0]
    assert market.market_id == "projection-1"
    assert market.athlete is not None
    assert market.athlete.provider_id == "player-1"
    assert market.athlete.name == "Luka Doncic"
    assert market.team is not None
    assert market.team.provider_id == "team-dal"
    assert market.team.abbreviation == "DAL"
    assert market.league is not None
    assert market.league.provider_id == "7"
    assert market.league.label == "NBA"
    assert market.competition is not None
    assert market.competition.label == "NBA"
    assert market.sport is not None
    assert market.sport.label == "NBA"
    assert market.event is not None
    assert market.event.provider_id is None
    assert market.event.label == "DAL vs. SAS"
    assert market.event.starts_at == datetime(2026, 8, 10, 5, tzinfo=timezone.utc)
    assert market.starts_at == market.event.starts_at
    assert market.updated_at == datetime(2026, 8, 9, 23, tzinfo=timezone.utc)
    assert market.threshold is not None
    assert str(market.threshold.value) == "27.500"
    assert market.threshold.original_value == "27.500"
    assert market.variant is MarketVariant.STANDARD
    assert market.variant_label == "standard"
    assert market.scoring_period is ScoringPeriod.UNKNOWN
    assert market.scoring_period_label is None
    assert market.selections == ()
    assert snapshot.coverage.fetched_count == 2
    assert snapshot.coverage.eligible_count == 2
    assert snapshot.coverage.normalized_count == 2
    assert snapshot.coverage.skipped_count == 0
    assert snapshot.coverage.pagination_complete is True
    assert "duplicate_source_identity" in snapshot.coverage.warning_codes
    assert [call[1]["page"] for call in session.calls] == [1, 2]
    events = telemetry.get_recorded_provider_events()
    assert len(events) == 2
    assert {event["provider"] for event in events} == {
        telemetry.PROVIDER_PRIZEPICKS
    }
    assert {event["operation"] for event in events} == {"get_snapshot"}
    assert {event["request_id"] for event in events} == {"prizepicks-request"}


def test_later_prizepicks_page_failure_returns_partial_snapshot() -> None:
    session = FakeSession(
        [
            FakeResponse(_payload("projections.page1.valid.json")),
            requests.ReadTimeout("later page timed out"),
        ]
    )

    snapshot = PrizePicksAdapter(session=session).get_snapshot(_query(), _context())

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert len(snapshot.markets) == 1
    assert snapshot.coverage.pagination_complete is False
    assert "page_fetch_failed" in snapshot.coverage.warning_codes


def test_prizepicks_repeated_wrong_page_is_partial_with_incomplete_pagination() -> None:
    page_one = _payload("projections.page1.valid.json")
    session = FakeSession(
        [
            FakeResponse(page_one),
            FakeResponse(copy.deepcopy(page_one)),
        ]
    )

    snapshot = PrizePicksAdapter(
        session=session
    ).get_snapshot(_query(), _context())

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert len(snapshot.markets) == 1
    assert snapshot.coverage.pagination_complete is False
    assert "page_metadata_mismatch" in snapshot.coverage.warning_codes
    assert "pagination_page_mismatch" in snapshot.coverage.skipped_reasons
    assert [call[1]["page"] for call in session.calls] == [1, 2]


def test_prizepicks_shrinking_total_pages_is_partial_with_incomplete_pagination() -> None:
    page_one = _payload("projections.page1.valid.json")
    page_one["meta"] = {"current_page": 1, "total_pages": 3}
    page_two = _payload("projections.page2.valid.json")
    page_two["meta"] = {"current_page": 2, "total_pages": 2}
    session = FakeSession([FakeResponse(page_one), FakeResponse(page_two)])

    snapshot = PrizePicksAdapter(session=session).get_snapshot(_query(), _context())

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert len(snapshot.markets) == 1
    assert snapshot.coverage.pagination_complete is False
    assert "page_metadata_mismatch" in snapshot.coverage.warning_codes
    assert "pagination_total_pages_changed" in snapshot.coverage.skipped_reasons
    assert [call[1]["page"] for call in session.calls] == [1, 2]


def test_missing_prizepicks_relationship_is_partial_when_another_row_is_valid() -> None:
    payload = _payload("projections.page1.valid.json")
    rows = payload["data"]
    assert isinstance(rows, list)
    rows.append(
        {
            "type": "projection",
            "id": "projection-missing-player",
            "attributes": {
                "line_score": "10.5",
                "stat_type": "Assists",
                "status": "pre_game",
            },
            "relationships": {
                "new_player": {"data": {"type": "new_player", "id": "missing"}},
                "league": {"data": {"type": "league", "id": "7"}},
            },
        }
    )
    payload["meta"] = {"current_page": 1, "total_pages": 1}

    snapshot = PrizePicksAdapter(
        session=FakeSession([FakeResponse(payload)])
    ).get_snapshot(_query(), _context())

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert [market.market_id for market in snapshot.markets] == ["projection-1"]
    assert snapshot.coverage.skipped_count == 1
    assert snapshot.coverage.fanout_complete is False
    assert CoverageCode.MALFORMED_RECORD in snapshot.coverage.skipped_reasons
    assert "projection relationships could not be resolved" in snapshot.coverage.diagnostic_details


def test_missing_prizepicks_relationship_without_usable_rows_is_provider_error() -> None:
    payload = _payload("projections.page1.valid.json")
    payload["data"] = [payload["data"][0]]
    row = payload["data"][0]
    row["relationships"]["new_player"]["data"]["id"] = "missing"
    payload["meta"] = {"current_page": 1, "total_pages": 1}

    with pytest.raises(ProviderUnavailableError):
        PrizePicksAdapter(
            session=FakeSession([FakeResponse(payload)])
        ).get_snapshot(_query(), _context())


def test_prizepicks_excludes_closed_rows_and_preserves_unknown_variant_label() -> None:
    payload = _payload("projections.page1.valid.json")
    row = payload["data"][0]
    row["attributes"]["status"] = "closed"
    row["attributes"]["odds_type"] = "demon"
    payload["meta"] = {"current_page": 1, "total_pages": 1}

    snapshot = PrizePicksAdapter(
        session=FakeSession([FakeResponse(payload)])
    ).get_snapshot(_query(), _context())

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.markets == ()
    assert snapshot.coverage.skipped_count == 1
    assert "ineligible_status" in snapshot.coverage.skipped_reasons


def test_prizepicks_excludes_linked_live_event_and_keeps_eligible_status_evidence() -> None:
    payload = _payload("projections.page1.valid.json")
    row = payload["data"][0]
    included = payload["included"]
    assert isinstance(included, list)
    row["relationships"]["event"] = {
        "data": {"type": "game", "id": "game-1"}
    }
    included.append(
        {
            "type": "game",
            "id": "game-1",
            "attributes": {"name": "DAL vs. SAS", "status": "scheduled"},
        }
    )
    payload["meta"] = {"current_page": 1, "total_pages": 1}

    eligible = PrizePicksAdapter(
        session=FakeSession([FakeResponse(payload)])
    ).get_snapshot(_query(), _context())
    assert eligible.markets[0].event is not None
    assert eligible.markets[0].event.status_label == "scheduled"

    payload["included"][-1]["attributes"]["status"] = "in-play"
    excluded = PrizePicksAdapter(
        session=FakeSession([FakeResponse(payload)])
    ).get_snapshot(_query(), _context())
    assert excluded.markets == ()
    assert "ineligible_event_status" in excluded.coverage.skipped_reasons


def test_prizepicks_recorded_boundary_excludes_ineligible_market_kinds_with_coverage() -> None:
    payload = _payload("projections.page1.valid.json")
    rows = payload["data"]
    included = payload["included"]
    assert isinstance(rows, list)
    assert isinstance(included, list)

    for index, status in enumerate(("live", "closed", "settled"), start=1):
        row = copy.deepcopy(rows[0])
        row["id"] = f"projection-status-{index}"
        row["attributes"]["status"] = status
        rows.append(row)

    for index, kind in enumerate(
        ("Team", "Match", "Futures", "Entry Placement"), start=1
    ):
        row = copy.deepcopy(rows[0])
        row["id"] = f"projection-kind-{index}"
        row["attributes"]["projection_type"] = kind
        rows.append(row)

    non_nba = copy.deepcopy(rows[0])
    non_nba["id"] = "projection-nfl"
    non_nba["relationships"]["league"]["data"] = {
        "type": "league",
        "id": "nfl",
    }
    rows.append(non_nba)
    included.append(
        {
            "type": "league",
            "id": "nfl",
            "attributes": {"name": "NFL"},
        }
    )
    payload["meta"] = {"current_page": 1, "total_pages": 1}

    snapshot = PrizePicksAdapter(
        session=FakeSession([FakeResponse(payload)])
    ).get_snapshot(_query(), _context())

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert [market.market_id for market in snapshot.markets] == ["projection-1"]
    assert snapshot.coverage.fetched_count == 9
    assert snapshot.coverage.eligible_count == 1
    assert snapshot.coverage.skipped_count == 8
    assert "ineligible_status" in snapshot.coverage.skipped_reasons
    assert "non_player_market" in snapshot.coverage.skipped_reasons
    assert "non_nba_market" in snapshot.coverage.skipped_reasons


def test_prizepicks_future_with_linked_event_is_excluded_with_coverage() -> None:
    payload = _payload("projections.page1.valid.json")
    row = payload["data"][0]
    included = payload["included"]
    assert isinstance(included, list)
    row["attributes"]["projection_type"] = "Futures"
    row["relationships"]["event"] = {
        "data": {"type": "game", "id": "game-future-1"}
    }
    included.append(
        {
            "type": "game",
            "id": "game-future-1",
            "attributes": {
                "name": "DAL vs. SAS",
                "status": "scheduled",
            },
        }
    )
    payload["meta"] = {"current_page": 1, "total_pages": 1}

    snapshot = PrizePicksAdapter(
        session=FakeSession([FakeResponse(payload)])
    ).get_snapshot(_query(), _context())

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.markets == ()
    assert snapshot.coverage.fetched_count == 1
    assert snapshot.coverage.skipped_count == 1
    assert CoverageCode.NON_PLAYER_MARKET in snapshot.coverage.skipped_reasons


def test_prizepicks_active_player_future_without_event_is_excluded() -> None:
    payload = _payload("projections.page1.valid.json")
    row = payload["data"][0]
    row["attributes"]["projection_type"] = "Futures"
    row["attributes"]["description"] = "NBA champion"
    payload["meta"] = {"current_page": 1, "total_pages": 1}

    snapshot = PrizePicksAdapter(
        session=FakeSession([FakeResponse(payload)])
    ).get_snapshot(_query(), _context())

    assert snapshot.markets == ()
    assert snapshot.coverage.fetched_count == 1
    assert snapshot.coverage.skipped_count == 1
    assert CoverageCode.NON_PLAYER_MARKET in snapshot.coverage.skipped_reasons


def test_prizepicks_linked_future_relationship_is_excluded_with_coverage() -> None:
    payload = _payload("projections.page1.valid.json")
    rows = payload["data"]
    included = payload["included"]
    assert isinstance(rows, list)
    assert isinstance(included, list)

    linked_future = copy.deepcopy(rows[0])
    linked_future["id"] = "projection-linked-future"
    linked_future["relationships"]["future"] = {
        "data": {"type": "future", "id": "future-2026-nba-champion"}
    }
    rows.append(linked_future)
    included.append(
        {
            "type": "future",
            "id": "future-2026-nba-champion",
            "attributes": {"name": "2026 NBA Champion"},
        }
    )
    payload["meta"] = {"current_page": 1, "total_pages": 1}

    snapshot = PrizePicksAdapter(
        session=FakeSession([FakeResponse(payload)])
    ).get_snapshot(_query(), _context())

    assert [market.market_id for market in snapshot.markets] == ["projection-1"]
    assert snapshot.coverage.fetched_count == 2
    assert snapshot.coverage.skipped_count == 1
    assert CoverageCode.NON_PLAYER_MARKET in snapshot.coverage.skipped_reasons


def test_prizepicks_linked_future_without_resource_is_excluded_without_event_id() -> None:
    payload = _payload("projections.page1.valid.json")
    rows = payload["data"]
    assert isinstance(rows, list)

    linked_future = copy.deepcopy(rows[0])
    linked_future["id"] = "projection-linked-future-without-resource"
    linked_future["relationships"]["future"] = {
        "data": {"type": "future", "id": "future-not-included"}
    }
    rows.append(linked_future)
    payload["meta"] = {"current_page": 1, "total_pages": 1}

    snapshot = PrizePicksAdapter(
        session=FakeSession([FakeResponse(payload)])
    ).get_snapshot(_query(), _context())

    assert [market.market_id for market in snapshot.markets] == ["projection-1"]
    assert snapshot.markets[0].event is not None
    assert snapshot.markets[0].event.provider_id is None
    assert snapshot.coverage.skipped_count == 1
    assert CoverageCode.NON_PLAYER_MARKET in snapshot.coverage.skipped_reasons


def test_prizepicks_malformed_linked_future_relationship_is_partial() -> None:
    payload = _payload("projections.page1.valid.json")
    rows = payload["data"]
    assert isinstance(rows, list)

    malformed_future = copy.deepcopy(rows[0])
    malformed_future["id"] = "projection-malformed-future"
    malformed_future["relationships"]["future"] = {
        "data": {"type": "future"}
    }
    rows.append(malformed_future)
    payload["meta"] = {"current_page": 1, "total_pages": 1}

    snapshot = PrizePicksAdapter(
        session=FakeSession([FakeResponse(payload)])
    ).get_snapshot(_query(), _context())

    assert [market.market_id for market in snapshot.markets] == ["projection-1"]
    assert snapshot.coverage.skipped_count == 1
    assert snapshot.coverage.fanout_complete is False
    assert CoverageCode.MALFORMED_RECORD in snapshot.coverage.warning_codes
    assert CoverageCode.MALFORMED_RECORD in snapshot.coverage.skipped_reasons
    assert "id must be present" in snapshot.coverage.diagnostic_details


def test_prizepicks_deadline_is_checked_after_upstream_response() -> None:
    deadline = datetime(2030, 1, 1, tzinfo=timezone.utc)
    now = {"value": datetime(2029, 12, 31, 23, 59, 55, tzinfo=timezone.utc)}
    session = FakeSession([FakeResponse(_payload("projections.page1.valid.json"))])
    original_get = session.get

    def get_and_expire(url, *, params, timeout):
        response = original_get(url, params=params, timeout=timeout)
        now["value"] = deadline.replace(second=1)
        return response

    session.get = get_and_expire

    with pytest.raises(ProviderUnavailableError, match="deadline"):
        PrizePicksAdapter(session=session, now=lambda: now["value"]).get_snapshot(
            _query(),
            RetrievalContext(deadline=deadline),
        )

    assert session.calls[0][2] == (5.0, 5.0)


def test_prizepicks_first_page_timeout_is_typed_provider_error() -> None:
    with pytest.raises(ProviderUnavailableError):
        PrizePicksAdapter(
            session=FakeSession([requests.ReadTimeout("timed out")])
        ).get_snapshot(_query(), _context())


def test_malformed_prizepicks_page_is_recorded_as_provider_failure() -> None:
    with pytest.raises(ProviderUnavailableError):
        PrizePicksAdapter(
            session=FakeSession([FakeResponse({})])
        ).get_snapshot(_query(), _context("malformed-prizepicks"))

    event = telemetry.get_recorded_provider_events()[0]
    assert event["outcome"] == telemetry.OUTCOME_MALFORMED
    assert event["request_id"] == "malformed-prizepicks"


def test_prizepicks_conflicting_duplicate_identity_is_malformed() -> None:
    first = _payload("projections.page1.valid.json")
    conflict = copy.deepcopy(first)
    conflict["data"][0]["attributes"]["line_score"] = "28.500"
    conflict["meta"] = {"current_page": 2, "total_pages": 2}

    with pytest.raises((ProviderUnavailableError, MalformedProviderResponseError)):
        PrizePicksAdapter(
            session=FakeSession(
                [FakeResponse(first), FakeResponse(conflict)]
            )
        ).get_snapshot(_query(), _context())


def test_prizepicks_does_not_hide_implementation_defects(monkeypatch) -> None:
    session = FakeSession([FakeResponse(_payload("projections.page1.valid.json"))])
    adapter = PrizePicksAdapter(session=session)

    def broken_parser(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("adapter bug")

    monkeypatch.setattr(adapter, "_parse_page", broken_parser)

    with pytest.raises(RuntimeError, match="adapter bug"):
        adapter.get_snapshot(_query(), _context())


def test_prizepicks_does_not_hide_value_error_from_normalizer(monkeypatch) -> None:
    payload = _payload("projections.page1.valid.json")
    payload["meta"] = {"current_page": 1, "total_pages": 1}
    session = FakeSession([FakeResponse(payload)])

    def broken_market_kind(attributes):
        del attributes
        raise ValueError("normalizer bug")

    monkeypatch.setattr(
        PrizePicksAdapter,
        "_market_kind",
        staticmethod(broken_market_kind),
    )

    with pytest.raises(ValueError, match="normalizer bug"):
        PrizePicksAdapter(session=session).get_snapshot(_query(), _context())
