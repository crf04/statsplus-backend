"""Offline behavior tests for the Dabble Provider Snapshot adapter."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from app.errors import ProviderUnavailableError
from app.providers.dabble import DabbleAdapter
from app.providers.dfs import (
    CoverageEvidence,
    NBAMarketQuery,
    RetrievalContext,
    SnapshotStatus,
)
from app.utils import telemetry


FIXTURES = Path(__file__).parents[1] / "fixtures" / "dabble"
DEADLINE = "2026-08-09T17:00:00Z"


def _payload(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _context(*, deadline: str = DEADLINE) -> RetrievalContext:
    return RetrievalContext(deadline=deadline, request_id="dabble-test")


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


@pytest.fixture(autouse=True)
def _clear_provider_events():
    from app.utils import telemetry

    telemetry.clear_recorded_provider_events()
    yield
    telemetry.clear_recorded_provider_events()


def test_get_snapshot_hides_discovery_and_groups_actual_selections():
    session = Mock()
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(_payload("fixtures.valid.json")),
        FakeResponse(_payload("fixture_details.valid.json")),
    ]
    adapter = DabbleAdapter(session=session)

    snapshot = adapter.get_snapshot(NBAMarketQuery(), _context())

    assert snapshot.provider == "dabble"
    assert snapshot.status is SnapshotStatus.COMPLETE
    assert len(snapshot.markets) == 2
    assert snapshot.coverage == CoverageEvidence(
        fetched_count=3,
        eligible_count=3,
        normalized_count=3,
        skipped_count=0,
        pagination_complete=True,
        fanout_complete=True,
    )
    first = snapshot.markets[0]
    assert first.market_id == "market-1"
    assert first.athlete.provider_id == "player-1"
    assert first.athlete.name == "LeBron James"
    assert first.team.provider_id == "team-1"
    assert first.team.abbreviation == "LAL"
    assert first.event.provider_id == "fixture-1"
    assert first.event.starts_at == datetime(
        2026, 8, 9, 16, 30, tzinfo=timezone.utc
    )
    assert first.competition.provider_id == (
        "090c2877-4d13-4f6e-8faf-886092153c58"
    )
    assert first.sport.label == "Basketball"
    assert first.threshold.value == Decimal("45.5")
    assert first.threshold.original_value == "45.5"
    assert first.statistic.components == ("points", "rebounds", "assists")
    assert [selection.direction.value for selection in first.selections] == [
        "higher",
        "lower",
    ]
    assert first.selections[0].selection_id == "selection-over"
    assert first.selections[0].modifiers[0].value == Decimal("1.5")
    assert first.selections[0].modifiers[0].kind == "multiplier"
    assert first.selections[0].modifiers[0].scope == "selection"
    assert first.selections[0].modifiers[0].label == "1.5x"
    assert first.variant.value == "unknown"
    assert first.variant_label is None

    paths = [call.args[0] for call in session.get.call_args_list]
    assert paths == [
        f"{adapter.BASE_URL}/competitions",
        f"{adapter.BASE_URL}/frontend-api/competitions/"
        "090c2877-4d13-4f6e-8faf-886092153c58/sport-fixtures",
        f"{adapter.BASE_URL}/frontend-api/sport-fixtures/details/fixture-1",
    ]
    assert session.get.call_args_list[1].kwargs["params"] == {
        "includeInPlay": "false"
    }


def test_missing_provider_ids_are_retained_as_null_without_fabrication():
    detail = _payload("fixture_details.valid.json")
    prop = detail["sportFixtureDetail"]["playerProps"][0]
    prop.pop("playerId")
    prop.pop("teamId")
    prop.pop("selectionId")
    prop.pop("marketId")
    session = Mock()
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(_payload("fixtures.valid.json")),
        FakeResponse(detail),
    ]

    snapshot = DabbleAdapter(session=session).get_snapshot(
        NBAMarketQuery(), _context()
    )

    market = snapshot.markets[0]
    assert market.market_id is None
    assert market.athlete.provider_id is None
    assert market.team.provider_id is None
    assert market.selections[0].selection_id is None


def test_idless_markets_with_distinct_variant_labels_remain_distinct():
    detail = _payload("fixture_details.valid.json")
    original = detail["sportFixtureDetail"]["playerProps"][0]
    first = copy.deepcopy(original)
    second = copy.deepcopy(original)
    first.pop("marketId")
    second.pop("marketId")
    first["variant"] = "Partner A"
    second["variant"] = "Partner B"
    detail["sportFixtureDetail"]["playerProps"] = [
        first,
        second,
        detail["sportFixtureDetail"]["playerProps"][2],
    ]
    session = Mock()
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(_payload("fixtures.valid.json")),
        FakeResponse(detail),
    ]

    snapshot = DabbleAdapter(session=session).get_snapshot(
        NBAMarketQuery(), _context()
    )

    idless = [market for market in snapshot.markets if market.market_id is None]
    assert len(idless) == 2
    assert {market.variant_label for market in idless} == {"Partner A", "Partner B"}
    assert {market.variant.value for market in idless} == {"unknown"}


@pytest.mark.parametrize(
    ("status", "expected_status"),
    [("Open", "available"), ("Suspended", "suspended")],
)
def test_available_and_suspended_pregame_markets_are_eligible(
    status: str, expected_status: str
):
    fixture = _payload("fixtures.valid.json")
    fixture["data"][0]["status"] = status
    detail = _payload("fixture_details.valid.json")
    detail["sportFixtureDetail"]["status"] = status
    session = Mock()
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(fixture),
        FakeResponse(detail),
    ]

    snapshot = DabbleAdapter(session=session).get_snapshot(
        NBAMarketQuery(), _context()
    )

    assert snapshot.markets
    assert {market.status.value for market in snapshot.markets} == {expected_status}


@pytest.mark.parametrize("status", ["Live", "InPlay", "Closed", "Settled"])
def test_live_closed_and_settled_fixtures_are_excluded_with_coverage(status: str):
    fixture = _payload("fixtures.valid.json")
    fixture["data"][0]["status"] = status
    session = Mock()
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(fixture),
    ]

    snapshot = DabbleAdapter(session=session).get_snapshot(
        NBAMarketQuery(), _context()
    )

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.markets == ()
    assert snapshot.coverage.skipped_count == 1
    assert "ineligible_status" in snapshot.coverage.skipped_reasons
    assert session.get.call_count == 2


def test_non_nba_competitions_are_excluded():
    competitions = _payload("competitions.valid.json")
    competitions["data"][0]["name"] = "NFL"
    session = Mock()
    session.get.return_value = FakeResponse(competitions)

    snapshot = DabbleAdapter(session=session).get_snapshot(
        NBAMarketQuery(), _context()
    )

    assert snapshot.markets == ()
    assert "non_nba_competition" in snapshot.coverage.skipped_reasons
    assert session.get.call_count == 1


def test_malformed_prop_is_skipped_and_makes_nonempty_snapshot_partial():
    detail = _payload("fixture_details.valid.json")
    detail["sportFixtureDetail"]["playerProps"][1]["value"] = "not-a-number"
    session = Mock()
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(_payload("fixtures.valid.json")),
        FakeResponse(detail),
    ]

    snapshot = DabbleAdapter(session=session).get_snapshot(
        NBAMarketQuery(), _context()
    )

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.markets
    assert snapshot.coverage.fanout_complete is False
    assert snapshot.coverage.skipped_count == 1
    assert "malformed_record" in snapshot.coverage.warning_codes


def test_failed_fixture_detail_yields_partial_snapshot_when_another_succeeds():
    fixtures = _payload("fixtures.valid.json")
    fixtures["data"].append(
        {
            "id": "fixture-2",
            "name": "Boston Celtics @ Miami Heat",
            "competitionId": fixtures["data"][0]["competitionId"],
            "competitionName": "NBA",
            "advertisedStart": "2026-08-10T16:30:00.000Z",
            "status": "Open",
        }
    )
    session = Mock()

    def get(url, **kwargs):
        del kwargs
        if url.endswith("/competitions"):
            return FakeResponse(_payload("competitions.valid.json"))
        if url.endswith("sport-fixtures"):
            return FakeResponse(fixtures)
        if url.endswith("fixture-1"):
            return FakeResponse(_payload("fixture_details.valid.json"))
        raise requests.ReadTimeout("fixture-2")

    session.get.side_effect = get

    snapshot = DabbleAdapter(session=session).get_snapshot(
        NBAMarketQuery(), _context()
    )

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.markets
    assert snapshot.coverage.fanout_complete is False
    assert "fixture_failed" in snapshot.coverage.warning_codes


def test_all_upstream_detail_failures_translate_to_provider_unavailable():
    session = Mock()
    session.get.side_effect = requests.ReadTimeout("private upstream")

    with pytest.raises(ProviderUnavailableError) as raised:
        DabbleAdapter(session=session).get_snapshot(NBAMarketQuery(), _context())

    assert raised.value.code == "provider_unavailable"


def test_deadline_before_discovery_translates_to_provider_unavailable():
    session = Mock()
    context = _context(deadline="2026-08-09T16:00:00Z")

    with pytest.raises(ProviderUnavailableError):
        DabbleAdapter(
            session=session,
            now=lambda: datetime(2026, 8, 9, 17, tzinfo=timezone.utc),
        ).get_snapshot(NBAMarketQuery(), context)

    session.get.assert_not_called()


def test_identical_repeated_market_identity_deduplicates():
    detail = _payload("fixture_details.valid.json")
    detail["sportFixtureDetail"]["playerProps"].append(
        copy.deepcopy(detail["sportFixtureDetail"]["playerProps"][0])
    )
    session = Mock()
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(_payload("fixtures.valid.json")),
        FakeResponse(detail),
    ]

    snapshot = DabbleAdapter(session=session).get_snapshot(
        NBAMarketQuery(), _context()
    )

    assert len(snapshot.markets) == 2
    assert "duplicate_source_identity" in snapshot.coverage.warning_codes


def test_conflicting_repeated_market_identity_is_malformed_and_partial():
    detail = _payload("fixture_details.valid.json")
    conflict = copy.deepcopy(detail["sportFixtureDetail"]["playerProps"][0])
    conflict["value"] = 46.5
    detail["sportFixtureDetail"]["playerProps"].append(conflict)
    session = Mock()
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(_payload("fixtures.valid.json")),
        FakeResponse(detail),
    ]

    snapshot = DabbleAdapter(session=session).get_snapshot(
        NBAMarketQuery(), _context()
    )

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.markets
    assert "conflicting_source_identity" in snapshot.coverage.warning_codes


def test_invalid_json_and_http_errors_are_provider_unavailable():
    for response in (
        FakeResponse(ValueError("bad json")),
        FakeResponse({}, status_code=403),
    ):
        session = Mock()
        session.get.return_value = response
        with pytest.raises(ProviderUnavailableError):
            DabbleAdapter(session=session).get_snapshot(NBAMarketQuery(), _context())


def test_top_level_malformed_detail_with_other_valid_detail_is_partial():
    fixtures = _payload("fixtures.valid.json")
    fixtures["data"].append(
        {
            "id": "fixture-2",
            "competitionId": fixtures["data"][0]["competitionId"],
            "competitionName": "NBA",
            "advertisedStart": "2026-08-10T16:30:00.000Z",
            "status": "Open",
        }
    )
    session = Mock()

    def get(url, **kwargs):
        del kwargs
        if url.endswith("/competitions"):
            return FakeResponse(_payload("competitions.valid.json"))
        if url.endswith("sport-fixtures"):
            return FakeResponse(fixtures)
        if url.endswith("fixture-1"):
            return FakeResponse(_payload("fixture_details.valid.json"))
        return FakeResponse({"sportFixtureDetail": {"playerProps": "bad"}})

    session.get.side_effect = get
    snapshot = DabbleAdapter(session=session).get_snapshot(
        NBAMarketQuery(), _context()
    )

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert "fixture_malformed" in snapshot.coverage.warning_codes


def test_dabble_telemetry_uses_retrieval_context_request_id():
    session = Mock()
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(_payload("fixtures.valid.json")),
        FakeResponse(_payload("fixture_details.valid.json")),
    ]

    DabbleAdapter(session=session).get_snapshot(
        NBAMarketQuery(), _context()
    )

    events = telemetry.get_recorded_provider_events()
    assert events
    assert {event["request_id"] for event in events} == {"dabble-test"}
