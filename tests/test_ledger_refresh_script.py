"""A ledger refresh before the morning's collection manifest opens skips cleanly; from noon UTC a missing one fails."""

import json
from datetime import UTC, datetime

import pytest

from app.services.team_matchup_publications import PublicationGovernanceUnavailable
from scripts import ledger_refresh
from tests.support.game_context_world import migrated_engine


def _run_at(tmp_path, monkeypatch, at):
    engine = migrated_engine(tmp_path)
    url = str(engine.url)
    engine.dispose()
    monkeypatch.setattr(ledger_refresh, "_now", lambda: at)
    return ledger_refresh.main(["2026-27", "--database-url", url])


def test_the_morning_run_before_any_manifest_skips_and_exits_cleanly(tmp_path, monkeypatch, capsys):
    code = _run_at(tmp_path, monkeypatch, datetime(2026, 11, 10, 10, 15, tzinfo=UTC))

    assert code == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {
        "season": "2026-27",
        "skipped": "no_active_manifest",
        "detail": "active manifest and completed Event Catalog governance are required",
    }


def test_a_run_from_noon_without_a_manifest_still_fails(tmp_path, monkeypatch):
    with pytest.raises(PublicationGovernanceUnavailable):
        _run_at(tmp_path, monkeypatch, datetime(2026, 11, 10, 12, 15, tzinfo=UTC))
