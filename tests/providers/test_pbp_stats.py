"""Offline contract tests for the PBP Stats adapter."""

from __future__ import annotations

from unittest.mock import Mock

import pandas as pd
import pytest
import requests

from app.config.settings import load_settings
from app.errors import ProviderUnavailableError
from app.providers.pbp_stats import PBPStatsAdapter


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


def _adapter(session: Mock) -> PBPStatsAdapter:
    settings = load_settings(
        environ={
            "FLASK_ENV": "testing",
            "FIREBASE_ADMIN_DISABLED": "true",
            "NBA_API_TIMEOUT_CONNECT": "1.5",
            "NBA_API_TIMEOUT_READ": "4.0",
            "NBA_STATS_TIMEOUT_SECONDS": "8.0",
        }
    )
    return PBPStatsAdapter(settings, session=session)


def test_get_totals_uses_shared_session_contract_and_normalizes_rows() -> None:
    session = Mock()
    session.get.return_value = FakeResponse(
        {"multi_row_table_data": [{"Name": "Test Player", "Points": 24}]}
    )
    adapter = _adapter(session)

    totals = adapter.get_totals("opponent")

    assert isinstance(totals, pd.DataFrame)
    assert totals.to_dict(orient="records") == [{"Name": "Test Player", "Points": 24}]
    session.get.assert_called_once_with(
        PBPStatsAdapter.BASE_URL,
        params={
            "Season": adapter.settings.nba.current_season,
            "SeasonType": "Regular Season",
            "Type": "Opponent",
        },
        timeout=(1.5, 4.0),
    )


def test_health_check_uses_totals_payload_and_names_provider() -> None:
    session = Mock()
    session.get.return_value = FakeResponse(
        {"multi_row_table_data": [{"Name": "Test Player"}]}
    )

    result = _adapter(session).health_check()

    assert result["status"] == "healthy"
    assert result["provider"] == "PBP Stats"
    assert result["endpoint"] == PBPStatsAdapter.BASE_URL
    assert result["test_type"] == "totals"


def test_timeout_becomes_provider_unavailable_error() -> None:
    session = Mock()
    session.get.side_effect = requests.exceptions.ReadTimeout("timed out")

    with pytest.raises(ProviderUnavailableError) as raised:
        _adapter(session).get_totals()

    assert raised.value.code == "provider_unavailable"
    assert raised.value.public_message == "PBP Stats timed out while fetching totals."


def test_malformed_response_becomes_provider_unavailable_error() -> None:
    session = Mock()
    session.get.return_value = FakeResponse({"unexpected": []})

    with pytest.raises(ProviderUnavailableError) as raised:
        _adapter(session).get_totals()

    assert raised.value.code == "provider_unavailable"
    assert raised.value.public_message == "PBP Stats returned an invalid response."


def test_unavailable_http_response_becomes_provider_unavailable_error() -> None:
    session = Mock()
    session.get.return_value = FakeResponse({"error": "upstream"}, status_code=503)

    with pytest.raises(ProviderUnavailableError) as raised:
        _adapter(session).get_totals()

    assert raised.value.code == "provider_unavailable"
    assert raised.value.public_message == "PBP Stats could not be reached."
