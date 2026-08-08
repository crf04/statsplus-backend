"""Data refresh integration tests using an injected PBP provider."""

from __future__ import annotations

from unittest.mock import Mock

import pandas as pd
import pytest

from app.config.settings import load_settings
from app.errors import ProviderUnavailableError
from app.services.data_service import DataService


def _settings():
    return load_settings(
        environ={"FLASK_ENV": "testing", "FIREBASE_ADMIN_DISABLED": "true"}
    )


def test_pbp_refresh_crosses_injected_provider_interface(monkeypatch) -> None:
    provider = Mock()
    provider.get_totals.return_value = pd.DataFrame(
        [{"Name": "Test Player", "Points": 12}]
    )
    writes: list[tuple[str, str]] = []

    def fake_to_sql(self, table_name, engine, if_exists, index):
        writes.append((table_name, if_exists))

    monkeypatch.setattr(pd.DataFrame, "to_sql", fake_to_sql)
    service = DataService(Mock(), settings=_settings(), pbp_provider=provider)

    assert service.fetch_PBP_data("opponent") is True

    provider.get_totals.assert_called_once_with("opponent")
    assert writes == [("pbp_opponent_stats", "replace")]


def test_pbp_refresh_preserves_provider_unavailable_error(monkeypatch) -> None:
    provider = Mock()
    provider.get_totals.side_effect = ProviderUnavailableError(
        "PBP Stats could not be reached.", detail="offline"
    )
    service = DataService(Mock(), settings=_settings(), pbp_provider=provider)

    with pytest.raises(ProviderUnavailableError) as raised:
        service.fetch_PBP_data()

    assert raised.value.code == "provider_unavailable"
