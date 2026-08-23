#!/usr/bin/env python3
"""Keep the dedicated Railway projection collector awake on a five-minute beat."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import collect_projections


def run() -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for projection collection")
    if collect_projections.is_demo_database_url(database_url):
        raise RuntimeError("the read-only demo database cannot collect projections")
    settings = collect_projections.load_settings(
        overrides={"DATABASE_URL": database_url}
    )
    dependencies = collect_projections.build_dependencies(settings)
    coordinator = dependencies.projection_collection_coordinator
    while True:
        try:
            collect_projections.run_once(coordinator)
        except Exception as error:
            print(f"projection collector attempt failed: {error}")
        time.sleep(300)


if __name__ == "__main__":
    run()
