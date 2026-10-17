"""Warm the on/off (WOWY) cache for a game day's injury-report players (#424).

Reads the injury report that the scheduled refresh stored for the games of the
target day (``--date``, default today in Eastern time; the same listing as
``GET /api/injuries``) and, for every Out, Doubtful or Questionable player,
fetches the two splits an Injury Scenario reads (teammates with him off, and
with him on, the floor) into Redis.  Probable and Available players are not
warmed.

Run it after each scheduled injury refresh (``railway.on_off_warm.json``:
the ``railway.injuries.json`` cadence, offset five minutes).  It is idempotent:
an entry still fresh in the 24-hour cache costs no upstream call, so each run
fetches only players not yet cached, and a run with nothing new makes no calls.
Exits 1 when the report cannot be read or when it could not store what it
fetched (Redis write failure), so the scheduler records the failure.  Needs
``INJURY_REPORT_ENABLED=true``, the application's database settings and
``REDIS_URL``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.errors import ProviderUnavailableError
from app.services.on_off import injury_warm_requests
from app.services.research_season import previous_nba_season

EASTERN = ZoneInfo("America/New_York")


def warm(listing, service, season, target_date, prior_eligible=None):
    """Warm the splits for ``target_date``'s stored injury report.

    ``prior_eligible(row, prior_season)`` says whether a listed player's last
    season joins his scenario window; those splits are warmed too.
    """

    report = listing.list_injuries(target_date.isoformat(), None)
    prior_season = previous_nba_season(season)
    requests = injury_warm_requests(
        report["injuries"],
        season,
        prior_season,
        None if prior_eligible is None else lambda row: prior_eligible(row, prior_season),
    )
    if not requests:
        return {"state": "no_listed_players", "requests": 0}
    return {"state": "warmed", "requests": len(requests), **service.warm(requests)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        help="game day (YYYY-MM-DD, Eastern); defaults to today",
    )
    parser.add_argument(
        "--season",
        help="YYYY-YY season; defaults to the season of the game day's schedule",
    )
    args = parser.parse_args(argv)
    from app.config.settings import load_settings
    from app.dependencies import build_dependencies
    from app.services.research_season import schedule_season
    from app.utils.json_logging import configure_json_logging

    settings = load_settings()
    configure_json_logging(settings.log_level)
    target_date = args.date or datetime.now(timezone.utc).astimezone(EASTERN).date()
    dependencies = build_dependencies(settings)
    try:
        season = args.season or schedule_season(settings, dependencies.event_catalog_service, target_date)
        result = warm(
            dependencies.injury_listing_service,
            dependencies.on_off_service,
            season,
            target_date,
            getattr(
                getattr(dependencies, "injury_scenario_service", None),
                "prior_season_eligible",
                None,
            ),
        )
    except ProviderUnavailableError as error:
        print(json.dumps({"state": "injury_report_unavailable", "error": str(error)}), flush=True)
        return 1
    finally:
        dependencies.engine.dispose()
    print(json.dumps(result, sort_keys=True), flush=True)
    return 1 if result.get("cache_write_failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
