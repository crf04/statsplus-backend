#!/usr/bin/env python3
"""Run the separately installable pull-only residential Collector.

This entry point imports only ``app.collector``; it does not create a Flask
application, import routes, or read a product database URL.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
