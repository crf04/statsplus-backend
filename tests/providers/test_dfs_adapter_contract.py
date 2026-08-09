"""Shared offline compliance checks for every DFS provider adapter."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Callable

import pytest

from app.providers.dabble import DabbleAdapter
from app.providers.dfs import (
    NBAMarketQuery,
    ProviderSnapshot,
    ProviderSnapshotProvider,
    RetrievalContext,
    SnapshotStatus,
)
from app.providers.prizepicks import PrizePicksAdapter
from app.providers.underdog import UnderdogAdapter


FIXTURES = Path(__file__).parents[1] / "fixtures"


class RecordedResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class RecordedSession:
    def __init__(self, *payloads: object) -> None:
        self._responses = [RecordedResponse(payload) for payload in payloads]
        self._lock = Lock()
        self.headers: dict[str, str] = {}

    def get(self, url: str, **kwargs: object) -> RecordedResponse:
        del url, kwargs
        with self._lock:
            return self._responses.pop(0)


def _load(provider: str, name: str) -> object:
    return json.loads(
        (FIXTURES / provider / name).read_text(encoding="utf-8")
    )


def _dabble() -> DabbleAdapter:
    return DabbleAdapter(
        session=RecordedSession(
            _load("dabble", "competitions.valid.json"),
            _load("dabble", "fixtures.valid.json"),
            _load("dabble", "fixture_details.valid.json"),
        )
    )


def _prizepicks() -> PrizePicksAdapter:
    return PrizePicksAdapter(
        session=RecordedSession(
            _load("prizepicks", "projections.page1.valid.json"),
            _load("prizepicks", "projections.page2.valid.json"),
        )
    )


def _underdog() -> UnderdogAdapter:
    return UnderdogAdapter(
        session=RecordedSession(
            _load("underdog", "over_under_lines.valid.json"),
        )
    )


@pytest.mark.parametrize(
    ("provider", "adapter_factory"),
    [
        ("dabble", _dabble),
        ("prizepicks", _prizepicks),
        ("underdog", _underdog),
    ],
)
def test_recorded_adapter_satisfies_shared_snapshot_contract(
    provider: str,
    adapter_factory: Callable[[], ProviderSnapshotProvider],
) -> None:
    adapter = adapter_factory()
    context = RetrievalContext(deadline=datetime(2030, 1, 1, tzinfo=timezone.utc))

    assert isinstance(adapter, ProviderSnapshotProvider)

    snapshot = adapter.get_snapshot(NBAMarketQuery(), context)

    assert isinstance(snapshot, ProviderSnapshot)
    assert snapshot.provider == provider
    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.coverage.is_complete
    assert snapshot.coverage.normalized_count >= len(snapshot.markets)
    assert snapshot.retrieved_at.tzinfo is timezone.utc
    assert snapshot.markets
    assert all(market.provider == provider for market in snapshot.markets)

    with pytest.raises(FrozenInstanceError):
        snapshot.markets = ()  # type: ignore[misc]
