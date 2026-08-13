"""Offline seams for the standalone Residential Collector package."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from app.collector.client import CollectorToken, HTTPResponse, RailwayClient
from app.collector.cache import InstructionCache
from app.collector.config import CollectorConfig, CollectorConfigurationError, load_collector_config
from app.collector.contracts import ProviderContractError
from app.collector.normalizers import (
    normalize_grouped_shot_response,
    normalize_roster_response,
    normalize_schedule_response,
    normalize_synergy_response,
    normalize_zone_response,
)
from app.collector.outbox import OutboxBusy, OutboxFull, OutboxRepository
from app.collector.runner import (
    EXIT_NO_WORK,
    EXIT_NON_RETRYABLE,
    EXIT_RETRY,
    RunDisposition,
    ResidentialCollector,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)


def _schedule():
    return [{
        "game_id": "g-1", "home_team_id": 1610612737, "away_team_id": 1610612738,
        "scheduled_at": "2026-04-10T00:00:00Z", "status": "Final",
        "classification": "Regular Season",
    }]


def _roster():
    return [{"player_id": 1, "display_name": "One", "team_id": 1610612737, "season": "2025-26", "roster_status": "active"}]


def _stats(category="Transition"):
    return [{
        "player_id": 1, "category": category, "GP": 1, "POSS": 2,
        "PTS": 3,
    }]


def _zones():
    return [{
        "player_id": 1, "Restricted Area": 1, "In The Paint (Non-RA)": 2,
        "Mid-Range": 3, "Corner 3": 4, "Above the Break 3": 5,
    }]


def _wire_checksum(marker: str) -> str:
    payload = {"records": [marker]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _wire(client_id: str, marker: str) -> bytes:
    payload = {"records": [marker]}
    checksum = _wire_checksum(marker)
    document = {
        "manifest_id": "manifest", "client_observation_id": client_id,
        "environment": "testing", "provider": "nba", "observation_type": "scope",
        "scope": "season", "season": "2025-26", "cutoff": NOW.isoformat(),
        "schema_version": 2, "retrieved_at": NOW.isoformat(), "checksum": checksum,
        "payload": payload,
    }
    return gzip.compress(json.dumps(document, sort_keys=True, separators=(",", ":")).encode(), mtime=0)


def test_normalizers_reject_cross_phase_duplicate_and_negative_values():
    with pytest.raises(ProviderContractError, match="cross_phase"):
        normalize_schedule_response(
            [{**_schedule()[0], "classification": "Playoffs"}], season="2025-26", cutoff=NOW
        )
    with pytest.raises(ProviderContractError, match="duplicate_identity"):
        normalize_schedule_response(_schedule() * 2, season="2025-26", cutoff=NOW)
    with pytest.raises(ProviderContractError, match="value_invariant"):
        normalize_grouped_shot_response(
            [{"player_id": 1, "category": "Catch and Shoot", "FGA": -1}],
            season="2025-26", cutoff=NOW,
        )


def test_normalizers_preserve_provider_categories_and_scope_evidence():
    synergy = normalize_synergy_response(_stats(), season="2025-26", cutoff=NOW)
    assert synergy.observation_type == "synergy_play_types"
    assert synergy.payload["records"][0]["category"] == "Transition"
    assert synergy.scope["phase"] == "Regular Season"
    zones = normalize_zone_response(_zones(), season="2025-26", cutoff=NOW)
    assert zones.payload["coverage"]["zones"] == [
        "Restricted Area", "In The Paint (Non-RA)", "Mid-Range", "Corner 3", "Above the Break 3"
    ]


def test_schedule_roster_require_identity_and_exact_season():
    schedule = normalize_schedule_response(_schedule(), season="2025-26", cutoff=NOW)
    assert schedule.payload["coverage"]["game_count"] == 1
    roster = normalize_roster_response(_roster(), season="2025-26", cutoff=NOW)
    assert roster.payload["records"][0]["player_id"] == 1
    with pytest.raises(ProviderContractError, match="manifest_scope"):
        normalize_roster_response(_roster(), season="2024-25", cutoff=NOW)


def test_sanitized_recorded_nba_json_is_normalized_without_network():
    fixture_root = Path(__file__).parent / "fixtures"
    schedule = json.loads((fixture_root / "nba_stats" / "schedule.valid.json").read_text())
    roster = json.loads((fixture_root / "nba_stats_player_roster.json").read_text())
    synergy = json.loads((fixture_root / "player_diets" / "synergy_isolation.json").read_text())
    shots = json.loads((fixture_root / "player_diets" / "shot_type_catch_and_shoot.json").read_text())
    zones = json.loads((fixture_root / "player_diets" / "shot_zones.json").read_text())
    assert normalize_schedule_response(schedule, season="2025-26", cutoff=NOW).payload["records"]
    assert normalize_roster_response(roster, season="2024-25", cutoff=NOW).payload["records"]
    assert normalize_synergy_response(synergy, season="2025-26", cutoff=NOW).payload["records"]
    assert normalize_grouped_shot_response(
        shots, season="2025-26", cutoff=NOW,
        scope={"window": "season", "subject": "player", "category": "Catch and Shoot"},
    ).payload["records"]
    assert normalize_zone_response(zones, season="2025-26", cutoff=NOW).payload["records"]


def test_outbox_is_newest_cutoff_first_and_receipt_gated(tmp_path: Path):
    outbox = OutboxRepository(tmp_path / "outbox.sqlite3", max_bytes=128 * 1024, max_item_bytes=2048, clock=lambda: NOW)
    older_checksum = _wire_checksum("a")
    newer_checksum = _wire_checksum("b")
    older = outbox.enqueue(kind="observation", client_observation_id="old", checksum=older_checksum, cutoff=NOW - timedelta(days=1), payload=_wire("old", "a"), metadata={"observation_type": "x"})
    newer = outbox.enqueue(kind="observation", client_observation_id="new", checksum=newer_checksum, cutoff=NOW, payload=_wire("new", "b"), metadata={"observation_type": "x"})
    assert [item.client_observation_id for item in outbox.pending()] == ["new", "old"]
    with pytest.raises(Exception):
        outbox.acknowledge(newer.item_id, checksum="wrong")
    assert outbox.get(newer.item_id) is not None
    assert outbox.acknowledge(newer.item_id, checksum=newer_checksum)
    assert outbox.get(older.item_id) is not None
    outbox.close()


def test_outbox_hard_limit_preserves_current_work_and_non_overlap(tmp_path: Path):
    first_payload = _wire("one", "a")
    outbox = OutboxRepository(tmp_path / "outbox.sqlite3", max_bytes=20 * 1024, max_item_bytes=2048, clock=lambda: NOW)
    outbox.enqueue(kind="observation", client_observation_id="one", checksum=_wire_checksum("a"), cutoff=NOW, payload=first_payload, metadata={})
    rejected = False
    for index in range(2, 100):
        marker = f"item-{index}"
        try:
            outbox.enqueue(kind="observation", client_observation_id=marker, checksum=_wire_checksum(marker),
                           cutoff=NOW, payload=_wire(marker, marker), metadata={})
        except OutboxFull:
            rejected = True
            break
    assert rejected
    assert outbox.pending()[0].client_observation_id == "one"
    owner = outbox.acquire_lease(owner="first", ttl_seconds=60)
    assert owner == "first"
    with pytest.raises(OutboxBusy):
        outbox.acquire_lease(owner="second", ttl_seconds=60)
    outbox.release_lease(owner)
    assert outbox.durability_pragmas() == {"journal_mode": "wal", "synchronous": 2, "foreign_keys": 1}
    assert outbox.storage_footprint_bytes() >= (tmp_path / "outbox.sqlite3").stat().st_size
    assert outbox.within_hard_limit()
    outbox.close()


def test_scope_descriptors_govern_all_opponent_team_windows_and_cutoff():
    from app.services.collection_control import NBA_TEAM_IDS, _collector_scope_descriptors

    descriptors = _collector_scope_descriptors({"grouped_shot_types", "exact_shot_zones"}, NOW)
    opponent = [item for item in descriptors if item["parameters"].get("subject") == "opponent"]
    assert {str(item["parameters"]["team_id"]) for item in opponent} == NBA_TEAM_IDS
    assert {item["parameters"]["window"] for item in opponent} == {"season", "l15"}
    assert {item["parameters"]["date_to"] for item in opponent} == {NOW.date().isoformat()}
    assert all(item["parameters"]["date_from"] is None for item in opponent)


def test_windows_task_lifecycle_requires_explicit_named_promotion():
    root = Path(__file__).resolve().parents[1] / "scripts"
    install = (root / "install_collector.ps1").read_text(encoding="utf-8")
    upgrade = (root / "upgrade_collector.ps1").read_text(encoding="utf-8")
    rollback = (root / "rollback_collector.ps1").read_text(encoding="utf-8")
    promote = (root / "promote_collector.ps1").read_text(encoding="utf-8")
    assert "Disable-ScheduledTask -TaskName $TaskName" in install
    assert "Disable-ScheduledTask -TaskName $TaskName" in upgrade
    assert "Disable-ScheduledTask -TaskName $TaskName" in rollback
    assert "credential-check" in promote and "validate-config" in promote
    assert "rehearsal --season $Season --cutoff $Cutoff" in promote
    assert "railway-rehearsal --season $Season --cutoff $Cutoff" in promote
    assert "RailwayRehearsalResult" not in promote
    assert "Enable-ScheduledTask -TaskName $TaskName" in promote


def test_cli_rehearsal_command_invokes_compatibility_probes(monkeypatch, capsys):
    from app.collector import cli
    from app.collector.rehearsal import ProbeResult

    calls = []

    class FakeProbes:
        def __init__(self, provider):
            assert provider is not None

        def run(self, *, season, cutoff, opponent_team_id):
            calls.append((season, cutoff, opponent_team_id))
            return (ProbeResult("event_catalog", True, {"season": season}),)

    monkeypatch.setattr(cli, "ResidentialCompatibilityProbes", FakeProbes)
    monkeypatch.setattr(cli, "NBAStatsProviderAdapter", lambda: object())
    monkeypatch.setattr(cli, "NBA_TEAM_IDS", (1610612737, 1610612738))
    monkeypatch.setenv("COLLECTOR_RAILWAY_URL", "https://railway.example")
    monkeypatch.setenv("COLLECTOR_IDENTITY_ID", "collector")
    assert cli.main(["rehearsal", "--season", "2025-26", "--cutoff", NOW.isoformat()]) == 0
    assert [call[2] for call in calls] == [1610612737, 1610612738]
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


def test_real_cli_rehearsal_defaults_to_offline_sanitized_fixtures(monkeypatch, capsys):
    from app.collector import cli

    monkeypatch.setenv("COLLECTOR_RAILWAY_URL", "https://railway.example")
    monkeypatch.setenv("COLLECTOR_IDENTITY_ID", "collector")
    assert cli.main(["rehearsal", "--season", "2025-26", "--cutoff", NOW.isoformat()]) == 0
    evidence = json.loads(capsys.readouterr().out)
    assert evidence["mode"] == "offline"
    assert evidence["teams"] == 30
    assert evidence["failed_scopes"] == []


def test_fake_railway_rehearsal_requires_durable_idempotent_receipt():
    class RehearsalTransport:
        def __init__(self):
            self.receipt = None

        def request(self, method, url, *, headers=None, body=None, json_body=None, timeout=30):
            if url.endswith("/api/collector/token"):
                return HTTPResponse(201, {"token": "token", "expires_in": 300})
            if url.endswith("/api/collector/rehearsal-manifest"):
                return HTTPResponse(200, {"manifest_id": "validation-manifest"})
            if url.endswith("/api/collector/observations"):
                document = json.loads(gzip.decompress(body))
                if self.receipt is None:
                    self.receipt = {"observation_id": "durable-1", "client_observation_id": document["client_observation_id"],
                                    "checksum": document["checksum"], "replay": False}
                    return HTTPResponse(202, self.receipt)
                return HTTPResponse(202, {**self.receipt, "replay": True})
            if url.endswith("/api/collector/rehearsal-evidence"):
                assert json_body["observation_id"] == json_body["replay_observation_id"] == "durable-1"
                return HTTPResponse(200, {"operations": ["credential", "auth", "discovery", "status", "ingestion"],
                                          "replay_verified": True, "observation_id": "durable-1"})
            return HTTPResponse(200, {"environment": "testing", "bootstrap_requests": [], "manifests": []})

    client = RailwayClient("http://127.0.0.1", identity_id="collector", environment="testing",
                           transport=RehearsalTransport(), allow_insecure_localhost=True)
    token = client.exchange_token("machine-secret")
    manifest = client.rehearsal_manifest(token, season="2025-26", cutoff=NOW.isoformat())
    wire = _wire("rehearsal", "sanitized")
    receipt = client.upload_observation(token, wire)
    replay = client.upload_observation(token, wire)
    evidence = client.rehearsal_evidence(
        token, release_version="0.1.0", release_checksum="a" * 64,
        season="2025-26", cutoff=NOW.isoformat(), receipt=receipt, replay=replay,
        manifest_id=manifest["manifest_id"], client_observation_id="rehearsal",
        checksum=receipt["checksum"],
    )
    assert replay["replay"] and evidence["replay_verified"]


def test_long_run_refreshes_short_lived_tokens_between_incremental_uploads(tmp_path: Path):
    clock = [NOW]
    descriptors = [{"scope": "synergy_play_types", "parameters": {
        "window": "season", "subject": "player", "play_type": "Transition",
    }} for _ in range(5)]
    discovery = {"environment": "testing", "bootstrap_requests": [], "manifests": [{
        "manifest_id": "long", "season": "2025-26", "cutoff": NOW.isoformat(),
        "collect_before": (NOW + timedelta(hours=2)).isoformat(), "accepted_versions": [2],
        "scopes": ["synergy_play_types"], "scope_descriptors": descriptors,
    }]}

    class ExpiringTransport(FakeTransport):
        def __init__(self):
            super().__init__(discovery=discovery)
            self.issued = 0
            self.expiries = {}

        def request(self, method, url, **kwargs):
            if url.endswith("/api/collector/token"):
                self.issued += 1
                token = f"token-{self.issued}"
                self.expiries[token] = clock[0] + timedelta(seconds=120)
                self.calls.append((method, url, kwargs.get("headers") or {}, kwargs.get("body"), kwargs.get("json_body")))
                return HTTPResponse(201, {"token": token, "expires_at": self.expiries[token].isoformat()})
            bearer = (kwargs.get("headers") or {}).get("Authorization", "").removeprefix("Bearer ")
            if bearer and clock[0] >= self.expiries[bearer]:
                return HTTPResponse(401, {"error": {"code": "token_expired"}})
            return super().request(method, url, **kwargs)

    class SlowProvider(FakeProvider):
        def fetch_synergy_play_types(self, *args, **kwargs):
            clock[0] += timedelta(seconds=70)
            return super().fetch_synergy_play_types(*args, **kwargs)

    transport = ExpiringTransport()
    collector, _, outbox = _collector(tmp_path, discovery=discovery, transport=transport,
                                      provider=SlowProvider(), now=clock[0])
    collector.clock = lambda: clock[0]
    collector.executor.clock = collector.clock
    result = collector.run()
    assert result.uploaded == 5
    assert transport.issued >= 3
    assert outbox.count() == 0
    outbox.close()


def test_runner_spools_verified_responses_before_later_category_failure(tmp_path: Path):
    class PartialProvider(FakeProvider):
        calls = 0

        def fetch_synergy_play_types(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise ValueError("later schema failure")
            return super().fetch_synergy_play_types(*args, **kwargs)

    discovery = {"environment": "testing", "bootstrap_requests": [], "manifests": [{
        "manifest_id": "m-partial", "season": "2025-26", "cutoff": NOW.isoformat(),
        "collect_before": (NOW + timedelta(hours=1)).isoformat(),
        "accepted_versions": [2], "scopes": ["synergy_play_types"],
    }]}
    collector, _, outbox = _collector(tmp_path, discovery=discovery, provider=PartialProvider())
    result = collector.run()
    assert result.spooled == 1
    assert result.uploaded == 1
    assert result.disposition is RunDisposition.NON_RETRYABLE
    outbox.close()


class FakeTransport:
    def __init__(self, *, discovery=None, upload_status=202):
        self.discovery = discovery or {"environment": "testing", "bootstrap_requests": [], "manifests": []}
        self.upload_status = upload_status
        self.calls = []

    def request(self, method, url, *, headers=None, body=None, json_body=None, timeout=30):
        self.calls.append((method, url, headers or {}, body, json_body))
        if url.endswith("/api/collector/token"):
            return HTTPResponse(201, {"token": "token", "expires_in": 300})
        if "/api/collector/discovery" in url:
            return HTTPResponse(200, self.discovery)
        if url.endswith("/api/collector/status"):
            return HTTPResponse(200, {**dict(json_body or {}), "last_seen_at": NOW.isoformat()})
        if "/api/collector/observations" in url or "/api/collector/catalog/" in url:
            document = json.loads(gzip.decompress(body or b"{}"))
            checksum = hashlib.sha256(json.dumps(document["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            return HTTPResponse(self.upload_status, {"checksum": checksum})
        return HTTPResponse(404, {"error": {"code": "not_found"}})


class FakeProvider:
    def fetch_whole_season_schedule(self, *, season):
        return _schedule()

    def get_player_roster(self, *, season):
        return _roster()

    def fetch_synergy_play_types(self, *args, **kwargs):
        return _stats(args[0] if args else kwargs.get("play_type", "Transition"))


def _collector(tmp_path, *, discovery, transport=None, provider=None, now=NOW, release_checksum=None):
    fake_transport = transport or FakeTransport(discovery=discovery)
    client = RailwayClient("http://127.0.0.1", identity_id="collector", environment="testing", transport=fake_transport, allow_insecure_localhost=True)
    outbox = OutboxRepository(tmp_path / "outbox.sqlite3", clock=lambda: now)
    return ResidentialCollector(
        client=client, outbox=outbox, provider=provider or FakeProvider(),
        identity_id="collector", environment="testing", secret="machine-secret",
        clock=lambda: now,
        instruction_cache=InstructionCache(tmp_path / "instructions.json", clock=lambda: now),
        release_checksum=release_checksum,
    ), fake_transport, outbox


def test_runner_no_work_has_distinct_control_disposition(tmp_path: Path):
    collector, transport, outbox = _collector(tmp_path, discovery={"environment": "testing", "bootstrap_requests": [], "manifests": []})
    result = collector.run()
    assert result.disposition is RunDisposition.NO_WORK
    assert result.exit_code == EXIT_NO_WORK
    assert len(transport.calls) == 2  # token + discovery
    outbox.close()


def test_runner_reports_bounded_start_and_terminal_status(tmp_path: Path):
    collector, transport, outbox = _collector(
        tmp_path, discovery={"environment": "testing", "bootstrap_requests": [], "manifests": []},
        release_checksum="a" * 64,
    )
    assert collector.run().disposition is RunDisposition.NO_WORK
    status_calls = [call for call in transport.calls if call[1].endswith("/api/collector/status")]
    assert [call[4]["state"] for call in status_calls] == ["running", "no_work"]
    assert all(set(call[4]) == {"release_version", "release_checksum", "state", "reason"} for call in status_calls)
    outbox.close()


def test_rejected_status_is_reported_without_skipping_primary_work(tmp_path: Path):
    class RejectedStatus(FakeTransport):
        def request(self, method, url, **kwargs):
            if url.endswith("/api/collector/status"):
                self.calls.append((method, url, kwargs.get("headers") or {}, None, kwargs.get("json_body")))
                return HTTPResponse(400, {"error": {"code": "invalid_release_status"}})
            return super().request(method, url, **kwargs)

    transport = RejectedStatus(discovery={"environment": "testing", "bootstrap_requests": [], "manifests": []})
    collector, _, outbox = _collector(tmp_path, discovery=transport.discovery, transport=transport,
                                      release_checksum="a" * 64)
    result = collector.run()
    assert any("discovery" in call[1] for call in transport.calls)
    assert result.disposition is RunDisposition.NON_RETRYABLE
    assert "control_rejected" in result.failures
    outbox.close()


def test_runner_spools_and_uploads_all_ready_manifest_scopes(tmp_path: Path):
    discovery = {"environment": "testing", "bootstrap_requests": [], "manifests": [{
        "manifest_id": "m-1", "season": "2025-26", "cutoff": NOW.isoformat(),
        "collect_before": (NOW + timedelta(hours=1)).isoformat(),
        "accepted_versions": [1, 2], "scopes": ["synergy:l15"],
    }]}
    collector, transport, outbox = _collector(tmp_path, discovery=discovery)
    result = collector.run()
    assert result.disposition is RunDisposition.COMPLETE
    assert result.attempted_scopes == 0
    assert "synergy:l15" in result.skipped_scopes
    assert result.exit_code == EXIT_NO_WORK
    outbox.close()


def test_runner_bootstrap_catalog_uses_server_null_manifest_field(tmp_path: Path):
    discovery = {"environment": "testing", "bootstrap_requests": [{
        "request_id": "request-1", "catalog_type": "event", "season": "2025-26",
        "cutoff": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "status": "pending",
    }], "manifests": []}
    collector, transport, outbox = _collector(tmp_path, discovery=discovery)
    result = collector.run()
    assert result.disposition is RunDisposition.COMPLETE
    assert result.spooled == 1
    assert result.uploaded == 1
    catalog_calls = [call for call in transport.calls if "/api/collector/catalog/" in call[1]]
    assert len(catalog_calls) == 1
    assert json.loads(gzip.decompress(catalog_calls[0][3]))["manifest_id"] is None
    outbox.close()


def test_runner_spools_before_upload_and_deletes_only_matching_receipts(tmp_path: Path):
    discovery = {"environment": "testing", "bootstrap_requests": [], "manifests": [{
        "manifest_id": "m-2", "season": "2025-26", "cutoff": NOW.isoformat(),
        "collect_before": (NOW + timedelta(hours=1)).isoformat(),
        "accepted_versions": [1, 2], "scopes": ["synergy_play_types"],
    }]}
    collector, transport, outbox = _collector(tmp_path, discovery=discovery)
    result = collector.run()
    assert result.disposition is RunDisposition.COMPLETE
    assert result.spooled == 11
    assert result.uploaded == 11
    assert outbox.count() == 0
    upload_calls = [call for call in transport.calls if "/api/collector/observations" in call[1]]
    assert len(upload_calls) == 11
    assert all(call[2].get("Content-Encoding") == "gzip" for call in upload_calls)
    outbox.close()


def test_runner_provider_schema_failure_is_non_retryable(tmp_path: Path):
    discovery = {"environment": "testing", "bootstrap_requests": [{
        "request_id": "r-1", "catalog_type": "event", "season": "2025-26",
        "cutoff": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=1)).isoformat(),
    }], "manifests": []}

    class BadProvider(FakeProvider):
        def fetch_whole_season_schedule(self, *, season):
            return [{"game_id": "bad"}]

    collector, transport, outbox = _collector(tmp_path, discovery=discovery, provider=BadProvider())
    result = collector.run()
    assert result.disposition is RunDisposition.NON_RETRYABLE
    assert result.exit_code == EXIT_NON_RETRYABLE
    assert result.failures
    outbox.close()


def test_cached_instruction_is_used_before_expiry_during_railway_outage(tmp_path: Path):
    discovery = {"environment": "testing", "bootstrap_requests": [], "manifests": [{
        "manifest_id": "m-cache", "season": "2025-26", "cutoff": NOW.isoformat(),
        "collect_before": (NOW + timedelta(hours=1)).isoformat(),
        "accepted_versions": [1, 2], "scopes": ["synergy:l15"],
    }]}
    cache = InstructionCache(tmp_path / "instructions.json", clock=lambda: NOW)
    cache.store(discovery)

    class Outage(FakeTransport):
        def request(self, *args, **kwargs):
            raise TimeoutError("offline")

    collector, transport, outbox = _collector(tmp_path, discovery={"environment": "testing", "bootstrap_requests": [], "manifests": []}, transport=Outage())
    result = collector.run()
    assert result.disposition is RunDisposition.RETRY
    assert result.attempted_scopes == 0
    outbox.close()


def test_cached_instruction_expiry_is_not_executed(tmp_path: Path):
    cache = InstructionCache(tmp_path / "instructions.json", clock=lambda: NOW)
    cache.store({"environment": "testing", "bootstrap_requests": [], "manifests": [{
        "manifest_id": "expired", "season": "2025-26", "cutoff": NOW.isoformat(),
        "collect_before": (NOW - timedelta(minutes=1)).isoformat(),
        "accepted_versions": [2], "scopes": ["synergy_play_types"],
    }]})
    value = cache.load(now=NOW)
    assert value.manifests == ()


def test_cached_instruction_is_bound_to_its_environment_and_config_uses_exact_local_host(tmp_path: Path):
    cache = InstructionCache(tmp_path / "instructions.json", clock=lambda: NOW)
    cache.store({"environment": "testing", "bootstrap_requests": [], "manifests": []})
    assert cache.load(now=NOW, environment="production").manifests == ()
    assert cache.load(now=NOW, environment="testing").environment == "testing"

    with pytest.raises(CollectorConfigurationError):
        CollectorConfig(
            railway_url="http://collector.example/localhost",
            environment="testing", identity_id="collector",
            outbox_path=tmp_path / "outbox.sqlite3", log_path=tmp_path / "collector.log",
            release_version="test", allow_insecure_localhost=True,
        )
    values = {
        "COLLECTOR_RAILWAY_URL": "https://railway.example",
        "COLLECTOR_IDENTITY_ID": "collector",
    }
    assert load_collector_config(values).identity_id == "collector"


def test_wrong_environment_and_revoked_token_stop_without_provider_calls(tmp_path: Path):
    class WrongEnvironment(FakeTransport):
        def request(self, method, url, **kwargs):
            if "/api/collector/discovery" in url:
                return HTTPResponse(200, {"environment": "production", "bootstrap_requests": [], "manifests": []})
            return super().request(method, url, **kwargs)

    collector, _, outbox = _collector(tmp_path, discovery={"environment": "testing", "bootstrap_requests": [], "manifests": []}, transport=WrongEnvironment())
    result = collector.run()
    assert result.disposition is RunDisposition.NON_RETRYABLE
    assert "environment_mismatch" in result.failures
    outbox.close()

    class Revoked(FakeTransport):
        def request(self, method, url, **kwargs):
            if url.endswith("/api/collector/token"):
                return HTTPResponse(401, {"error": {"code": "invalid_token"}})
            return super().request(method, url, **kwargs)

    collector, _, outbox = _collector(tmp_path / "revoked", discovery={"environment": "testing", "bootstrap_requests": [], "manifests": []}, transport=Revoked())
    result = collector.run()
    assert result.disposition is RunDisposition.NON_RETRYABLE
    assert result.exit_code == EXIT_NON_RETRYABLE
    outbox.close()


def test_provider_timeout_is_retryable_and_expired_token_is_rejected(tmp_path: Path):
    class SlowProvider(FakeProvider):
        def fetch_synergy_play_types(self, *args, **kwargs):
            raise TimeoutError("provider timeout")

    discovery = {"environment": "testing", "bootstrap_requests": [], "manifests": [{
        "manifest_id": "m-timeout", "season": "2025-26", "cutoff": NOW.isoformat(),
        "collect_before": (NOW + timedelta(hours=1)).isoformat(),
        "accepted_versions": [2], "scopes": ["synergy_play_types"],
    }]}
    collector, _, outbox = _collector(tmp_path, discovery=discovery, provider=SlowProvider())
    result = collector.run()
    assert result.disposition is RunDisposition.RETRY
    assert result.exit_code == EXIT_RETRY
    outbox.close()
    token = CollectorToken.from_response({"token": "expired", "expires_at": (NOW - timedelta(seconds=1)).isoformat()}, now=NOW)
    assert token.is_expired


def test_outbox_replay_survives_repository_restart_and_live_lease_is_busy(tmp_path: Path):
    path = tmp_path / "restart.sqlite3"
    first = OutboxRepository(path, clock=lambda: NOW)
    restart_checksum = _wire_checksum("same")
    item = first.enqueue(kind="observation", client_observation_id="restart", checksum=restart_checksum, cutoff=NOW, payload=_wire("restart", "same"), metadata={})
    owner = first.acquire_lease(owner="live", ttl_seconds=60)
    first.close()
    second = OutboxRepository(path, clock=lambda: NOW)
    assert second.pending()[0].client_observation_id == "restart"
    # SQLite persistence means the lease remains a live cross-process fence.
    with pytest.raises(OutboxBusy):
        second.acquire_lease(owner="new", ttl_seconds=60)
    second.release_lease(owner)
    assert second.acknowledge(item.item_id, checksum=restart_checksum)
    assert second.within_hard_limit()
    second.close()


def test_aged_unsent_work_is_preserved_and_does_not_hide_newer_drain(tmp_path: Path):
    path = tmp_path / "retention.sqlite3"
    old_clock = NOW - timedelta(days=31)
    old = OutboxRepository(path, clock=lambda: old_clock)
    old.enqueue(kind="observation", client_observation_id="aged", checksum=_wire_checksum("aged"),
                cutoff=old_clock, payload=_wire("aged", "aged"), metadata={})
    old.close()
    current = OutboxRepository(path, clock=lambda: NOW)
    current.enqueue(kind="observation", client_observation_id="current", checksum=_wire_checksum("current"),
                    cutoff=NOW, payload=_wire("current", "current"), metadata={})
    assert [item.client_observation_id for item in current.aged_pending(now=NOW)] == ["aged"]
    with pytest.raises(Exception, match="older than"):
        current.enforce_retention(now=NOW)
    assert [item.client_observation_id for item in current.pending()] == ["current", "aged"]
    assert current.prune_obsolete(governed_before_cutoff=NOW - timedelta(days=1)) == 1
    assert [item.client_observation_id for item in current.pending()] == ["current"]
    current.close()


def test_runner_skips_aged_item_but_uploads_newest_priority_first(tmp_path: Path):
    path = tmp_path / "priority.sqlite3"
    old = OutboxRepository(path, clock=lambda: NOW - timedelta(days=31))
    old.enqueue(kind="observation", client_observation_id="aged", checksum=_wire_checksum("aged"),
                cutoff=NOW - timedelta(days=31), payload=_wire("aged", "aged"), metadata={})
    old.close()
    transport = FakeTransport(discovery={"environment": "testing", "bootstrap_requests": [], "manifests": []})
    current = OutboxRepository(path, clock=lambda: NOW)
    current.enqueue(kind="observation", client_observation_id="new", checksum=_wire_checksum("new"),
                    cutoff=NOW, payload=_wire("new", "new"), metadata={})
    collector = ResidentialCollector(
        client=RailwayClient("http://127.0.0.1", identity_id="collector", environment="testing",
                             transport=transport, allow_insecure_localhost=True),
        outbox=current, provider=FakeProvider(), identity_id="collector", environment="testing",
        secret="machine-secret", clock=lambda: NOW,
    )
    result = collector.run()
    assert result.uploaded == 1
    assert current.pending()[0].client_observation_id == "aged"
    assert "outbox_retention" in result.failures
    current.close()


def test_runner_prunes_only_server_governed_obsolete_cutoff(tmp_path: Path):
    path = tmp_path / "governed.sqlite3"
    old = OutboxRepository(path, clock=lambda: NOW - timedelta(days=31))
    old.enqueue(kind="observation", client_observation_id="obsolete", checksum=_wire_checksum("obsolete"),
                cutoff=NOW - timedelta(days=10), payload=_wire("obsolete", "obsolete"), metadata={})
    old.close()
    discovery = {"environment": "testing", "bootstrap_requests": [], "manifests": [],
                 "obsolete_before_cutoff": (NOW - timedelta(days=1)).isoformat()}
    collector, _, outbox = _collector(tmp_path / "unused", discovery=discovery)
    outbox.close()
    transport = FakeTransport(discovery=discovery)
    governed = OutboxRepository(path, clock=lambda: NOW)
    runner = ResidentialCollector(
        client=RailwayClient("http://127.0.0.1", identity_id="collector", environment="testing",
                             transport=transport, allow_insecure_localhost=True),
        outbox=governed, provider=FakeProvider(), identity_id="collector", environment="testing",
        secret="machine-secret", clock=lambda: NOW,
    )
    assert runner.run().disposition in {RunDisposition.NO_WORK, RunDisposition.COMPLETE}
    assert governed.count() == 0
    governed.close()


def test_outbox_fails_closed_when_wal_durability_is_unavailable(monkeypatch, tmp_path: Path):
    import app.collector.outbox as module

    real_connect = module.sqlite3.connect

    class ConnectionProxy:
        def __init__(self, connection):
            object.__setattr__(self, "connection", connection)

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def __setattr__(self, name, value):
            setattr(self.connection, name, value)

        def execute(self, statement, *args, **kwargs):
            if str(statement).strip().casefold() == "pragma journal_mode=wal":
                return self.connection.execute("SELECT 'delete'")
            return self.connection.execute(statement, *args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", lambda *args, **kwargs: ConnectionProxy(real_connect(*args, **kwargs)))
    with pytest.raises(Exception, match="durability mode"):
        OutboxRepository(tmp_path / "unsafe.sqlite3")
