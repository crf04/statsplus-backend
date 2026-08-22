#!/usr/bin/env python3
"""Keep the dedicated Railway projection collector awake on a five-minute beat."""

from __future__ import annotations

import os
import time

from scripts.collect_projections import main


def run() -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for projection collection")
    while True:
        try:
            main(["--database-url", database_url])
        except Exception as error:
            print(f"projection collector attempt failed: {error}")
        time.sleep(300)


if __name__ == "__main__":
    run()
