"""A ledger refresh before the morning's collection manifest opens skips cleanly; any other governance failure,
and a missing manifest from noon UTC, still fails."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.services.ledger_runtime import (
    ActiveManifestLedgerGovernanceReader,
    LedgerRuntime,
    NoActiveCollectionManifest,
)
from app.services.team_matchup_publications import PublicationGovernanceUnavailable
from scripts import ledger_refresh
from tests.support.game_context_world import migrated_engine

MORNING = datetime(2026, 11, 10, 10, 15, tzinfo=UTC)
NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _run_at(tmp_path, monkeypatch, at, *flags):
    engine = migrated_engine(tmp_path)
    url = str(engine.url)
    engine.dispose()
    monkeypatch.setattr(ledger_refresh, "_now", lambda: at)
    return ledger_refresh.main(["2026-27", "--database-url", url, *flags])


def _last_json(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_the_morning_run_before_any_manifest_skips_and_exits_cleanly(tmp_path, monkeypatch, capsys):
    assert _run_at(tmp_path, monkeypatch, MORNING) == 0
    assert _last_json(capsys) == {
        "season": "2026-27",
        "skipped": "no_active_manifest",
        "detail": "active manifest and completed Event Catalog governance are required",
    }


def test_a_run_from_noon_without_a_manifest_still_fails(tmp_path, monkeypatch):
    with pytest.raises(NoActiveCollectionManifest):
        _run_at(tmp_path, monkeypatch, NOON)


def test_a_broken_manifest_still_fails_in_the_morning(tmp_path, monkeypatch):
    def corrupt_catalog(self, season):
        raise PublicationGovernanceUnavailable("immutable Event Catalog payload checksum mismatch")

    monkeypatch.setattr(ActiveManifestLedgerGovernanceReader, "read_for_collection", corrupt_catalog)
    with pytest.raises(PublicationGovernanceUnavailable, match="checksum mismatch"):
        _run_at(tmp_path, monkeypatch, MORNING)


def test_an_incomplete_refresh_still_exits_2(tmp_path, monkeypatch):
    @dataclass
    class Incomplete:
        complete: bool = False

    monkeypatch.setattr(LedgerRuntime, "refresh", lambda self, season, **_: Incomplete())
    assert _run_at(tmp_path, monkeypatch, MORNING) == 2


def test_compose_only_composes_without_reading_collection_governance(tmp_path, monkeypatch, capsys):
    assert _run_at(tmp_path, monkeypatch, MORNING, "--compose-only") == 0
    assert _last_json(capsys) == {"season": "2026-27", "composed_jobs": 0}
