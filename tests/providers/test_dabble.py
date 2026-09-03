"""Offline behavior tests for the Dabble Provider Snapshot adapter."""

from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from app.errors import ProviderUnavailableError
from app.providers.dabble import DabbleAdapter, _SerializedSession
from app.providers.dfs import (
    CoverageEvidence,
    NBAMarketQuery,
    RetrievalContext,
    SnapshotStatus,
    DeadlineExceededError,
)
from app.utils import telemetry
from app.services.dfs_board import DFSBoardService, ProviderFailureReason


FIXTURES = Path(__file__).parents[1] / "fixtures" / "dabble"
# Ordinary fixture tests must not become wall-clock dependent.  Deadline
# semantics are covered explicitly below with injected clocks and timestamps.
DEADLINE = "2099-01-01T00:00:00Z"


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
    assert first.selections[0].modifiers[0].kind == "payout_multiplier"
    assert first.selections[0].modifiers[0].scope == "selection"
    assert first.selections[0].modifiers[0].label is None
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


def test_session_factory_builds_one_shared_session_for_the_adapter_lifetime():
    """Every request, including the detail fan-out, reuses one pooled client."""

    fixtures = _payload("fixtures.valid.json")
    details: dict[str, dict[str, object]] = {}
    for index in range(3):
        fixture = copy.deepcopy(fixtures["data"][0])
        fixture["id"] = f"fixture-{index + 1}"
        if index == 0:
            fixtures["data"][0] = fixture
        else:
            fixtures["data"].append(fixture)

        detail = _payload("fixture_details.valid.json")
        detail_data = detail["sportFixtureDetail"]
        detail_data["id"] = fixture["id"]
        detail_data["playerProps"] = [
            copy.deepcopy(detail_data["playerProps"][0])
        ]
        detail_data["playerProps"][0]["marketId"] = f"market-{index + 1}"
        detail_data["playerProps"][0]["selectionId"] = f"selection-{index + 1}"
        details[fixture["id"]] = detail

    detail_barrier = threading.Barrier(3)
    factory_lock = threading.Lock()
    sessions: list[object] = []

    class PooledSession:
        headers: dict[str, str] = {}

        def get(self, url: str, **kwargs: object) -> FakeResponse:
            del kwargs
            if "/details/" in url:
                # All three detail workers must reach here together, proving
                # the shared client still serves the concurrent fan-out.
                detail_barrier.wait(timeout=1.0)
                return FakeResponse(details[url.rsplit("/", 1)[-1]])
            if url.endswith("/competitions"):
                return FakeResponse(_payload("competitions.valid.json"))
            return FakeResponse(fixtures)

    def session_factory() -> PooledSession:
        session = PooledSession()
        with factory_lock:
            sessions.append(session)
        return session

    adapter = DabbleAdapter(session_factory=session_factory, detail_concurrency=3)

    snapshot = adapter.get_snapshot(NBAMarketQuery(), _context())
    second = adapter.get_snapshot(NBAMarketQuery(), _context())

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert len(snapshot.markets) == 3
    assert second.status is SnapshotStatus.COMPLETE
    assert len(sessions) == 1
    assert adapter.session is sessions[0]


def test_serialized_session_drops_queued_detail_calls_after_deadline():
    """Queued injected-session calls must not start upstream work late."""

    fixtures = _payload("fixtures.valid.json")
    details: dict[str, dict[str, object]] = {}
    for index in range(3):
        fixture = copy.deepcopy(fixtures["data"][0])
        fixture["id"] = f"fixture-{index + 1}"
        if index == 0:
            fixtures["data"][0] = fixture
        else:
            fixtures["data"].append(fixture)

        detail = _payload("fixture_details.valid.json")
        detail_data = detail["sportFixtureDetail"]
        detail_data["id"] = fixture["id"]
        detail_data["playerProps"] = [
            copy.deepcopy(detail_data["playerProps"][0])
        ]
        detail_data["playerProps"][0]["marketId"] = f"market-{index + 1}"
        detail_data["playerProps"][0]["selectionId"] = f"selection-{index + 1}"
        details[fixture["id"]] = detail

    first_detail_started = threading.Event()
    release_first_detail = threading.Event()
    all_detail_calls_started = threading.Event()
    detail_calls: list[str] = []

    class BlockingSession:
        headers: dict[str, str] = {}

        def get(self, url: str, **kwargs: object) -> FakeResponse:
            del kwargs
            if "/details/" not in url:
                if url.endswith("/competitions"):
                    return FakeResponse(_payload("competitions.valid.json"))
                return FakeResponse(fixtures)

            fixture_id = url.rsplit("/", 1)[-1]
            detail_calls.append(fixture_id)
            if len(detail_calls) == 1:
                first_detail_started.set()
                release_first_detail.wait(timeout=1.0)
            elif len(detail_calls) == 3:
                all_detail_calls_started.set()
            return FakeResponse(details[fixture_id])

    session = BlockingSession()
    deadline = datetime.now(timezone.utc) + timedelta(seconds=0.2)
    context = RetrievalContext(deadline=deadline, request_id="serialized-deadline")
    outcome: dict[str, BaseException | object] = {}

    def retrieve() -> None:
        try:
            outcome["snapshot"] = DabbleAdapter(
                session=session,
                detail_concurrency=3,
            ).get_snapshot(NBAMarketQuery(), context)
        except BaseException as error:
            outcome["error"] = error

    thread = threading.Thread(target=retrieve)
    thread.start()
    assert first_detail_started.wait(timeout=1.0)
    thread.join(timeout=1.0)
    try:
        assert not thread.is_alive()
        assert isinstance(outcome.get("error"), ProviderUnavailableError)
    finally:
        release_first_detail.set()
        thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert not all_detail_calls_started.wait(timeout=0.25)
    assert len(detail_calls) == 1


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


def test_idless_markets_with_distinct_scoring_periods_remain_distinct():
    detail = _payload("fixture_details.valid.json")
    original = detail["sportFixtureDetail"]["playerProps"][0]
    full_game = copy.deepcopy(original)
    first_half = copy.deepcopy(original)
    full_game.pop("marketId")
    first_half.pop("marketId")
    full_game["stats"] = ["points"]
    first_half["stats"] = ["first-half-points"]
    detail["sportFixtureDetail"]["playerProps"] = [
        full_game,
        first_half,
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
    assert {market.scoring_period.value for market in idless} == {
        "full_game",
        "first_half",
    }


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


def test_live_fixture_detail_cannot_be_overridden_by_open_prop_status():
    detail = _payload("fixture_details.valid.json")
    detail["sportFixtureDetail"]["status"] = "Live"
    detail["sportFixtureDetail"]["playerProps"][0]["status"] = "Open"
    session = Mock()
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(_payload("fixtures.valid.json")),
        FakeResponse(detail),
    ]

    snapshot = DabbleAdapter(session=session).get_snapshot(
        NBAMarketQuery(), _context()
    )

    assert snapshot.markets == ()
    assert "ineligible_status" in snapshot.coverage.skipped_reasons


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


def test_mixed_malformed_competition_and_ineligible_competition_is_total_failure():
    competitions = {
        "data": [
            {"name": "NBA"},
            {"id": "nfl", "name": "NFL", "sportName": "Football"},
        ]
    }
    session = Mock()
    session.get.return_value = FakeResponse(competitions)

    with pytest.raises(ProviderUnavailableError) as raised:
        DabbleAdapter(session=session).get_snapshot(NBAMarketQuery(), _context())

    assert raised.value.code == "provider_unavailable"
    events = telemetry.get_recorded_provider_events()
    assert [(event["operation"], event["outcome"]) for event in events] == [
        ("competition_lookup", telemetry.OUTCOME_SUCCESS),
        ("snapshot_normalization", telemetry.OUTCOME_MALFORMED),
    ]
    assert telemetry.snapshot_metrics()["provider_failures"] == {
        telemetry.PROVIDER_DABBLE: {telemetry.OUTCOME_MALFORMED: 1}
    }


def test_mixed_malformed_fixture_and_ineligible_fixture_is_total_failure():
    fixtures = {
        "data": [
            {"status": "Open"},
            {"id": "fixture-live", "status": "Live"},
        ]
    }
    session = Mock()
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(fixtures),
    ]

    with pytest.raises(ProviderUnavailableError) as raised:
        DabbleAdapter(session=session).get_snapshot(NBAMarketQuery(), _context())

    assert raised.value.code == "provider_unavailable"
    events = telemetry.get_recorded_provider_events()
    assert [(event["operation"], event["outcome"]) for event in events] == [
        ("competition_lookup", telemetry.OUTCOME_SUCCESS),
        ("competition_fixtures", telemetry.OUTCOME_SUCCESS),
        ("snapshot_normalization", telemetry.OUTCOME_MALFORMED),
    ]
    assert telemetry.snapshot_metrics()["provider_failures"] == {
        telemetry.PROVIDER_DABBLE: {telemetry.OUTCOME_MALFORMED: 1}
    }


def test_malformed_discovery_with_usable_market_is_partial_without_failure():
    competitions = _payload("competitions.valid.json")
    competitions["data"].insert(0, {"name": "NBA"})
    session = Mock()
    session.get.side_effect = [
        FakeResponse(competitions),
        FakeResponse(_payload("fixtures.valid.json")),
        FakeResponse(_payload("fixture_details.valid.json")),
    ]

    snapshot = DabbleAdapter(session=session).get_snapshot(
        NBAMarketQuery(), _context()
    )

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.markets
    assert snapshot.coverage.fanout_complete is False
    assert "missing_competition_id" in snapshot.coverage.warning_codes
    events = telemetry.get_recorded_provider_events()
    assert [(event["operation"], event["outcome"]) for event in events] == [
        ("competition_lookup", telemetry.OUTCOME_SUCCESS),
        ("competition_fixtures", telemetry.OUTCOME_SUCCESS),
        ("fixture_details", telemetry.OUTCOME_SUCCESS),
        ("snapshot_normalization", telemetry.OUTCOME_SUCCESS),
    ]
    assert telemetry.snapshot_metrics()["provider_failures"] == {}

    board_session = Mock()
    board_session.get.side_effect = [
        FakeResponse(competitions),
        FakeResponse(_payload("fixtures.valid.json")),
        FakeResponse(_payload("fixture_details.valid.json")),
    ]
    board = DFSBoardService(
        provider_registry={"dabble": DabbleAdapter(session=board_session)}
    ).get_board(NBAMarketQuery(), _context())
    assert board.provider_outcomes[0].reason is ProviderFailureReason.MALFORMED_RESPONSE
    assert telemetry.get_recorded_board_events()[-1]["failure_malformed_response"] == 1


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


def test_all_malformed_props_record_one_sanitized_normalization_failure():
    detail = _payload("fixture_details.valid.json")
    for prop in detail["sportFixtureDetail"]["playerProps"]:
        prop["value"] = "secret-token"
    session = Mock()
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(_payload("fixtures.valid.json")),
        FakeResponse(detail),
    ]
    with pytest.raises(ProviderUnavailableError) as raised:
        DabbleAdapter(session=session).get_snapshot(
            NBAMarketQuery(), _context(deadline=DEADLINE)
        )

    events = telemetry.get_recorded_provider_events()
    assert [(event["operation"], event["outcome"]) for event in events] == [
        ("competition_lookup", telemetry.OUTCOME_SUCCESS),
        ("competition_fixtures", telemetry.OUTCOME_SUCCESS),
        ("fixture_details", telemetry.OUTCOME_SUCCESS),
        ("snapshot_normalization", telemetry.OUTCOME_MALFORMED),
    ]
    assert {event["request_id"] for event in events} == {"dabble-test"}
    assert "secret-token" not in json.dumps(events)
    assert "secret-token" not in str(raised.value.detail)

    metrics = telemetry.snapshot_metrics()
    assert metrics["provider_failures"] == {
        telemetry.PROVIDER_DABBLE: {telemetry.OUTCOME_MALFORMED: 1}
    }
    assert metrics["application_failures"] == {}


def test_all_malformed_competitions_record_one_local_normalization_failure():
    competitions = _payload("competitions.valid.json")
    competitions["data"] = [{"name": "NBA"}, {"name": "NBA"}]
    session = Mock()
    session.get.return_value = FakeResponse(competitions)

    with pytest.raises(ProviderUnavailableError) as raised:
        DabbleAdapter(session=session).get_snapshot(NBAMarketQuery(), _context())

    assert raised.value.code == "provider_unavailable"
    assert raised.value.public_message == "Dabble snapshot is currently unavailable."
    events = telemetry.get_recorded_provider_events()
    assert [(event["operation"], event["outcome"]) for event in events] == [
        ("competition_lookup", telemetry.OUTCOME_SUCCESS),
        ("snapshot_normalization", telemetry.OUTCOME_MALFORMED),
    ]
    assert telemetry.snapshot_metrics()["provider_failures"] == {
        telemetry.PROVIDER_DABBLE: {telemetry.OUTCOME_MALFORMED: 1}
    }


def test_all_malformed_fixtures_record_one_local_normalization_failure():
    competitions = _payload("competitions.valid.json")
    fixtures = _payload("fixtures.valid.json")
    fixtures["data"] = [{"status": "Open"}, {"status": "Open"}]
    session = Mock()
    session.get.side_effect = [
        FakeResponse(competitions),
        FakeResponse(fixtures),
    ]

    with pytest.raises(ProviderUnavailableError):
        DabbleAdapter(session=session).get_snapshot(NBAMarketQuery(), _context())

    events = telemetry.get_recorded_provider_events()
    assert [(event["operation"], event["outcome"]) for event in events] == [
        ("competition_lookup", telemetry.OUTCOME_SUCCESS),
        ("competition_fixtures", telemetry.OUTCOME_SUCCESS),
        ("snapshot_normalization", telemetry.OUTCOME_MALFORMED),
    ]
    assert telemetry.snapshot_metrics()["provider_failures"] == {
        telemetry.PROVIDER_DABBLE: {telemetry.OUTCOME_MALFORMED: 1}
    }


def test_all_malformed_detail_payload_is_not_counted_again_at_normalization():
    detail = {"sportFixtureDetail": {"playerProps": "not-a-list"}}
    session = Mock()
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(_payload("fixtures.valid.json")),
        FakeResponse(detail),
    ]

    with pytest.raises(ProviderUnavailableError) as raised:
        DabbleAdapter(session=session).get_snapshot(NBAMarketQuery(), _context())

    assert raised.value.code == "provider_unavailable"
    assert raised.value.public_message == "Dabble snapshot is currently unavailable."
    events = telemetry.get_recorded_provider_events()
    assert [(event["operation"], event["outcome"]) for event in events] == [
        ("competition_lookup", telemetry.OUTCOME_SUCCESS),
        ("competition_fixtures", telemetry.OUTCOME_SUCCESS),
        ("fixture_details", telemetry.OUTCOME_MALFORMED),
    ]
    assert telemetry.snapshot_metrics()["provider_failures"] == {
        telemetry.PROVIDER_DABBLE: {telemetry.OUTCOME_MALFORMED: 1}
    }


def test_local_detail_id_conflict_records_one_normalization_failure():
    detail = _payload("fixture_details.valid.json")
    detail["sportFixtureDetail"]["id"] = "different-fixture"
    session = Mock()
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(_payload("fixtures.valid.json")),
        FakeResponse(detail),
    ]

    with pytest.raises(ProviderUnavailableError) as raised:
        DabbleAdapter(session=session).get_snapshot(NBAMarketQuery(), _context())

    assert raised.value.code == "provider_unavailable"
    events = telemetry.get_recorded_provider_events()
    assert [(event["operation"], event["outcome"]) for event in events] == [
        ("competition_lookup", telemetry.OUTCOME_SUCCESS),
        ("competition_fixtures", telemetry.OUTCOME_SUCCESS),
        ("fixture_details", telemetry.OUTCOME_SUCCESS),
        ("snapshot_normalization", telemetry.OUTCOME_MALFORMED),
    ]
    assert telemetry.snapshot_metrics()["provider_failures"] == {
        telemetry.PROVIDER_DABBLE: {telemetry.OUTCOME_MALFORMED: 1}
    }


def test_partial_snapshot_does_not_emit_total_failure_normalization_event():
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
        return FakeResponse({"sportFixtureDetail": {"playerProps": "not-a-list"}})

    session.get.side_effect = get

    snapshot = DabbleAdapter(session=session).get_snapshot(NBAMarketQuery(), _context())

    assert snapshot.status is SnapshotStatus.PARTIAL
    events = telemetry.get_recorded_provider_events()
    recorded = [(event["operation"], event["outcome"]) for event in events]
    # The two fixture detail fetches run concurrently, so which one records
    # first is not determined. Everything around them is sequential, so only
    # that pair is compared without order.
    assert recorded[:2] == [
        ("competition_lookup", telemetry.OUTCOME_SUCCESS),
        ("competition_fixtures", telemetry.OUTCOME_SUCCESS),
    ]
    assert sorted(recorded[2:4]) == sorted(
        [
            ("fixture_details", telemetry.OUTCOME_SUCCESS),
            ("fixture_details", telemetry.OUTCOME_MALFORMED),
        ]
    )
    assert recorded[4:] == [("snapshot_normalization", telemetry.OUTCOME_SUCCESS)]
    assert telemetry.snapshot_metrics()["provider_failures"] == {
        telemetry.PROVIDER_DABBLE: {telemetry.OUTCOME_MALFORMED: 1}
    }


def test_dabble_does_not_hide_implementation_value_errors(monkeypatch):
    session = Mock()
    session.get.side_effect = [
        FakeResponse(_payload("competitions.valid.json")),
        FakeResponse(_payload("fixtures.valid.json")),
        FakeResponse(_payload("fixture_details.valid.json")),
    ]
    adapter = DabbleAdapter(session=session)

    def broken_normalizer(*args, **kwargs):
        del args, kwargs
        raise ValueError("adapter bug")

    monkeypatch.setattr(adapter, "_normalize_prop", broken_normalizer)

    with pytest.raises(ValueError, match="adapter bug"):
        adapter.get_snapshot(NBAMarketQuery(), _context())


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


def test_detail_response_after_absolute_deadline_is_not_accepted():
    now = datetime(2026, 8, 9, 17, tzinfo=timezone.utc)
    deadline = datetime(2026, 8, 9, 17, 0, 5, tzinfo=timezone.utc)
    detail_may_return = threading.Event()
    fixture_fanout_started = False
    detail_returned = False
    main_detail_checks = 0
    session = Mock()

    def get(url, **kwargs):
        nonlocal detail_returned, fixture_fanout_started
        del kwargs
        if url.endswith("/competitions"):
            return FakeResponse(_payload("competitions.valid.json"))
        if url.endswith("sport-fixtures"):
            fixture_fanout_started = True
            return FakeResponse(_payload("fixtures.valid.json"))
        detail_may_return.wait()
        detail_returned = True
        return FakeResponse(_payload("fixture_details.valid.json"))

    session.get.side_effect = get

    def clock():
        nonlocal main_detail_checks
        if fixture_fanout_started and threading.current_thread() is threading.main_thread():
            main_detail_checks += 1
            if main_detail_checks == 2:
                detail_may_return.set()
        return deadline if detail_returned else now

    with pytest.raises(ProviderUnavailableError):
        DabbleAdapter(
            session=session,
            detail_concurrency=1,
            now=clock,
        ).get_snapshot(
            NBAMarketQuery(),
            _context(deadline=deadline.isoformat().replace("+00:00", "Z")),
        )

    assert session.get.call_count == 3


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
    assert snapshot.coverage.fetched_count == 4
    assert snapshot.coverage.eligible_count == 3
    assert snapshot.coverage.normalized_count == 3
    assert snapshot.coverage.skipped_count == 1


def test_serialized_lease_uses_injected_monotonic_domain() -> None:
    class FakeClock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = FakeClock()
    session = Mock()
    serialized = _SerializedSession(session, monotonic=clock)
    lease = serialized.acquire_request(deadline=1.0)
    clock.value = 2.0

    with pytest.raises(DeadlineExceededError):
        lease.get("https://example.test/late")
    session.get.assert_not_called()


def test_serialized_lease_accepts_a_long_absolute_deadline() -> None:
    """Long-lived fixture contexts must not overflow platform lock timers."""

    session = Mock()
    serialized = _SerializedSession(session)
    lease = serialized.acquire_request(deadline=time.monotonic() + 10_000_000_000)

    lease.release()


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
