import json
from pathlib import Path


def test_railway_runs_migrations_before_starting_application_workers():
    config = json.loads(Path("railway.json").read_text(encoding="utf-8"))

    assert config["deploy"]["preDeployCommand"] == "python scripts/migrate.py"
