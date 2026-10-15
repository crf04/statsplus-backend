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
