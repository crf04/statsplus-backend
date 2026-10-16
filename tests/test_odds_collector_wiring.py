"""The odds beat is its own cron service, not part of the DFS loop (#377)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import sentry_sdk
from sentry_sdk.transport import Transport

import pytest

from app.config.settings import OddsApiSettings, load_settings
from app.services import odds_collection
from app.utils import sentry_setup
from scripts import collect_odds, projection_collection_service

ROOT = Path(__file__).resolve().parents[1]


def test_the_projection_collector_loop_no_longer_references_odds():
    source = (ROOT / "scripts" / "projection_collection_service.py").read_text()

    assert "odds" not in source.lower()
    assert not hasattr(projection_collection_service, "odds_collection")


def test_the_odds_service_is_a_five_minute_cron_running_the_standalone_script():
    deploy = json.loads((ROOT / "railway.odds.json").read_text())["deploy"]

    assert deploy == {
        "startCommand": "python scripts/collect_odds.py",
        "cronSchedule": "*/5 * * * *",
        "restartPolicyType": "NEVER",
    }
    assert (ROOT / "scripts" / "collect_odds.py").is_file()


def test_the_script_reads_the_database_url_from_the_environment(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://odds.example/statsplus")
    seen = {}
    settings = SimpleNamespace(odds_api=OddsApiSettings(), log_level="INFO")
    monkeypatch.setattr(
        collect_odds, "load_settings", lambda **kw: seen.update(kw) or settings
    )
    monkeypatch.setattr(collect_odds, "build_dependencies", lambda _s: object())

    assert collect_odds.main([]) == 0

    assert seen == {"overrides": {"DATABASE_URL": "postgresql://odds.example/statsplus"}}
    assert capsys.readouterr().out == "no_work: ODDS_API_ENABLED is false\n"


def test_enabled_without_a_key_exits_nonzero_and_says_so(monkeypatch, capsys):
    settings = SimpleNamespace(odds_api=OddsApiSettings(enabled=True), log_level="INFO")
    monkeypatch.setattr(collect_odds, "load_settings", lambda **_kw: settings)
    monkeypatch.setattr(collect_odds, "build_dependencies", lambda _s: object())

    code = collect_odds.main(["--database-url", "postgresql://collector.example/statsplus"])

    assert code == 1
    assert capsys.readouterr().out == "failed: ODDS_API_ENABLED is true but ODDS_API_KEY is not set\n"


def test_sentry_is_initialized_so_the_guard_alerts_are_delivered(monkeypatch):
    events = []

    class Capture(Transport):
        def capture_envelope(self, envelope):
            event = envelope.get_event()
            if event:
                events.append(event)

    real_init = sentry_setup.init_sentry
    monkeypatch.setenv("SENTRY_DSN", "https://public@o0.ingest.sentry.io/1")
    monkeypatch.setattr(
        collect_odds, "init_sentry", lambda: real_init(transport=Capture())
    )
    coordinator = SimpleNamespace(
        settings=SimpleNamespace(api_key=None),
        run=lambda: odds_collection._default_notify(
            logging.ERROR, "Odds API credits below 10% of plan (remaining=1500)"
        )
        or SimpleNamespace(
            status="complete", reason="collected", tier="normal", calls=0, remaining=1500,
            credits=0, unresolved_events=0, unresolved_players=0,
        ),
    )
    monkeypatch.setattr(
        odds_collection, "build_odds_collection", lambda deps, cfg: coordinator
    )
    monkeypatch.setattr(
        collect_odds, "load_settings", lambda **_kw: SimpleNamespace(log_level="INFO")
    )
    monkeypatch.setattr(collect_odds, "build_dependencies", lambda _s: object())
    try:
        assert collect_odds.main(["--database-url", "postgresql://x.example/db"]) == 0
    finally:
        sentry_sdk.get_client().close()

    assert [e["message"] for e in events if e.get("message")] == [
        "Odds API credits below 10% of plan (remaining=1500)"
    ]


def test_cron_logs_show_the_cycle_line_and_the_summary_carries_remaining_credits(
    monkeypatch, capsys
):
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    root.handlers[:] = []
    root.setLevel(logging.WARNING)  # what a bare cron process would have
    coordinator = SimpleNamespace(
        settings=SimpleNamespace(api_key=None),
        run=lambda: logging.getLogger("app.services.odds_collection").info(
            "Odds API cycle tier=normal calls=2 credits=10 remaining=4000"
        )
        or SimpleNamespace(
            status="complete", reason="collected", tier="normal", calls=2,
            credits=10, unresolved_events=0, unresolved_players=0, remaining=4000,
        ),
    )
    monkeypatch.setattr(odds_collection, "build_odds_collection", lambda d, c: coordinator)
    monkeypatch.setattr(
        collect_odds, "load_settings", lambda **_kw: SimpleNamespace(log_level="INFO")
    )
    monkeypatch.setattr(collect_odds, "build_dependencies", lambda _s: object())
    try:
        assert collect_odds.main(["--database-url", "postgresql://x.example/db"]) == 0
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])

    out = capsys.readouterr().out
    assert '"message": "Odds API cycle tier=normal calls=2 credits=10 remaining=4000"' in out
    assert (
        "complete: collected tier=normal calls=2 credits=10 remaining=4000 "
        "unresolved_events=0 unresolved_players=0"
    ) in out


def test_the_dfs_feed_needs_the_projection_archive_the_demo_database_lacks():
    from app.services.odds_collection import build_odds_collection

    settings = load_settings(
        overrides={
            "DATABASE_URL": "sqlite:///x.db",
            "ODDS_API_ENABLED": "true",
            "ODDS_API_KEY": "abc123",
        }
    )
    dependencies = SimpleNamespace(
        event_catalog_service=SimpleNamespace(engine=object()),
        projection_archive=None,
        statistic_catalog=None,
    )

    with pytest.raises(RuntimeError, match="needs the projection archive"):
        build_odds_collection(dependencies, settings)


def test_the_built_coordinator_requests_the_configured_region():
    from app.services.odds_collection import build_odds_collection
    from app.services.statistic_catalog import StatisticCatalog

    def build(regions, archive):
        settings = load_settings(
            overrides={
                "DATABASE_URL": "sqlite:///x.db",
                "ODDS_API_ENABLED": "true",
                "ODDS_API_KEY": "abc123",
                "ODDS_API_REGIONS": regions,
            }
        )
        dependencies = SimpleNamespace(
            event_catalog_service=SimpleNamespace(
                engine=object(), get_events_between=lambda *a: []
            ),
            projection_archive=archive,
            statistic_catalog=StatisticCatalog.load_default(),
            event_resolver=object(),
            event_mapping_repository=None,
            athlete_catalog_service=object(),
            athlete_mapping_repository=None,
        )
        return build_odds_collection(dependencies, settings)

    dfs = build("us_dfs", archive=object())
    assert dfs.client._regions == "us_dfs"
    assert dfs.dfs_writer is not None
    assert dfs.dfs_writer.recorder.scopes.keys() == {"prizepicks"}

    sportsbook = build("us", archive=None)  # the sportsbook feed needs no archive
    assert sportsbook.client._regions == "us"
    assert sportsbook.dfs_writer is None


def test_production_wiring_serves_a_recorded_prizepicks_line_for_ninety_minutes(
    monkeypatch, tmp_path
):
    """The writer ``build_odds_collection`` builds and the reader
    ``build_dependencies`` builds, over one database, without ``prizepicks``
    in ``DFS_ENABLED_PROVIDERS``."""

    from datetime import datetime, timedelta, timezone
    from unittest.mock import Mock

    from sqlalchemy import create_engine

    from app.dependencies import build_dependencies
    from app.migrations import run_migrations
    from app.providers.odds_api import BookQuote, OddsEvent
    from app.services.odds_collection import _build_dfs_writer
    from app.services.odds_identity import MappedEvent, ResolvedAthlete

    engine = create_engine(f"sqlite:///{tmp_path / 'wiring.sqlite3'}")
    run_migrations(engine)
    monkeypatch.setattr("app.utils.db.get_engine", Mock(return_value=engine))
    monkeypatch.setattr("app.utils.cache_config.get_redis_client", Mock(return_value=None))
    settings = load_settings(
        environ={
            "FLASK_ENV": "testing",
            "FIREBASE_ADMIN_DISABLED": "true",
            "DATABASE_URL": "postgresql://statsplus.example/db",
            "DFS_ENABLED_PROVIDERS": "underdog",
            "PROJECTION_ARCHIVE_READ_PROVIDER": "underdog",
            "PROJECTION_ARCHIVE_READ_ENABLED": "true",
            "NBA_CURRENT_SEASON": "2026-27",
        }
    )
    dependencies = build_dependencies(settings)
    assert "prizepicks" not in settings.providers.dfs_enabled_providers

    polled = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    tip = datetime(2026, 10, 22, 0, 30, tzinfo=timezone.utc)
    lal, den = 1610612747, 1610612743
    _build_dfs_writer(dependencies, "2026-27").record(
        MappedEvent(
            event=OddsEvent("evt-1", tip, "Denver Nuggets", "Los Angeles Lakers"),
            game_id="0022600001",
            team_ids=(den, lal),
            scheduled_at=tip,
        ),
        [BookQuote("prizepicks", "player_points", "PTS", "LeBron James", 27.5, -119, -119, None)],
        {"LeBron James": ResolvedAthlete(2544, lal, "LeBron James")},
        retrieved_at=polled,
        accepted_at=polled,
        poll_started_at=polled,
    )

    reader = dependencies.projection_player_pool_reader

    def lines_after(minutes):
        reader.clock = lambda: polled + timedelta(minutes=minutes)
        pool = reader.get_pool_for_game(season="2026-27", game_id="0022600001")
        return [dict(player.lines.get("prizepicks", {})) for player in pool.players]

    assert lines_after(60) == [{"PTS": 27.5}]
    assert lines_after(90) == [{"PTS": 27.5}]
    assert lines_after(91) == []
