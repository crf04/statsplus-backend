"""Store the official NBA injury report for today's games (one cron tick).

Run every 30 minutes by the injury-refresh Railway cron service
(``railway.injuries.json``).  Outside 11:00-19:30 Eastern, or on a date with no
game left to tip, it does nothing.  Needs ``INJURY_REPORT_ENABLED=true`` and
the application's database settings.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.errors import ProviderUnavailableError


def main() -> int:
    from app.config.settings import load_settings
    from app.dependencies import build_dependencies
    from app.utils.json_logging import configure_json_logging

    settings = load_settings()
    configure_json_logging(settings.log_level)
    dependencies = build_dependencies(settings)
    try:
        result = dependencies.injury_refresh_service.run()
    except ProviderUnavailableError as error:
        print(json.dumps({"state": "provider_unavailable", "error": str(error)}), flush=True)
        return 1
    finally:
        dependencies.engine.dispose()
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
