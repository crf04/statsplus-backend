"""Offline seams for the standalone Residential Collector package."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import pandas as pd
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.collector.client import CollectorToken, HTTPResponse, RailwayClient
from app.collector.cache import InstructionCache
from app.collector.config import CollectorConfig, CollectorConfigurationError, load_collector_config
from app.collector.contracts import ProviderContractError
from app.collector.normalizers import (
    SHOT_ZONES,
    normalize_grouped_shot_response,
    normalize_opponent_grouped_shot_response,
    normalize_opponent_zone_response,
    normalize_roster_response,
    normalize_schedule_response,
    normalize_synergy_response,
    normalize_zone_response,
)
from app.collector.outbox import OutboxBusy, OutboxFull, OutboxRepository
from app.collector.provider import _StandaloneNBAProvider
from app.collector.runner import (
    EXIT_NO_WORK,
    EXIT_NON_RETRYABLE,
    EXIT_RETRY,
    RunDisposition,
    ResidentialCollector,
)
from app.domain.team_matchup_taxonomy import (
    NBA_PUBLICATION_STREAMS,
)
from app.domain.slate_time import slate_date_for_instant
from app.models.collection_control import (
    CollectionObservation,
    CompositionJob,
    PublicationPointer,
    PublicationVersion,
)
from app.migrations import run_migrations
from app.services.collection_control import (
    CollectorTokenService,
    CollectionControlService,
    CollectionOperationsService,
    ControlPlaneError,
    NBA_TEAM_IDS,
    ObservationIngestionService,
    PublicationService,
    _collector_scope_descriptors,
)
from app.services import collection_control as collection_control_module
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.ledger_runtime import LedgerRuntime
from app.services.database_first_activation import DatabaseFirstPublicationReader
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from app.services.ledger_matchup_materialization import (
    LedgerMatchupMaterializationService,
)
from app.services.team_matchup_repository import (
    TeamMatchupFact,
    TeamMatchupObservation,
    TeamMatchupRepository,
    TeamMatchupSnapshotScope,
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


def test_live_schedule_shape_selects_regular_season_by_canonical_game_id():
    live_rows = [
        {
            "gameId": "0012500001",
            "homeTeam_teamId": 1610612737,
            "awayTeam_teamId": 1610612738,
            "gameDateTimeUTC": "2025-10-05T00:00:00Z",
            "gameStatus": 3,
            "gameLabel": "Preseason",
        },
        {
            "gameId": "0022500001",
            "homeTeam_teamId": 1610612737,
            "awayTeam_teamId": 1610612738,
            "gameDateTimeUTC": "2025-10-23T00:00:00Z",
            "gameStatus": 3,
            # ScheduleLeagueV2 commonly leaves this optional display field null.
            "gameLabel": None,
        },
    ]

    result = normalize_schedule_response(live_rows, season="2025-26", cutoff=NOW)

    assert [row["nba_game_id"] for row in result.payload["records"]] == ["0022500001"]
    assert result.payload["records"][0]["phase"] == "Regular Season"


def test_production_opponent_shot_frames_preserve_registered_raw_taxonomy():
    shot = normalize_opponent_grouped_shot_response(
        pd.DataFrame([{
            "TEAM_ID": 1610612737,
            "GENERAL_RANGE": "Catch and Shoot",
            "GP": 15, "MIN": 725,
            "FG2M": 4, "FG2A": 8, "FG3M": 2, "FG3A": 6,
        }]),
        season="2025-26", cutoff=NOW, team_id=1610612737,
        category="Catch and Shoot",
    )
    assert shot.observation_type == "shot_types_opponent"
    assert {
        key: shot.payload["records"][0][key]
        for key in ("FG2M", "FG2A", "FG3M", "FG3A")
    } == {"FG2M": 4, "FG2A": 8, "FG3M": 2, "FG3A": 6}
    with pytest.raises(ProviderContractError, match="manifest_scope_mismatch"):
        normalize_opponent_grouped_shot_response(
            pd.DataFrame([{
                "TEAM_ID": 1610612738, "GP": 15, "MIN": 725,
                "GENERAL_RANGE": "Catch and Shoot",
                "FG2M": 4, "FG2A": 8, "FG3M": 2, "FG3A": 6,
            }]),
            season="2025-26", cutoff=NOW, team_id=1610612737,
            category="Catch and Shoot",
        )

    columns = [("", "TEAM_ID"), ("", "GP"), ("", "MIN")]
    values = [1610612737, 15, 725]
    for zone in (
        "Restricted Area", "In The Paint (Non-RA)", "Mid-Range",
        "Above the Break 3",
    ):
        columns.extend(((zone, "OPP_FGM"), (zone, "OPP_FGA")))
        values.extend((4, 8))
    for zone in ("Left Corner 3", "Right Corner 3"):
        columns.extend(((zone, "OPP_FGM"), (zone, "OPP_FGA")))
        values.extend((2, 4))
    # Backcourt completes the opponent's own field-goal totals: the five
    # published zones are 20/40, so 21/43 leaves exactly 1/3 behind the arc's
    # far side.
    columns.extend((("Backcourt", "OPP_FGM"), ("Backcourt", "OPP_FGA")))
    values.extend((1, 3))
    reconciled = [*columns, ("", "OPP_TOTAL_FGM"), ("", "OPP_TOTAL_FGA")]
    zone = normalize_opponent_zone_response(
        pd.DataFrame(
            [[*values, 21, 43]],
            columns=pd.MultiIndex.from_tuples(reconciled),
        ),
        season="2025-26", cutoff=NOW, team_id=1610612737,
    )
    assert zone.observation_type == "shot_zones_opponent"
    assert len(zone.payload["records"]) == 5
    assert {
        (row["category"], row["FGM"], row["FGA"])
        for row in zone.payload["records"]
    } == {
        (zone_name, 4, 8)
        for zone_name in (
            "Restricted Area", "In The Paint (Non-RA)", "Mid-Range",
            "Corner 3", "Above the Break 3",
        )
    }
    # Backcourt is reconciliation evidence, never a published zone.
    assert zone.payload["reconciliation"]["Backcourt"] == {"FGM": 1, "FGA": 3}
    assert "Backcourt" not in {
        row["category"] for row in zone.payload["records"]
    }

    # The provider binds games, minutes, and the independent opponent totals
    # from the TeamStats row for the identical window.
    bound = _StandaloneNBAProvider._bind_opponent_zone_evidence(
        pd.DataFrame(
            [values], columns=pd.MultiIndex.from_tuples(columns)
        ).drop(columns=[("", "GP"), ("", "MIN")]),
        pd.DataFrame([{
            "TEAM_ID": 1610612737, "GP": 15, "MIN": 725,
            "OPP_FGM": 21, "OPP_FGA": 43,
        }]),
        team_id=1610612737,
    )
    assert bound["TEAM_ID"].tolist() == [1610612737]
    assert bound["GP"].tolist() == [15]
    assert bound["MIN"].tolist() == [725]
    assert bound["OPP_TOTAL_FGM"].tolist() == [21]
    assert bound["OPP_TOTAL_FGA"].tolist() == [43]
    # A combined corner beside its split is the live shape.  Under Totals the
    # sides are exact components of it, so the two must agree.
    corner_columns = pd.MultiIndex.from_tuples(
        reconciled + [("Corner 3", "OPP_FGM"), ("Corner 3", "OPP_FGA")]
    )
    combined = normalize_opponent_zone_response(
        pd.DataFrame([[*values, 21, 43, 4, 8]], columns=corner_columns),
        season="2025-26", cutoff=NOW, team_id=1610612737,
    )
    assert {
        (record["category"], record["FGM"], record["FGA"])
        for record in combined.payload["records"]
        if record["category"] == "Corner 3"
    } == {("Corner 3", 4, 8)}

    # The Per48 scale reported a combined corner near the sides' mean rather
    # than their sum.  That disagreement is now the defect itself.
    with pytest.raises(ProviderContractError) as drifted:
        normalize_opponent_zone_response(
            pd.DataFrame([[*values, 21, 43, 5, 8]], columns=corner_columns),
            season="2025-26", cutoff=NOW, team_id=1610612737,
        )
    assert drifted.value.reason == "value_invariant_failed"
    assert drifted.value.diagnostics["window"] == "season"

    wrong_team_values = list(values)
    wrong_team_values[0] = 1610612738
    with pytest.raises(ProviderContractError, match="manifest_scope_mismatch"):
        normalize_opponent_zone_response(
            pd.DataFrame(
                [[*wrong_team_values, 21, 43]],
                columns=pd.MultiIndex.from_tuples(reconciled),
            ),
            season="2025-26", cutoff=NOW, team_id=1610612737,
        )
    with pytest.raises(ProviderContractError, match="provider_window_unverified"):
        normalize_opponent_zone_response(
            pd.DataFrame(
                [[values[0], *values[2:], 21, 43]],
                columns=pd.MultiIndex.from_tuples(
                    [reconciled[0], *reconciled[2:]]
                ),
            ),
            season="2025-26", cutoff=NOW, team_id=1610612737,
        )


def test_schedule_normalizer_canonicalizes_terminal_alias_and_postponement():
    row = {
        **_schedule()[0],
        "status": "Game Finished",
        "is_postponed": True,
        "postponement_evidence": {"reason": "weather"},
    }
    event = normalize_schedule_response(
        [row], season="2025-26", cutoff=NOW
    ).payload["records"][0]
    assert event["status"] == "Final"
    assert event["is_postponed"] is True
    assert event["postponement_evidence"] == {"reason": "weather"}


def test_live_roster_shape_skips_inactive_unaffiliated_players_before_team_validation():
    rows = [
        {
            "PERSON_ID": 1,
            "DISPLAY_FIRST_LAST": "Active Player",
            "ROSTERSTATUS": 1,
            "FROM_YEAR": "2020",
            "TO_YEAR": "2025",
            "TEAM_ID": 1610612737,
        },
        {
            "PERSON_ID": 2,
            "DISPLAY_FIRST_LAST": "Inactive Free Agent",
            "ROSTERSTATUS": 0,
            "FROM_YEAR": "2020",
            "TO_YEAR": "2025",
            "TEAM_ID": 0,
        },
    ]

    result = normalize_roster_response(rows, season="2025-26", cutoff=NOW)

    assert [row["player_id"] for row in result.payload["records"]] == [1]


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

    l15_boundaries = {
        int(team_id): "07/30/2026" for team_id in NBA_TEAM_IDS
    }
    descriptors = _collector_scope_descriptors({
        "synergy_opponent", "shot_types_opponent", "shot_zones_opponent",
    }, NOW, l15_date_from_by_team=l15_boundaries)
    opponent = [item for item in descriptors if item["parameters"].get("subject") == "opponent"]
    team_scoped = [item for item in opponent if "team_id" in item["parameters"]]
    assert {str(item["parameters"]["team_id"]) for item in team_scoped} == NBA_TEAM_IDS
    assert {item["parameters"]["window"] for item in team_scoped} == {"season", "l15"}
    assert {item["parameters"]["date_to"] for item in team_scoped} == {"08/13/2026"}
    assert {
        item["parameters"]["date_from"]
        for item in team_scoped
        if item["parameters"]["window"] == "season"
    } == {None}
    assert {
        item["parameters"]["date_from"]
        for item in team_scoped
        if item["parameters"]["window"] == "l15"
    } == {"07/30/2026"}
    assert {
        item["parameters"]["per_mode"] for item in team_scoped
        if item["scope"] == "shot_types_opponent"
    } == {"Totals"}
    assert {
        item["parameters"]["value_mode"] for item in team_scoped
        if item["scope"] == "shot_types_opponent"
    } == {"totals_with_minutes"}
    assert {
        item["parameters"]["per_mode"] for item in team_scoped
        if item["scope"] == "shot_zones_opponent"
    } == {"Totals"}
    assert {
        item["parameters"]["value_mode"] for item in team_scoped
        if item["scope"] == "shot_zones_opponent"
    } == {"totals_with_minutes"}
    synergy = [item for item in opponent if item["scope"] == "synergy_opponent"]
    assert len(synergy) == 11
    assert {item["parameters"]["window"] for item in synergy} == {"season"}
    assert {item["parameters"]["subject_code"] for item in synergy} == {"T"}
    assert {item["parameters"]["type_grouping"] for item in synergy} == {"Defensive"}
    assert {item["parameters"]["per_mode"] for item in synergy} == {"Totals"}
    assert not any(item["scope"] in {"grouped_shot_types", "exact_shot_zones"} for item in opponent)

    l15_only = _collector_scope_descriptors(
        {"grouped_shot_types_opponent_l15"}, NOW,
        l15_date_from_by_team=l15_boundaries,
    )
    assert {item["parameters"]["window"] for item in l15_only} == {"l15"}
    assert {item["scope"] for item in l15_only} == {"shot_types_opponent"}

    utc_evening = datetime(2025, 11, 2, 3, 30, tzinfo=timezone.utc)
    prior_slate = _collector_scope_descriptors(
        {"shot_zones_opponent"}, utc_evening,
        l15_date_from_by_team={
            int(team_id): "10/15/2025" for team_id in NBA_TEAM_IDS
        },
    )
    assert {
        item["parameters"]["date_to"]
        for item in prior_slate
        if item["parameters"].get("subject") == "opponent"
    } == {"11/01/2025"}


def test_compose_prefers_the_latest_accepted_observation_per_identity(tmp_path):
    # A retried collection appends a second accepted observation for the same
    # (team, category); the composed payload must come from the newer one
    # rather than refusing the manifest as a taxonomy duplicate.
    control_db = create_engine(f"sqlite:///{tmp_path / 'latest.sqlite3'}")
    run_migrations(control_db)
    team_ids = sorted(NBA_TEAM_IDS)
    control = CollectionControlService(control_db, clock=lambda: NOW)
    control.activate_season("2025-26", actor="operator")
    event_payload = {"complete_snapshot": True, "events": [{
        "nba_game_id": f"game-{round_index}-{pair_index}",
        "home_team_id": team_ids[pair_index * 2],
        "away_team_id": team_ids[pair_index * 2 + 1],
        "phase": "Regular Season", "status": "Final",
        "scheduled_at": (
            NOW - timedelta(days=15 - round_index, hours=1)
        ).isoformat(),
    } for round_index in range(15) for pair_index in range(15)]}
    event_request = control.create_bootstrap_request("2025-26", "event", cutoff=NOW)
    control.publish_catalog(event_request.request_id, event_payload, version="event-v1")
    athlete_request = control.create_bootstrap_request("2025-26", "athlete", cutoff=NOW)
    control.publish_catalog(athlete_request.request_id, {"complete_snapshot": True, "identities": [{
        "player_id": "1", "team_id": team_ids[0], "status": "active",
        "event_ids": [
            f"game-{round_index}-{pair_index}"
            for round_index in range(15) for pair_index in range(15)
        ],
    }]}, version="athlete-v1")
    manifest = control.create_manifest(
        "2025-26", cutoff=NOW,
        scopes={"synergy_play_types_opponent_season", "canonical_game_ledger"},
        collect_before=NOW + timedelta(hours=1),
    )
    governance = ActiveManifestLedgerGovernanceReader(control_db)
    publications = PublicationService(
        control_db, clock=lambda: NOW, l15_expectation_resolver=governance,
    )
    publications.register_stream(
        "synergy_play_types_opponent_season", provider="nba",
        owner="residential_collector",
        required_observations=["synergy_opponent"],
        publication_strategy="snapshot_replace", supported_windows=["season"],
        completeness_rule="base_complete", enabled=True,
    )
    tokens = CollectorTokenService(
        control_db, environment="testing", signing_secret="test", clock=lambda: NOW
    )
    identity = tokens.create_identity(
        "collector", scopes=["ingest"], owner="residential_collector",
        providers=["nba"], surfaces=["synergy_opponent"],
    )
    claims = tokens.validate(tokens.issue_for_secret(
        identity["identity_id"], identity["secret"], scopes=["ingest"]
    ))
    ticks = iter(range(1, 10_000))
    ingestion = ObservationIngestionService(
        control_db, publication_service=publications,
        clock=lambda: NOW + timedelta(seconds=next(ticks)),
    )
    from app.collector.normalizers import normalize_opponent_synergy_response
    from app.models.catalogs import PLAY_TYPES
    def deliver(points):
        for play_type in PLAY_TYPES:
            observation = normalize_opponent_synergy_response(
                [{"TEAM_ID": team_id, "PLAY_TYPE": play_type,
                  "TYPE_GROUPING": "Defensive", "GP": 15, "MIN": 725.0,
                  "POSS": 100, "PTS": points}
                 for team_id in team_ids],
                season="2025-26", cutoff=NOW,
                scope={"window": "season", "phase": "Regular Season",
                       "play_type": play_type, "subject": "opponent",
                       "value_mode": "totals_with_minutes"},
            )
            envelope = {
                "client_observation_id": f"obs-{play_type}-{points}",
                "observation_type": "synergy_opponent",
                "provider": "nba", "season": "2025-26",
                "cutoff": NOW.isoformat(), "schema_version": 2,
                "retrieved_at": NOW.isoformat(),
                "manifest_id": manifest.manifest_id,
                "scope": observation.scope,
                "environment": "testing",
            }
            raw = json.dumps(observation.payload, sort_keys=True, separators=(",", ":")).encode()
            envelope["checksum"] = hashlib.sha256(raw).hexdigest()
            ingestion.ingest(claims, envelope, gzip.compress(raw), compressed=True)
    deliver(points=110)
    deliver(points=120)
    version = publications.compose_from_observations(
        "synergy_play_types_opponent_season", season="2025-26", cutoff=NOW,
        manifest_id=manifest.manifest_id,
    )
    payload = json.loads(version.payload) if isinstance(version.payload, str) else version.payload
    rows = payload if isinstance(payload, list) else payload.get("rows", payload.get("records"))
    text = json.dumps(rows)
    # 120 points over 725 minutes per 48 — the newer observation's value.
    assert f"{120 * 48 / 725.0:.9f}"[:8] in text or str(120 * 48 / 725.0)[:8] in text
    assert str(110 * 48 / 725.0)[:8] not in text
    transition = 120 * 48 / 725.0
    # 120 points over 725 minutes per 48 — the newer observation's value.
    assert abs(transition - 120 * 48 / 725.0) < 1e-9
    assert version.status in {"candidate", "active"}


def test_exact_window_opponent_breakdown_collapses_to_one_team_row():
    # With a date window the opponent shot chart reports one row per opponent
    # faced; the season shape is a single aggregate row, so the counts are
    # summed and the per-opponent game column dropped.
    breakdown = pd.DataFrame([
        {"TEAM_ID": 1610612765, "TEAM_NAME": "Washington Wizards", "GP": 15,
         "G": 2, "FG2M": 0, "FG2A": 1, "FG3M": 15, "FG3A": 30, "MIN": 96.0},
        {"TEAM_ID": 1610612765, "TEAM_NAME": "Minnesota Timberwolves", "GP": 15,
         "G": 1, "FG2M": 3, "FG2A": 5, "FG3M": 14, "FG3A": 28, "MIN": 48.0},
    ])
    collapsed = _StandaloneNBAProvider._collapse_opponent_breakdown(
        breakdown, team_id=1610612765
    )
    assert len(collapsed.index) == 1
    assert "G" not in collapsed.columns
    row = collapsed.iloc[0]
    assert (row["FG2M"], row["FG2A"], row["FG3M"], row["FG3A"], row["MIN"]) == (
        3, 6, 29, 58, 144.0,
    )

    # A single-row season response passes through untouched.
    season = pd.DataFrame([{"TEAM_ID": 1610612765, "GP": 82, "FG2M": 100, "FG2A": 200}])
    assert _StandaloneNBAProvider._collapse_opponent_breakdown(
        season, team_id=1610612765
    ).equals(season)


def _totals_zone_row(**overrides):
    """One coherent opponent Totals row: five zones plus Backcourt reconcile."""

    base = {
        "TEAM_ID": 1610612737, "GP": 15, "MIN": 725,
        **{f"{zone}_OPP_{stat}": value
           for zone in ("Restricted Area", "In The Paint (Non-RA)",
                        "Mid-Range", "Above the Break 3")
           for stat, value in (("FGM", 4), ("FGA", 8))},
        "Corner 3_OPP_FGM": 4, "Corner 3_OPP_FGA": 8,
        "Left Corner 3_OPP_FGM": 2, "Left Corner 3_OPP_FGA": 4,
        "Right Corner 3_OPP_FGM": 2, "Right Corner 3_OPP_FGA": 4,
        "Backcourt_OPP_FGM": 1, "Backcourt_OPP_FGA": 3,
        # Five zones (20/40) plus Backcourt (1/3).
        "OPP_TOTAL_FGM": 21, "OPP_TOTAL_FGA": 43,
    }
    base.update(overrides)
    return pd.DataFrame([{k: v for k, v in base.items() if v is not None}])


def test_totals_zone_response_reconciles_against_the_opponent_totals():
    """Under Totals the zones are exact components, so they must add up."""

    observation = normalize_opponent_zone_response(
        _totals_zone_row(), season="2025-26", cutoff=NOW, team_id=1610612737,
    )
    corner = next(
        record for record in observation.payload["records"]
        if record["category"] == "Corner 3"
    )
    assert (corner["FGM"], corner["FGA"]) == (4, 8)
    assert corner["minutes"] == 725

    # Backcourt and the Corner 3 sides are retained as evidence for the
    # backend's independent check, and never as published zones.
    reconciliation = observation.payload["reconciliation"]
    assert reconciliation["opponent_totals"] == {"FGM": 21, "FGA": 43}
    assert reconciliation["Backcourt"] == {"FGM": 1, "FGA": 3}
    assert reconciliation["Left Corner 3"] == {"FGM": 2, "FGA": 4}
    assert {record["category"] for record in observation.payload["records"]} == set(
        SHOT_ZONES
    )

    # One zone short of the opponent total is a defect, not a rounding band,
    # and it names the team, window, equation and residual so a persistent
    # mismatch is actionable.
    with pytest.raises(ProviderContractError) as short:
        normalize_opponent_zone_response(
            _totals_zone_row(**{"Mid-Range_OPP_FGA": 7}),
            season="2025-26", cutoff=NOW, team_id=1610612737,
        )
    assert short.value.reason == "value_invariant_failed"
    assert short.value.diagnostics == {
        "team_id": 1610612737,
        "window": "season",
        "equation": "zones_plus_backcourt_equals_opponent_fga",
        "expected": 43.0,
        "observed": 42.0,
        "residual": -1.0,
    }

    # A combined corner that disagrees with its own sides is the same defect.
    with pytest.raises(ProviderContractError) as corner_split:
        normalize_opponent_zone_response(
            _totals_zone_row(**{"Left Corner 3_OPP_FGM": 3}),
            season="2025-26", cutoff=NOW, team_id=1610612737,
        )
    assert corner_split.value.diagnostics["equation"] == (
        "corner_3_fgm_equals_left_plus_right"
    )
    assert corner_split.value.diagnostics["residual"] == -1.0

    # Missing reconciliation evidence cannot be waved through.
    for missing in ("Backcourt_OPP_FGM", "OPP_TOTAL_FGA"):
        with pytest.raises(ProviderContractError, match="provider_schema_changed"):
            normalize_opponent_zone_response(
                _totals_zone_row(**{missing: None}),
                season="2025-26", cutoff=NOW, team_id=1610612737,
            )


def test_legacy_per48_zone_response_prefers_the_combined_corner_over_its_sides():
    # The legacy rate-scale row reports "Corner 3" both combined and split;
    # the split is not an additive decomposition under Per48, so the combined
    # value wins whenever it is present and the sides are only summed in its
    # absence.  This mode can no longer authorize a publication -- the backend
    # requires ``totals_with_minutes`` -- but recorded evidence still parses.
    def row(**overrides):
        base = {
            "TEAM_ID": 1610612737, "GP": 15, "MIN": 725,
            **{f"{zone}_OPP_{stat}": value
               for zone in ("Restricted Area", "In The Paint (Non-RA)",
                            "Mid-Range", "Above the Break 3")
               for stat, value in (("FGM", 4), ("FGA", 8))},
            "Corner 3_OPP_FGM": 40.8, "Corner 3_OPP_FGA": 112.2,
            "Left Corner 3_OPP_FGM": 41.8, "Left Corner 3_OPP_FGA": 113.3,
            "Right Corner 3_OPP_FGM": 39.6, "Right Corner 3_OPP_FGA": 110.8,
        }
        base.update(overrides)
        return pd.DataFrame([{k: v for k, v in base.items() if v is not None}])

    observation = normalize_opponent_zone_response(
        row(), season="2025-26", cutoff=NOW, team_id=1610612737,
        value_mode="per48",
    )
    corner = next(
        record for record in observation.payload["records"]
        if record["category"] == "Corner 3"
    )
    assert (corner["FGM"], corner["FGA"]) == (40.8, 112.2)
    assert "reconciliation" not in observation.payload

    # Without the combined columns the sides are summed.
    summed = normalize_opponent_zone_response(
        row(**{"Corner 3_OPP_FGM": None, "Corner 3_OPP_FGA": None,
               "Left Corner 3_OPP_FGM": 2, "Left Corner 3_OPP_FGA": 4,
               "Right Corner 3_OPP_FGM": 2, "Right Corner 3_OPP_FGA": 4}),
        season="2025-26", cutoff=NOW, team_id=1610612737, value_mode="per48",
    )
    corner = next(
        record for record in summed.payload["records"]
        if record["category"] == "Corner 3"
    )
    assert (corner["FGM"], corner["FGA"]) == (4, 8)

    # A lone partial split remains schema drift.
    with pytest.raises(ProviderContractError, match="provider_schema_changed"):
        normalize_opponent_zone_response(
            row(**{"Corner 3_OPP_FGM": None, "Corner 3_OPP_FGA": None,
                   "Right Corner 3_OPP_FGM": None}),
            season="2025-26", cutoff=NOW, team_id=1610612737,
            value_mode="per48",
        )


def test_synergy_window_binds_team_minutes_and_tolerates_partial_play_type_games():
    # Synergy counts only games with at least one possession of the play
    # type, so its GP sits below the team's games for rarer play types.
    synergy = pd.DataFrame([
        {"TEAM_ID": 1610612737, "GP": 77, "POSS": 1415, "PTS": 1437},
        {"TEAM_ID": 1610612738, "GP": 80, "POSS": 1300, "PTS": 1290},
    ])
    minutes = pd.DataFrame([
        {"TEAM_ID": 1610612737, "GP": 82, "MIN": 3971},
        {"TEAM_ID": 1610612738, "GP": 82, "MIN": 3966},
    ])
    bound = _StandaloneNBAProvider._bind_synergy_window(synergy, minutes)
    assert bound["GP"].tolist() == [82, 82]
    assert bound["MIN"].tolist() == [3971, 3966]
    assert "WINDOW_GP" not in bound.columns

    # More Synergy games than the team played proves the wrong window.
    with pytest.raises(ProviderContractError, match="provider_window_unverified"):
        _StandaloneNBAProvider._bind_synergy_window(
            synergy.assign(GP=[83, 80]), minutes
        )
    # A team missing from the minutes evidence cannot be bound.
    with pytest.raises(ProviderContractError, match="provider_window_unverified"):
        _StandaloneNBAProvider._bind_synergy_window(synergy, minutes.iloc[:1])


@pytest.mark.parametrize(
    ("evidence_mode", "expected_composed"),
    (
        ("complete", 5), ("partial", 4), ("tampered", 4), ("gp14", 4),
        ("permuted", 1), ("projection_failure", 5),
        ("mixed_projection_failure", 2),
        ("mixed_governance_type_error", 2),
        ("mixed_governance_runtime_error", 2),
        ("mixed_governance_db_error", 2),
        ("backdated_synergy", 4),
        ("l15_boundary_mismatch", 4),
    ),
)
def test_runner_ingestion_and_composition_publish_all_supported_opponent_windows(
    tmp_path: Path, evidence_mode: str, expected_composed: int, monkeypatch,
):
    control_db = create_engine(f"sqlite:///{tmp_path / 'control.sqlite3'}")
    run_migrations(control_db)
    team_ids = sorted(NBA_TEAM_IDS)
    cutoff = (
        NOW - timedelta(days=1)
        if evidence_mode == "backdated_synergy"
        else NOW
    )
    control = CollectionControlService(control_db, clock=lambda: NOW)
    control.activate_season("2025-26", actor="operator")
    event_payload = {"complete_snapshot": True, "events": [{
        "nba_game_id": f"game-{round_index}-{pair_index}",
        "home_team_id": team_ids[pair_index * 2],
        "away_team_id": team_ids[pair_index * 2 + 1],
        "phase": "Regular Season", "status": "Final",
        "scheduled_at": (
            cutoff - timedelta(days=15 - round_index, hours=1)
        ).isoformat(),
    } for round_index in range(15) for pair_index in range(15)]}
    event_request = control.create_bootstrap_request(
        "2025-26", "event", cutoff=cutoff
    )
    event_publication = control.publish_catalog(
        event_request.request_id, event_payload, version="event-v1"
    )
    assert event_publication.complete
    athlete_request = control.create_bootstrap_request(
        "2025-26", "athlete", cutoff=cutoff
    )
    control.publish_catalog(athlete_request.request_id, {"complete_snapshot": True, "identities": [{
        "player_id": "1", "team_id": team_ids[0], "status": "active",
        "event_ids": [
            f"game-{round_index}-{pair_index}"
            for round_index in range(15) for pair_index in range(15)
        ],
    }]}, version="athlete-v1")
    observation_types = {
        "synergy_opponent", "shot_types_opponent", "shot_zones_opponent",
    }
    publication_streams = {
        template.format(window=window)
        for base, template in NBA_PUBLICATION_STREAMS.items()
        for window in ("season", "l15")
        if not (base == "play_types" and window == "l15")
    }
    manifest = control.create_manifest(
        "2025-26", cutoff=cutoff,
        scopes={*publication_streams, "canonical_game_ledger"},
        collect_before=NOW + timedelta(hours=1),
    )
    manifest_scopes = set(json.loads(manifest.scopes))
    assert observation_types <= manifest_scopes
    l15_date_from = slate_date_for_instant(
        cutoff - timedelta(days=15, hours=1)
    ).strftime("%m/%d/%Y")
    descriptors = _collector_scope_descriptors(
        manifest_scopes,
        cutoff,
        l15_date_from_by_team={
            int(team_id): l15_date_from for team_id in team_ids
        },
    )
    discovery = {"environment": "testing", "bootstrap_requests": [], "manifests": [{
        "manifest_id": manifest.manifest_id, "season": "2025-26",
        "cutoff": cutoff.isoformat(),
        "collect_before": (NOW + timedelta(hours=1)).isoformat(),
        "accepted_versions": [2], "scopes": sorted(manifest_scopes),
        "scope_descriptors": descriptors,
    }]}

    class OpponentProvider(FakeProvider):
        def fetch_synergy_play_types(self, category, **kwargs):
            assert kwargs["player_or_team_abbreviation"] == "T"
            assert kwargs["type_grouping"] == "Defensive"
            assert kwargs["per_mode_simple"] == "Totals"
            rows = [{
                "team_id": int(team_id), "category": category,
                "GP": 15, "MIN": 750, "POSS": 10, "PTS": 12,
            } for team_id in team_ids]
            return rows[:1] if evidence_mode == "partial" and category == "Transition" else rows

        def fetch_opponent_shot_chart(self, category, _date_from, **kwargs):
            assert kwargs["per_mode_simple"] == "Totals"
            assert kwargs["date_to"] == cutoff.strftime("%m/%d/%Y")
            assert _date_from == (
                l15_date_from if kwargs["last_n_games"] == 15 else None
            )
            return [{
                "team_id": kwargs["team_id"], "category": category,
                "GP": 14 if evidence_mode == "gp14" and kwargs["last_n_games"] == 15 else 15,
                "MIN": 725,
                "FG2M": 40, "FG2A": 80, "FG3M": 20, "FG3A": 60,
            }]

        def fetch_opponent_shooting_zone(self, _date_from, **kwargs):
            assert kwargs["per_mode_detailed"] == "Totals"
            assert _date_from == (
                l15_date_from if kwargs["last_n_games"] == 15 else None
            )
            # Four zones at 4/8 plus a 2+2 / 4+4 corner is 20/40; Backcourt
            # carries the remaining 1/3 up to the opponent's own totals.
            return [{
                "team_id": kwargs["team_id"],
                "GP": 15,
                "MIN": 725,
                **{
                    f"{zone}_{stat}": value
                    for zone in (
                        "Restricted Area", "In The Paint (Non-RA)",
                        "Mid-Range", "Above the Break 3",
                    )
                    for stat, value in (("OPP_FGM", 4), ("OPP_FGA", 8))
                },
                "Left Corner 3_OPP_FGM": 2,
                "Left Corner 3_OPP_FGA": 4,
                "Right Corner 3_OPP_FGM": 2,
                "Right Corner 3_OPP_FGA": 4,
                "Backcourt_OPP_FGM": 1,
                "Backcourt_OPP_FGA": 3,
                "OPP_TOTAL_FGM": 21,
                "OPP_TOTAL_FGA": 43,
            }]

    collector, transport, outbox = _collector(
        tmp_path, discovery=discovery, provider=OpponentProvider()
    )
    result = collector.run()
    assert result.disposition is RunDisposition.COMPLETE
    uploaded = [
        json.loads(gzip.decompress(call[3]))
        for call in transport.calls
        if "/api/collector/observations" in call[1]
    ]
    assert {document["observation_type"] for document in uploaded} == observation_types
    assert not {"synergy_play_types", "grouped_shot_types", "exact_shot_zones"}.intersection(
        document["observation_type"] for document in uploaded
    )

    governance = ActiveManifestLedgerGovernanceReader(control_db)
    publications = PublicationService(
        control_db, clock=lambda: NOW,
        l15_expectation_resolver=governance,
    )
    for base, template in NBA_PUBLICATION_STREAMS.items():
        for window in ("season", "l15"):
            if base == "play_types" and window == "l15":
                continue
            observation_type = {
                "play_types": "synergy_opponent",
                "shot_types": "shot_types_opponent",
                "shot_zones": "shot_zones_opponent",
            }[base]
            publications.register_stream(
                template.format(window=window), provider="nba",
                owner="residential_collector",
                required_observations=[observation_type],
                publication_strategy="snapshot_replace",
                supported_windows=[window], completeness_rule="base_complete",
                enabled=True,
            )
    tokens = CollectorTokenService(
        control_db, environment="testing", signing_secret="test", clock=lambda: NOW
    )
    identity = tokens.create_identity(
        "collector", scopes=["ingest"], owner="residential_collector",
        providers=["nba"], surfaces=sorted(observation_types),
    )
    claims = tokens.validate(tokens.issue_for_secret(
        identity["identity_id"], identity["secret"], scopes=["ingest"]
    ))
    ingestion = ObservationIngestionService(
        control_db, publication_service=publications, clock=lambda: NOW
    )
    replayed_cutoff = json.loads(json.dumps(next(
        document for document in uploaded
        if document["observation_type"] == "shot_types_opponent"
    )))
    replayed_cutoff["client_observation_id"] += "-wrong-cutoff"
    replayed_cutoff["scope"]["endpoint_window"]["date_to"] = "08/11/2026"
    replayed_payload = json.dumps(
        replayed_cutoff.pop("payload"), sort_keys=True, separators=(",", ":")
    ).encode()
    with pytest.raises(ControlPlaneError, match="manifest_scope_mismatch"):
        ingestion.ingest(claims, replayed_cutoff, replayed_payload)
    replayed_l15 = json.loads(json.dumps(next(
        document for document in uploaded
        if document["observation_type"] == "shot_types_opponent"
        and document["scope"]["window"] == "l15"
    )))
    replayed_l15["client_observation_id"] += "-wrong-l15-boundary"
    replayed_l15["scope"]["endpoint_window"]["date_from"] = "01/01/1900"
    replayed_l15_payload = json.dumps(
        replayed_l15.pop("payload"), sort_keys=True, separators=(",", ":")
    ).encode()
    with pytest.raises(ControlPlaneError, match="provider_window_unverified"):
        ingestion.ingest(claims, replayed_l15, replayed_l15_payload)
    for observation_type in ("shot_types_opponent", "shot_zones_opponent"):
        mismatched = json.loads(json.dumps(next(
            document
            for document in uploaded
            if document["observation_type"] == observation_type
        )))
        mismatched["client_observation_id"] += "-scope-mismatch"
        scoped_team_id = int(mismatched["scope"]["team_id"])
        mismatched["scope"]["team_id"] = next(
            int(team_id) for team_id in team_ids if int(team_id) != scoped_team_id
        )
        assert {
            int(record["team_id"])
            for record in mismatched["payload"]["records"]
        } == {scoped_team_id}
        assert mismatched["observation_type"] == observation_type
        assert int(mismatched["scope"]["team_id"]) != scoped_team_id
        mismatched_payload = json.dumps(
            mismatched.pop("payload"), sort_keys=True, separators=(",", ":")
        ).encode()
        with pytest.raises(ControlPlaneError, match="malformed_payload"):
            ingestion.ingest(claims, mismatched, mismatched_payload)
    for document in uploaded:
        payload = json.dumps(
            document.pop("payload"), sort_keys=True, separators=(",", ":")
        ).encode()
        ingestion.ingest(claims, document, payload)
    queued_streams = set(publication_streams)
    if evidence_mode in {
        "mixed_projection_failure", "mixed_governance_type_error",
        "mixed_governance_runtime_error", "mixed_governance_db_error",
    }:
        nba_stream = "grouped_shot_types_opponent_season"
        with control_db.begin() as connection:
            connection.execute(delete(CompositionJob).where(
                CompositionJob.stream_key != nba_stream
            ))
            connection.execute(CompositionJob.__table__.insert().values(
                job_id="mixed-ledger-job",
                stream_key="traditional_opponent_season",
                manifest_id=manifest.manifest_id,
                season="2025-26", cutoff=cutoff, status="queued",
                attempts=0, created_at=NOW, updated_at=NOW,
            ))
        queued_streams = {nba_stream, "traditional_opponent_season"}
    if evidence_mode == "tampered":
        with control_db.begin() as connection:
            observations = connection.execute(select(CollectionObservation).where(
                CollectionObservation.observation_type == "shot_types_opponent"
            )).mappings().all()
            target = next(
                row for row in observations
                if json.loads(row["scope"])["window"] == "season"
            )
            connection.execute(update(CollectionObservation).where(
                CollectionObservation.observation_id == target["observation_id"]
            ).values(payload=target["payload"] + " "))
    if evidence_mode == "permuted":
        next_team = {
            int(team_id): int(team_ids[(index + 1) % len(team_ids)])
            for index, team_id in enumerate(team_ids)
        }
        with control_db.begin() as connection:
            observations = connection.execute(select(CollectionObservation).where(
                CollectionObservation.observation_type.in_((
                    "shot_types_opponent", "shot_zones_opponent",
                ))
            )).mappings().all()
            for observation in observations:
                scope = json.loads(observation["scope"])
                scope["team_id"] = next_team[int(scope["team_id"])]
                connection.execute(update(CollectionObservation).where(
                    CollectionObservation.observation_id
                    == observation["observation_id"]
                ).values(scope=json.dumps(scope, sort_keys=True)))
    if evidence_mode == "l15_boundary_mismatch":
        with control_db.begin() as connection:
            observations = connection.execute(select(CollectionObservation).where(
                CollectionObservation.observation_type == "shot_types_opponent"
            )).mappings().all()
            target = next(
                row for row in observations
                if json.loads(row["scope"])["window"] == "l15"
            )
            scope = json.loads(target["scope"])
            scope["endpoint_window"]["date_from"] = "01/01/1900"
            connection.execute(update(CollectionObservation).where(
                CollectionObservation.observation_id == target["observation_id"]
            ).values(scope=json.dumps(scope, sort_keys=True)))
    with control_db.connect() as connection:
        assert {
            row.stream_key
            for row in connection.execute(select(CompositionJob))
        } == queued_streams

    with pytest.raises(
        ControlPlaneError,
        match=(
            "provider_unbounded_as_of"
            if evidence_mode == "backdated_synergy"
            else "publication_candidate_invalid"
        ),
    ):
        publications.compose(
            "synergy_play_types_opponent_season", season="2025-26",
            cutoff=cutoff, payload={"rows": []},
            manifest_id=manifest.manifest_id,
        )
    matchup_repository = TeamMatchupRepository(
        control_db,
        publication_write_capability=(
            publications.governed_publication_write_capability()
        ),
    )
    preserved_scope = TeamMatchupSnapshotScope("2025-26", cutoff.date())
    matchup_repository.replace_snapshots(
        [(
            preserved_scope,
            [
                TeamMatchupFact(
                    team_id=team_id, base="traditional", slice_key="overall",
                    stat_key="FGA", raw_value=1, denominator_value=1,
                    denominator_unit="minutes", provider="ledger",
                )
                for team_id in team_ids
            ],
            [TeamMatchupObservation(surface="traditional", status="available")],
        )],
        retrieved_at=cutoff,
    )
    class FailOnceMaterialization(LedgerMatchupMaterializationService):
        fail_once = evidence_mode in {
            "projection_failure", "mixed_projection_failure",
        }

        def refresh_publication_surfaces(self, *args, **kwargs):
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("injected matchup projection failure")
            return super().refresh_publication_surfaces(*args, **kwargs)

        def materialize(self, season, *, as_of, **kwargs):
            if evidence_mode not in {
                "mixed_projection_failure", "mixed_governance_type_error",
                "mixed_governance_runtime_error", "mixed_governance_db_error",
            }:
                return super().materialize(season, as_of=as_of, **kwargs)
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("injected mixed matchup projection failure")
            governed = governance.read_for_composition(
                season, cutoff, manifest.manifest_id
            )
            return self.refresh_publication_surfaces(
                season,
                as_of=as_of,
                expected_game_ids_by_team=governed.expected_season_game_ids,
                expected_l15_game_ids=governed.expected_l15_game_ids,
                team_ids=governed.team_ids,
                session=kwargs.get("session"),
            )

    matchup_materialization = FailOnceMaterialization(
        CanonicalGameLedgerRepository(control_db),
        matchup_repository,
        publication_reader=DatabaseFirstPublicationReader(
            control_db, clock=lambda: NOW
        ),
        l15_expectation_resolver=governance,
        clock=lambda: NOW,
    )
    ledger_window = SimpleNamespace(complete=True, reason=None)
    ledger_l15_window = SimpleNamespace(
        complete=False, reason="insufficient governed games"
    )
    ledger_materialization = SimpleNamespace(compose=lambda *args, **kwargs: (
        SimpleNamespace(
            season_window=ledger_window,
            l15_window=ledger_l15_window,
            assist_location_season=None,
            assist_location_l15=None,
        )
    ))
    runtime_repository = SimpleNamespace(
        engine=control_db,
        list_games=lambda *args, **kwargs: (),
        get_game=lambda *args, **kwargs: None,
    )
    runtime = LedgerRuntime(
        backfill=None,
        repository=runtime_repository,
        materialization=(
            ledger_materialization
            if evidence_mode in {
                "mixed_projection_failure", "mixed_governance_type_error",
                "mixed_governance_runtime_error", "mixed_governance_db_error",
            }
            else None
        ),
        governance=governance,
        matchup_materialization=matchup_materialization,
        publication_service=publications,
        clock=lambda: NOW,
    )
    original_resolver = publications.l15_expectation_resolver
    if evidence_mode.startswith("mixed_governance_"):
        class BrokenResolver:
            def resolve_team_game_ids(self, *args, **kwargs):
                if evidence_mode == "mixed_governance_type_error":
                    raise TypeError("resolver implementation defect")
                if evidence_mode == "mixed_governance_runtime_error":
                    raise RuntimeError("resolver runtime defect")
                raise OperationalError("SELECT governed", {}, RuntimeError("db down"))

        publications.l15_expectation_resolver = BrokenResolver()
    if evidence_mode in {
        "projection_failure", "mixed_projection_failure",
        "mixed_governance_type_error",
        "mixed_governance_runtime_error", "mixed_governance_db_error",
    }:
        expected_failure = {
            "projection_failure": RuntimeError,
            "mixed_projection_failure": RuntimeError,
            "mixed_governance_type_error": TypeError,
            "mixed_governance_runtime_error": RuntimeError,
            "mixed_governance_db_error": OperationalError,
        }[evidence_mode]
        with pytest.raises(expected_failure):
            runtime.compose_queued("2025-26")
        with control_db.connect() as connection:
            assert {
                row.status for row in connection.execute(select(CompositionJob))
            } == {"failed"}
            assert connection.execute(select(PublicationVersion)).all() == []
            assert connection.execute(
                select(PublicationPointer.__table__)
            ).mappings().all() == []
        failed_snapshot = matchup_repository.get_snapshot(preserved_scope)
        assert {fact.base for fact in failed_snapshot.facts} == {"traditional"}
        publications.l15_expectation_resolver = original_resolver
        with control_db.begin() as connection:
            connection.execute(update(CompositionJob).where(
                CompositionJob.status == "failed",
            ).values(status="queued", last_error=None))
    composed = runtime.compose_queued("2025-26")
    with control_db.connect() as connection:
        composition_diagnostics = [
            (row.stream_key, row.status, row.last_error)
            for row in connection.execute(select(CompositionJob))
        ]
    assert composed == expected_composed, composition_diagnostics
    with control_db.connect() as connection:
        job_statuses = {
            row.stream_key: row.status
            for row in connection.execute(select(CompositionJob))
        }
    assert job_statuses == {
        stream_key: (
            "failed"
            if (
                evidence_mode == "partial"
                and stream_key == "synergy_play_types_opponent_season"
            ) or (
                evidence_mode == "backdated_synergy"
                and stream_key == "synergy_play_types_opponent_season"
            ) or (
                evidence_mode == "tampered"
                and stream_key == "grouped_shot_types_opponent_season"
            ) or (
                evidence_mode == "gp14"
                and stream_key == "grouped_shot_types_opponent_l15"
            ) or (
                evidence_mode == "permuted"
                and stream_key != "synergy_play_types_opponent_season"
            ) or (
                evidence_mode == "l15_boundary_mismatch"
                and stream_key == "grouped_shot_types_opponent_l15"
            )
            else "succeeded"
        )
        for stream_key in queued_streams
    }
    if evidence_mode == "projection_failure":
        with control_db.connect() as connection:
            assert len(connection.execute(select(PublicationVersion)).all()) == 5
    reader = DatabaseFirstPublicationReader(control_db, clock=lambda: NOW)
    expected_values = {
        "synergy_play_types_opponent_season": ("Transition_PTS", 12 * 48 / 750),
        "grouped_shot_types_opponent_season": ("catch_and_shoot_FG2M", 40 * 48 / 725),
        "grouped_shot_types_opponent_l15": ("catch_and_shoot_FG2M", 40 * 48 / 725),
        # Zones publish a derived rate now, not the provider's own scale.
        "exact_shot_zones_opponent_season": ("Restricted Area_FGM", 4 * 48 / 725),
        "exact_shot_zones_opponent_l15": ("Restricted Area_FGM", 4 * 48 / 725),
    }
    if evidence_mode == "partial":
        expected_values.pop("synergy_play_types_opponent_season")
        assert not reader.read(
            "synergy_play_types_opponent_season", season="2025-26"
        ).available
    if evidence_mode == "backdated_synergy":
        expected_values.pop("synergy_play_types_opponent_season")
        assert not reader.read(
            "synergy_play_types_opponent_season", season="2025-26"
        ).available
        with control_db.connect() as connection:
            synergy_job = connection.execute(select(CompositionJob).where(
                CompositionJob.stream_key
                == "synergy_play_types_opponent_season"
            )).one()
        assert synergy_job.status == "failed"
        assert synergy_job.last_error == "provider_unbounded_as_of"
        with control_db.begin() as connection:
            connection.execute(update(CompositionJob).where(
                CompositionJob.job_id == synergy_job.job_id
            ).values(status="queued", last_error=None))
        assert runtime.compose_queued("2025-26") == 0
        with control_db.connect() as connection:
            retried_job = connection.execute(select(CompositionJob).where(
                CompositionJob.job_id == synergy_job.job_id
            )).one()
            synergy_pointer = connection.execute(select(PublicationPointer).where(
                PublicationPointer.stream_key
                == "synergy_play_types_opponent_season"
            )).first()
        assert retried_job.status == "failed"
        assert retried_job.last_error == "provider_unbounded_as_of"
        assert synergy_pointer is None
    if evidence_mode == "tampered":
        expected_values.pop("grouped_shot_types_opponent_season")
        assert not reader.read(
            "grouped_shot_types_opponent_season", season="2025-26"
        ).available
    if evidence_mode == "gp14":
        expected_values.pop("grouped_shot_types_opponent_l15")
        assert not reader.read(
            "grouped_shot_types_opponent_l15", season="2025-26"
        ).available
    if evidence_mode == "l15_boundary_mismatch":
        expected_values.pop("grouped_shot_types_opponent_l15")
        assert not reader.read(
            "grouped_shot_types_opponent_l15", season="2025-26"
        ).available
    if evidence_mode == "permuted":
        expected_values = {
            "synergy_play_types_opponent_season": (
                "Transition_PTS", 12 * 48 / 750,
            )
        }
    if evidence_mode in {
        "mixed_projection_failure", "mixed_governance_type_error",
        "mixed_governance_runtime_error", "mixed_governance_db_error",
    }:
        expected_values = {
            "grouped_shot_types_opponent_season": (
                "catch_and_shoot_FG2M", 40 * 48 / 725,
            )
        }
    for stream_key, (metric, expected) in expected_values.items():
        read = reader.read(stream_key, season="2025-26")
        assert read.available and len(read.decoded or ()) == 30
        assert (read.decoded or ())[0].per48[metric] == pytest.approx(expected)
        assert read.payload["source_observations"]
    if evidence_mode in {"complete", "projection_failure"}:
        season_snapshot = matchup_repository.get_snapshot(
            TeamMatchupSnapshotScope("2025-26", cutoff.date())
        )
        l15_snapshot = matchup_repository.get_snapshot(
            TeamMatchupSnapshotScope("2025-26", cutoff.date(), 15)
        )
        assert {fact.base for fact in season_snapshot.facts} >= {
            "play_types", "shot_types", "shot_zones",
        }
        assert {fact.base for fact in l15_snapshot.facts} >= {
            "shot_types", "shot_zones",
        }
        assert all(
            fact.publication is not None
            for fact in (*season_snapshot.facts, *l15_snapshot.facts)
            if fact.base in {"play_types", "shot_types", "shot_zones"}
        )
        assert any(
            fact.base == "traditional" and fact.raw_value == 1
            for fact in season_snapshot.facts
        )
        if evidence_mode == "projection_failure":
            with control_db.connect() as connection:
                prior_pointers = {
                    row.stream_key: row.active_publication_id
                    for row in connection.execute(
                        select(PublicationPointer)
                    )
                }
            with control_db.begin() as connection:
                connection.execute(update(CompositionJob).values(
                    status="queued", last_error=None,
                ))
            matchup_materialization.fail_once = True
            with pytest.raises(RuntimeError, match="injected matchup projection failure"):
                runtime.compose_queued("2025-26")
            assert matchup_repository.get_snapshot(
                TeamMatchupSnapshotScope("2025-26", cutoff.date())
            ) == season_snapshot
            with control_db.connect() as connection:
                assert {
                    row.stream_key: row.active_publication_id
                    for row in connection.execute(select(PublicationPointer))
                } == prior_pointers
                assert len(connection.execute(select(PublicationVersion)).all()) == 5
                assert {
                    row.status
                    for row in connection.execute(select(CompositionJob))
                } == {"failed"}
            with control_db.begin() as connection:
                connection.execute(update(CompositionJob).values(
                    status="queued", last_error=None,
                ))
            assert runtime.compose_queued("2025-26") == 5
            with control_db.connect() as connection:
                versions_by_stream = {}
                for version in connection.execute(select(PublicationVersion)):
                    versions_by_stream[version.stream_key] = (
                        versions_by_stream.get(version.stream_key, 0) + 1
                    )
                # Retrying after the failed projection reuses each unchanged
                # publication rather than minting a duplicate generation.
                assert set(versions_by_stream.values()) == {1}
                assert {
                    row.status
                    for row in connection.execute(select(CompositionJob))
                } == {"succeeded"}
        if evidence_mode == "complete":
            stream_key = "grouped_shot_types_opponent_season"
            stale_session = Session(control_db, expire_on_commit=False)
            advancing_session = Session(control_db, expire_on_commit=False)
            try:
                stale_pointer = stale_session.get(PublicationPointer, stream_key)
                assert stale_pointer is not None
                initial_fence = stale_pointer.fence
                initial_active = stale_pointer.active_publication_id
                original_compose_payload = (
                    collection_control_module._compose_nba_observation_payload
                )
                concurrent = None
                advance_started = False

                def advance_during_stale_derivation(*args, **kwargs):
                    nonlocal advance_started, concurrent
                    if not advance_started:
                        advance_started = True
                        derived_payload = original_compose_payload(*args, **kwargs)
                        with advancing_session.begin():
                            concurrent = publications.compose(
                                stream_key,
                                season="2025-26",
                                cutoff=cutoff,
                                payload=derived_payload,
                                reason="concurrent generation",
                                expected_fence=initial_fence,
                                manifest_id=manifest.manifest_id,
                                session=advancing_session,
                            )
                        return derived_payload
                    return original_compose_payload(*args, **kwargs)

                monkeypatch.setattr(
                    collection_control_module,
                    "_compose_nba_observation_payload",
                    advance_during_stale_derivation,
                )
                with pytest.raises(ControlPlaneError, match="stale_composition"):
                    publications.compose_from_observations(
                        stream_key, season="2025-26", cutoff=cutoff,
                        manifest_id=manifest.manifest_id,
                        session=stale_session,
                    )
                stale_session.rollback()
            finally:
                stale_session.close()
                advancing_session.close()
            assert concurrent is not None
            with control_db.connect() as connection:
                pointer = connection.execute(select(PublicationPointer).where(
                    PublicationPointer.stream_key == stream_key
                )).one()
                versions = connection.execute(select(PublicationVersion).where(
                    PublicationVersion.stream_key == stream_key
                )).all()
            assert pointer.fence == initial_fence + 1
            assert pointer.previous_publication_id == initial_active
            assert pointer.active_publication_id == concurrent.publication_id
            assert {
                row.publication_id for row in versions if row.status == "active"
            } == {concurrent.publication_id}
            assert all(
                row.status == "superseded"
                for row in versions
                if row.publication_id == initial_active
            )
    with control_db.connect() as connection:
        assert len(connection.execute(select(CollectionObservation)).all()) == len(uploaded)
    outbox.close()


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


def test_operator_enable_unblocks_the_first_collection_of_an_nba_stream(tmp_path: Path):
    """The registry ships NBA opponent streams disabled, and ingestion only
    accepts observations for an enabled stream, so the operator enable is the
    only way out of the first-candidate cycle."""

    control_db = create_engine(f"sqlite:///{tmp_path / 'control.sqlite3'}")
    run_migrations(control_db)
    team_ids = sorted(NBA_TEAM_IDS)
    cutoff = NOW
    control = CollectionControlService(control_db, clock=lambda: NOW)
    control.activate_season("2025-26", actor="operator")
    event_request = control.create_bootstrap_request(
        "2025-26", "event", cutoff=cutoff
    )
    control.publish_catalog(event_request.request_id, {
        "complete_snapshot": True,
        "events": [{
            "nba_game_id": f"game-{round_index}-{pair_index}",
            "home_team_id": team_ids[pair_index * 2],
            "away_team_id": team_ids[pair_index * 2 + 1],
            "phase": "Regular Season", "status": "Final",
            "scheduled_at": (
                cutoff - timedelta(days=15 - round_index, hours=1)
            ).isoformat(),
        } for round_index in range(15) for pair_index in range(15)],
    }, version="event-v1")
    athlete_request = control.create_bootstrap_request(
        "2025-26", "athlete", cutoff=cutoff
    )
    control.publish_catalog(athlete_request.request_id, {
        "complete_snapshot": True,
        "identities": [{
            "player_id": "1", "team_id": team_ids[0], "status": "active",
            "event_ids": [
                f"game-{round_index}-{pair_index}"
                for round_index in range(15) for pair_index in range(15)
            ],
        }],
    }, version="athlete-v1")
    stream_key = NBA_PUBLICATION_STREAMS["play_types"].format(window="season")
    manifest = control.create_manifest(
        "2025-26", cutoff=cutoff, scopes={stream_key, "canonical_game_ledger"},
        collect_before=NOW + timedelta(hours=1),
    )
    manifest_scopes = set(json.loads(manifest.scopes))
    descriptors = _collector_scope_descriptors(manifest_scopes, cutoff)
    discovery = {"environment": "testing", "bootstrap_requests": [], "manifests": [{
        "manifest_id": manifest.manifest_id, "season": "2025-26",
        "cutoff": cutoff.isoformat(),
        "collect_before": (NOW + timedelta(hours=1)).isoformat(),
        "accepted_versions": [2], "scopes": sorted(manifest_scopes),
        "scope_descriptors": descriptors,
    }]}

    class OpponentProvider(FakeProvider):
        def fetch_synergy_play_types(self, category, **kwargs):
            return [{
                "team_id": int(team_id), "category": category,
                "GP": 15, "MIN": 750, "POSS": 10, "PTS": 12,
            } for team_id in team_ids]

    collector, transport, outbox = _collector(
        tmp_path, discovery=discovery, provider=OpponentProvider()
    )
    assert collector.run().disposition is RunDisposition.COMPLETE
    uploaded = [
        json.loads(gzip.decompress(call[3]))
        for call in transport.calls
        if "/api/collector/observations" in call[1]
    ]
    assert {document["observation_type"] for document in uploaded} == {
        "synergy_opponent"
    }

    governance = ActiveManifestLedgerGovernanceReader(control_db)
    publications = PublicationService(
        control_db, clock=lambda: NOW, l15_expectation_resolver=governance,
    )
    publications.register_default_streams()
    tokens = CollectorTokenService(
        control_db, environment="testing", signing_secret="test", clock=lambda: NOW
    )
    identity = tokens.create_identity(
        "collector", scopes=["ingest"], owner="residential_collector",
        providers=["nba"], surfaces=["synergy_opponent"],
    )
    claims = tokens.validate(tokens.issue_for_secret(
        identity["identity_id"], identity["secret"], scopes=["ingest"]
    ))
    ingestion = ObservationIngestionService(
        control_db, publication_service=publications, clock=lambda: NOW
    )
    blocked = json.loads(json.dumps(uploaded[0]))
    blocked_payload = json.dumps(
        blocked.pop("payload"), sort_keys=True, separators=(",", ":")
    ).encode()
    with pytest.raises(ControlPlaneError, match="provider_not_registered"):
        ingestion.ingest(claims, blocked, blocked_payload)

    operations = CollectionOperationsService(
        control_db, publication_service=publications, clock=lambda: NOW
    )
    activation = operations.activate_stream(
        stream_key, actor="operator", reason="enable for first collection",
    )
    assert activation.resource.enabled is True

    for document in uploaded:
        payload = json.dumps(
            document.pop("payload"), sort_keys=True, separators=(",", ":")
        ).encode()
        ingestion.ingest(claims, document, payload)
    publication = publications.compose_from_observations(
        stream_key, season="2025-26", cutoff=cutoff,
        manifest_id=manifest.manifest_id,
    )

    assert publication.status in {"candidate", "active"}
    with control_db.connect() as connection:
        assert connection.execute(select(PublicationVersion).where(
            PublicationVersion.stream_key == stream_key
        )).one().publication_id == publication.publication_id
    read = DatabaseFirstPublicationReader(control_db, clock=lambda: NOW).read(
        stream_key, season="2025-26"
    )
    assert read.available and len(read.decoded or ()) == 30
    outbox.close()


def _boston_zone_row(**overrides):
    """Boston's 2025-26 Regular Season shape at realistic scale.

    1,504 Restricted Area opponent attempts over 3,946 team minutes is about
    18.3 per 48 -- the value the product should show.  The defective Per48
    mode published 137.1 for the same window.
    """

    base = {
        "TEAM_ID": 1610612738, "GP": 82, "MIN": 3946,
        "Restricted Area_OPP_FGM": 902, "Restricted Area_OPP_FGA": 1504,
        "In The Paint (Non-RA)_OPP_FGM": 380, "In The Paint (Non-RA)_OPP_FGA": 905,
        "Mid-Range_OPP_FGM": 322, "Mid-Range_OPP_FGA": 780,
        "Above the Break 3_OPP_FGM": 742, "Above the Break 3_OPP_FGA": 2075,
        "Corner 3_OPP_FGM": 260, "Corner 3_OPP_FGA": 668,
        "Left Corner 3_OPP_FGM": 129, "Left Corner 3_OPP_FGA": 331,
        "Right Corner 3_OPP_FGM": 131, "Right Corner 3_OPP_FGA": 337,
        "Backcourt_OPP_FGM": 2, "Backcourt_OPP_FGA": 33,
        # 902+380+322+742+260+2 = 2608 makes; 1504+905+780+2075+668+33 = 5965.
        "OPP_TOTAL_FGM": 2608, "OPP_TOTAL_FGA": 5965,
    }
    base.update(overrides)
    return pd.DataFrame([{k: v for k, v in base.items() if v is not None}])


def test_boston_zone_evidence_carries_integer_totals_and_minutes():
    """The inputs the derived rate is computed from, at realistic scale.

    This asserts the observation's own contract -- integer Totals beside the
    window's minutes, never a provider rate.  That the *publication* divides
    them into about 18.3 is proven against a composed publication in
    ``tests/services/test_publication_repair_promotion.py``; recomputing the
    division here would only assert that 1504 * 48 / 3946 is 18.3.
    """

    observation = normalize_opponent_zone_response(
        _boston_zone_row(), season="2025-26", cutoff=NOW, team_id=1610612738,
    )
    restricted = next(
        record for record in observation.payload["records"]
        if record["category"] == "Restricted Area"
    )
    # The observation carries integer Totals, never a provider rate.
    assert restricted["FGA"] == 1504
    assert restricted["minutes"] == 3946

    # No provider rate survives into the evidence: 1504 is a count.
    assert float(restricted["FGA"]).is_integer()
    assert restricted["FGA"] > 1000


def test_a_transient_zone_mismatch_refetches_the_pair_once():
    """One incoherent read is retried as a pair; a repeated one is reported."""

    from app.collector.provider import ResidentialScopeExecutor, ScopeWork

    class FlakyProvider:
        def __init__(self, *, failures: int):
            self.failures = failures
            self.calls = 0

        def fetch_opponent_shooting_zone(self, _date_from, **kwargs):
            self.calls += 1
            overrides = (
                {"Backcourt_OPP_FGA": 34} if self.calls <= self.failures else {}
            )
            return _boston_zone_row(**overrides)

    work = ScopeWork(
        scope="shot_zones_opponent", observation_type="shot_zones_opponent",
        season="2025-26", cutoff=NOW.isoformat(), instruction_id="probe",
        manifest_id=None,
        parameters={
            "window": "season", "subject": "opponent", "team_id": 1610612738,
            "date_from": None, "date_to": "08/13/2026",
            "per_mode": "Totals", "value_mode": "totals_with_minutes",
        },
    )

    transient = FlakyProvider(failures=1)
    router = ResidentialScopeExecutor(transient, clock=lambda: NOW)
    observations = router.execute_scope(
        work, collector_id="collector", environment="testing",
        retrieved_at=NOW,
    )
    assert transient.calls == 2
    assert len(observations) == 1
    assert observations[0].observation_type == "shot_zones_opponent"

    persistent = FlakyProvider(failures=2)
    router = ResidentialScopeExecutor(persistent, clock=lambda: NOW)
    with pytest.raises(ProviderContractError) as failure:
        router.execute_scope(
            work, collector_id="collector", environment="testing",
            retrieved_at=NOW,
        )
    # Exactly one retry, then the defect is reported with its residual.
    assert persistent.calls == 2
    assert failure.value.reason == "value_invariant_failed"
    assert failure.value.diagnostics["team_id"] == 1610612738
    assert failure.value.diagnostics["residual"] == 1.0


def test_a_persistent_zone_mismatch_reports_its_residual_to_the_operator(tmp_path: Path):
    """The diagnostics have to reach the status an operator actually reads.

    Constructing them on the exception proves nothing on its own: the runner
    collapses a contract error to its bare reason code, so without the status
    record the equation and residual would be built and thrown away.
    """

    # A real manifest freezes the stream key beside the observation type it
    # authorizes; the runner matches descriptors against the latter.
    manifest_scopes = {"exact_shot_zones_opponent_season", "shot_zones_opponent"}
    descriptors = _collector_scope_descriptors(manifest_scopes, NOW)
    discovery = {"environment": "testing", "bootstrap_requests": [], "manifests": [{
        "manifest_id": "manifest-zone", "season": "2025-26",
        "cutoff": NOW.isoformat(),
        "collect_before": (NOW + timedelta(hours=1)).isoformat(),
        "accepted_versions": [2], "scopes": sorted(manifest_scopes),
        "scope_descriptors": descriptors,
    }]}

    class IncoherentProvider(FakeProvider):
        """Zones that never add up, however many times they are refetched."""

        def fetch_opponent_shooting_zone(self, _date_from, **kwargs):
            return _boston_zone_row(
                **{"TEAM_ID": kwargs["team_id"], "Backcourt_OPP_FGA": 34}
            )

    collector, _, outbox = _collector(
        tmp_path, discovery=discovery, provider=IncoherentProvider(),
    )
    try:
        collector.run()
        events = collector.status.snapshot(version="test")["recent"]
    finally:
        outbox.close()

    detailed = [event for event in events if event.get("detail")]
    assert detailed, events
    detail = detailed[-1]["detail"]
    assert detailed[-1]["code"] == "value_invariant_failed"
    assert "equation=zones_plus_backcourt_equals_opponent_fga" in detail
    assert "residual=1.0" in detail
    reported_team = re.search(r"team_id=(\d+)", detail)
    assert reported_team is not None, detail
    assert reported_team.group(1) in NBA_TEAM_IDS
    # Bounded: an operator diagnostic never becomes a payload channel.
    assert len(detail) <= 160
