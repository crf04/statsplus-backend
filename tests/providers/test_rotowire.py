"""Recorded, offline contract tests for the gated RotoWire adapter."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

from app.config.settings import ProviderSettings, RuntimeSettings
from app.providers.rotowire import RotoWireInjuryProvider
from app.utils import telemetry


FIXTURE = Path(__file__).parents[1] / "fixtures" / "rotowire" / "injury_report.valid.json"
NOW = datetime(2026, 1, 15, 23, 55, tzinfo=timezone.utc)


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.headers = {}

    def get(self, url, *, params, timeout):
        self.calls.append((url, params, timeout))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def test_recorded_snapshot_preserves_raw_evidence_and_strict_statuses():
    payload = json.loads(FIXTURE.read_text())
    session = FakeSession(FakeResponse(payload))

    snapshot = RotoWireInjuryProvider(
        session=session,
        settings=RuntimeSettings(
            environment="testing",
            providers=ProviderSettings(
                rotowire_connect_timeout_seconds=1.25,
                rotowire_read_timeout_seconds=4.5,
            ),
        ),
        clock=lambda: NOW,
    ).get_snapshot()

    assert snapshot.retrieved_at == NOW
    assert snapshot.raw_payload == payload
    assert [entry.entry_id for entry in snapshot.entries] == [
        "rotowire:6504",
        "rotowire:9999",
    ]
    assert snapshot.entries[0].canonical_status == "Questionable"
    assert snapshot.entries[0].source_url == (
        "https://www.rotowire.com/basketball/player/lebron-james-2344"
    )
    assert snapshot.entries[1].canonical_status is None
    assert snapshot.entries[1].raw_status == "Game Time Decision"
    assert all(entry.raw_status != "Available" for entry in snapshot.entries)
    assert session.calls == [
        (
            RotoWireInjuryProvider.ENDPOINT_URL,
            {"team": "ALL", "pos": "ALL"},
            (1.25, 4.5),
        )
    ]

    event = telemetry.get_recorded_provider_events()[-1]
    assert event["provider"] == "rotowire"
    assert event["operation"] == "get_injuries"
    assert event["outcome"] == "success"


def test_transport_failure_is_a_sanitized_provider_failure():
    session = FakeSession(requests.ReadTimeout("secret upstream detail"))

    try:
        RotoWireInjuryProvider(session=session, clock=lambda: NOW).get_snapshot()
    except Exception as error:
        assert error.__class__.__name__ == "ProviderUnavailableError"
        assert "secret upstream detail" not in str(error)
    else:
        raise AssertionError("provider failure was not translated")

    event = telemetry.get_recorded_provider_events()[-1]
    assert event["provider"] == "rotowire"
    assert event["outcome"] == "timeout"
