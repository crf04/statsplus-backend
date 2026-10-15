import json
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
    assert deploy["startCommand"] == (
        "sh -c 'python scripts/ledger_refresh.py \"$LEDGER_SEASON\" "
        "--database-url \"$DATABASE_URL\" --compose'"
    )
    assert deploy["preDeployCommand"] == "python scripts/migrate.py"
    assert deploy["restartPolicyType"] == "NEVER"
    assert "healthcheckPath" not in deploy
