"""Distribution boundary tests for the separately installed collector wheel."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import zipfile


def test_collector_wheel_contains_only_standalone_package_and_imports_cleanly(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
         "--wheel-dir", str(tmp_path), str(repository)],
        check=True, capture_output=True, text=True,
    )
    wheel = next(tmp_path.glob("statsplus_residential_collector-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    assert any(name.startswith("statsplus_collector/") for name in names)
    assert not any(name.startswith("app/") for name in names)
    assert not any(part in name for name in names for part in ("/routes/", "/models/", "/services/"))

    script = """
import sys
sys.path.insert(0, sys.argv[1])
import statsplus_collector
for forbidden in ('flask', 'sqlalchemy', 'psycopg', 'psycopg2'):
    assert forbidden not in sys.modules, forbidden
print(statsplus_collector.__name__)
"""
    environment = dict(os.environ)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(wheel)], check=True,
        capture_output=True, text=True, cwd=tmp_path, env=environment,
    )
    assert completed.stdout.strip() == "statsplus_collector"
