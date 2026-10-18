import json
from datetime import UTC
from pathlib import Path

# Railway applies a root railway.json to every service built from this repo, so
# the web settings live in railway.web.json and are set on the statsplus-backend
# service instance; a root railway.json would healthcheck the non-HTTP workers.


def test_railway_runs_migrations_before_starting_application_workers():
    config = json.loads(Path("railway.web.json").read_text(encoding="utf-8"))

    assert config["deploy"]["preDeployCommand"] == "python scripts/migrate.py"


def test_railway_health_checks_the_unauthenticated_health_route():
    deploy = json.loads(Path("railway.web.json").read_text(encoding="utf-8"))["deploy"]

    assert deploy["healthcheckPath"] == "/health"
    assert deploy["healthcheckTimeout"] == 60


def test_no_root_railway_json_applies_web_settings_to_workers():
    assert not Path("railway.json").exists()


def test_ledger_refresh_cron_runs_inside_the_daily_manifest_window():
    deploy = json.loads(Path("railway.ledger.json").read_text(encoding="utf-8"))["deploy"]

    # ledger_refresh.py collects only under an executable manifest, which the
    # player-shooting tick issues for 03:45-11:00 Central (08:45-16:00 UTC in
    # CDT, 09:45-17:00 in CST); every run must land inside both.
    assert deploy["cronSchedule"] == "15 10,12,14 * * *"
    # The rotation step (crf04/statsplus#223) runs after the ledger step
    # whatever the ledger's outcome, and the run fails if either step failed.
    assert deploy["startCommand"] == (
        'sh -c \'python scripts/ledger_refresh.py "$LEDGER_SEASON" '
        '--database-url "$DATABASE_URL" --compose; ledger=$?; '
        'python scripts/rotation_refresh.py "$LEDGER_SEASON" '
        '--database-url "$DATABASE_URL" --max-games 60; rotations=$?; '
        '[ "$ledger" -eq 0 ] && [ "$rotations" -eq 0 ]\''
    )
    assert deploy["preDeployCommand"] == "python scripts/migrate.py"
    assert deploy["restartPolicyType"] == "NEVER"
    assert "healthcheckPath" not in deploy


def test_on_off_warm_cron_follows_the_injury_refresh_cadence():
    deploy = json.loads(Path("railway.on_off_warm.json").read_text(encoding="utf-8"))["deploy"]

    # The injury refresh runs every 30 minutes, 11:00-23:30 Eastern; the warm
    # runs five minutes after each.  Railway cron is UTC: hours 15-23 and 0-4
    # cover 11:05-23:35 in EDT and 10:05-23:35 in EST (extra runs are cheap
    # no-ops because fresh entries and an unchanged report cost no calls).
    assert deploy["cronSchedule"] == "5,35 15-23,0-4 * * *"
    assert deploy["startCommand"] == "python scripts/on_off_warm.py"
    assert deploy["restartPolicyType"] == "NEVER"


def test_injury_refresh_cron_covers_the_eastern_window_in_both_halves_of_the_year():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    deploy = json.loads(Path("railway.injuries.json").read_text(encoding="utf-8"))["deploy"]
    assert deploy["startCommand"] == "python scripts/injury_refresh.py"
    assert deploy["restartPolicyType"] == "NEVER"
    assert deploy["cronSchedule"] == "0,30 15-23,0-4 * * *"
    runs = {(hour, minute) for hour in [*range(15, 24), *range(0, 5)] for minute in (0, 30)}

    eastern = ZoneInfo("America/New_York")
    for day in (datetime(2026, 1, 15), datetime(2026, 7, 15)):
        start = day.replace(hour=11, tzinfo=eastern)
        ticks = [start + timedelta(minutes=30 * step) for step in range(26)]
        assert ticks[-1].hour * 60 + ticks[-1].minute == 23 * 60 + 30
        for tick in ticks:
            utc = tick.astimezone(UTC)
            assert (utc.hour, utc.minute) in runs
