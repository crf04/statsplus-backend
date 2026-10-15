"""The odds beat is its own cron service, not part of the DFS loop (#377)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import sentry_sdk
from sentry_sdk.transport import Transport

from app.config.settings import OddsApiSettings
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
