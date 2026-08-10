"""Offline tests for the PBP Stats adapter seam (#12).

The adapter wraps the shared, retrying requests session, so timeouts, HTTP
errors, invalid JSON, and malformed rows each become the correct structured
provider outcome, and the thread-safe retry counter is captured in the event.
"""

from __future__ import annotations

import pandas as pd
import pytest
import requests

from app.config.settings import ProviderSettings, RuntimeSettings
from app.services.pbp_stats_adapter import PBPTotalsAdapter
from app.utils import telemetry

VALID_PAYLOAD = {
    "multi_row_table_data": [
        {
            "Name": "LeBron James",
            "Assists": 20,
            "AssistPoints": 40,
            "TwoPtAssists": 10,
            "ThreePtAssists": 10,
            "Arc3Assists": 4,
            "Corner3Assists": 3,
            "AtRimAssists": 8,
            "ShortMidRangeAssists": 3,
            "LongMidRangeAssists": 2,
        },
        {
            "Name": "Stephen Curry",
            "Assists": 18,
            "AssistPoints": 36,
            "TwoPtAssists": 6,
            "ThreePtAssists": 12,
            "Arc3Assists": 6,
            "Corner3Assists": 2,
            "AtRimAssists": 5,
            "ShortMidRangeAssists": 3,
            "LongMidRangeAssists": 2,
        },
    ]
}


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        environment="testing",
        providers=ProviderSettings(
            pbp_connect_timeout_seconds=1.0,
            pbp_read_timeout_seconds=2.0,
        ),
    )


class FakeResponse:
    def __init__(self, payload=None, status_code=200, json_error=None, http_error=None):
        self._payload = payload
        self.status_code = status_code
        self._json_error = json_error
        self._http_error = http_error

    def raise_for_status(self):
        if self._http_error:
            raise self._http_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


@pytest.fixture(autouse=True)
def _clean_events():
    telemetry.clear_recorded_provider_events()
    yield
    telemetry.clear_recorded_provider_events()


def _adapter(session, settings=None):
    return PBPTotalsAdapter(settings=settings or _settings(), session=session)


def test_fetch_totals_records_full_event_and_valid_frame(monkeypatch):
    fake_session = requests.Session()
    fake_session.get = lambda *a, **k: FakeResponse(payload=VALID_PAYLOAD, status_code=200)

    adapter = _adapter(fake_session)
    frame = adapter.fetch_totals_frame("player")

    assert isinstance(frame, pd.DataFrame)
    assert list(frame.columns) == [
        "Name",
        "Assists",
        "AssistPoints",
        "TwoPtAssists",
        "ThreePtAssists",
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    ]
    assert len(frame) == 2

    assert len(telemetry.get_recorded_provider_events()) == 1
    event = telemetry.get_recorded_provider_events()[0]
    assert event["provider"] == telemetry.PROVIDER_PBP_STATS
    assert event["operation"] == "get_totals_player"
    assert event["outcome"] == telemetry.OUTCOME_SUCCESS
    assert event["cache_status"] == telemetry.CACHE_DISABLED
    assert event["status_code"] == 200
    assert event["retry_count"] == 0


def test_fetch_totals_opponent_uses_opponent_operation():
    fake_session = requests.Session()
    fake_session.get = lambda *a, **k: FakeResponse(payload=VALID_PAYLOAD)

    adapter = _adapter(fake_session)
    adapter.fetch_totals_frame("opponent")

    event = telemetry.get_recorded_provider_events()[-1]
    assert event["operation"] == "get_totals_opponent"


def test_fetch_totals_opponent_supports_exact_team_date_bounds():
    fake_session = requests.Session()
    calls = []

    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse(payload=VALID_PAYLOAD)

    fake_session.get = get
    adapter = _adapter(fake_session)

    adapter.fetch_totals_frame(
        "opponent",
        season="2024-25",
        team_id=1610612738,
        from_date="2025-03-01",
        to_date="2025-04-15",
    )

    assert calls == [
        (
            (adapter.base_url,),
            {
                "params": {
                    "Season": "2024-25",
                    "SeasonType": "Regular+Season",
                    "Type": "Opponent",
                    "TeamId": "1610612738",
                    "FromDate": "2025-03-01",
                    "ToDate": "2025-04-15",
                },
                "timeout": (1.0, 2.0),
            },
        )
    ]


def test_fetch_totals_rejects_unsupported_data_type():
    from app.errors import InvalidInputError

    fake_session = requests.Session()

    with pytest.raises(InvalidInputError):
        _adapter(fake_session).fetch_totals_frame("everything")

    assert telemetry.get_recorded_provider_events() == []


def test_timeout_is_recorded_and_raised(monkeypatch):
    def timeout(*a, **k):
        raise requests.exceptions.ReadTimeout("pbp timed out")

    fake_session = requests.Session()
    fake_session.get = timeout

    with pytest.raises(requests.exceptions.ReadTimeout):
        _adapter(fake_session).fetch_totals_frame()

    event = telemetry.get_recorded_provider_events()[-1]
    assert event["outcome"] == telemetry.OUTCOME_TIMEOUT


def test_http_error_is_recorded_with_status(monkeypatch):
    http_error = requests.exceptions.HTTPError("502 Bad Gateway")

    fake_session = requests.Session()
    fake_session.get = lambda *a, **k: FakeResponse(
        payload={}, status_code=502, http_error=http_error
    )

    with pytest.raises(requests.exceptions.HTTPError):
        _adapter(fake_session).fetch_totals_frame()

    event = telemetry.get_recorded_provider_events()[-1]
    assert event["outcome"] == telemetry.OUTCOME_HTTP_ERROR
    assert event["status_code"] == 502


def test_invalid_json_is_recorded_as_malformed():
    import json

    fake_session = requests.Session()
    fake_session.get = lambda *a, **k: FakeResponse(
        payload=None, json_error=json.JSONDecodeError("bad", "doc", 0)
    )

    with pytest.raises(telemetry.ProviderResponseError):
        _adapter(fake_session).fetch_totals_frame()

    event = telemetry.get_recorded_provider_events()[-1]
    assert event["outcome"] == telemetry.OUTCOME_MALFORMED


def test_malformed_shape_is_recorded_as_malformed():
    fake_session = requests.Session()
    fake_session.get = lambda *a, **k: FakeResponse(payload={"message": "error"})

    with pytest.raises(telemetry.ProviderResponseError):
        _adapter(fake_session).fetch_totals_frame()

    event = telemetry.get_recorded_provider_events()[-1]
    assert event["outcome"] == telemetry.OUTCOME_MALFORMED


def test_retries_within_session_are_counted_in_the_event():
    def retried_timeout(*a, **k):
        telemetry.increment_retry_count()
        raise requests.exceptions.ReadTimeout("timing out")

    fake_session = requests.Session()
    fake_session.get = retried_timeout

    with pytest.raises(requests.exceptions.ReadTimeout):
        _adapter(fake_session).fetch_totals_frame()

    event = telemetry.get_recorded_provider_events()[-1]
    assert event["retry_count"] == 1
    assert event["outcome"] == telemetry.OUTCOME_TIMEOUT


def test_shared_session_is_configured_with_retrying_adapter():
    from app.utils.nba_api_config import get_shared_nba_session

    session = get_shared_nba_session(_settings())
    adapter = session.get_adapter("https://api.pbpstats.com")

    assert adapter.max_retries.total >= 1
    assert type(adapter.max_retries).__name__ == "RetryWithLogging"


def test_parse_totals_valid_and_malformed():
    frame = PBPTotalsAdapter.parse_totals(VALID_PAYLOAD)
    assert len(frame) == 2

    with pytest.raises(telemetry.ProviderResponseError):
        PBPTotalsAdapter.parse_totals({"multi_row_table_data": "nope"})

    with pytest.raises(telemetry.ProviderResponseError):
        PBPTotalsAdapter.parse_totals([{"not": "a-row"}, "string"])

    with pytest.raises(telemetry.ProviderResponseError):
        PBPTotalsAdapter.parse_totals(None)
