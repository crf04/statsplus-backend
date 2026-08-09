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
