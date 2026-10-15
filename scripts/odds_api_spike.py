#!/usr/bin/env python3
"""Opt-in live spikes for The Odds API (crf04/statsplus#185 section 12).

Needs ``ODDS_API_KEY`` in the environment and spends real credits, so nothing
here runs in the gate.  It prints costs, counts, and times only: never the
key, a URL, or a payload.  Every paid call is capped by ``--max-credits``
(default 100, the parent's spike budget).

  costs     spike 1: bulk /odds does not serve player props, and an empty
            per-event response costs 0 credits (also records the cost of a
            populated one).
  posting   spike 2: record when each allowlisted book first posts props for
            each event in the next 36 hours.  Re-run it hourly over one or
            two slates; state accumulates in ``--state-file``.
  names     spike 4: resolve one event's player names within its two rosters
            against the database's athlete catalog, reporting the unresolved
            names (e.g. a rookie missing from the catalog).

Spike 3 (billing reset day) is read from the Odds API account dashboard and
set as ``ODDS_API_RESET_DAY``; there is no API for it.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.providers.odds_api import (  # noqa: E402
    BOOK_ALLOWLIST,
    OddsApiClient,
    OddsApiError,
)

HORIZON = timedelta(hours=36)


def _client() -> OddsApiClient:
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        raise SystemExit("ODDS_API_KEY is not set")
    return OddsApiClient(key)


class _Budget:
    def __init__(self, limit: int) -> None:
        self.limit, self.spent = limit, 0

    def call(self, client: OddsApiClient, event_id: str):
        if self.spent + 5 > self.limit:
            raise SystemExit(f"credit cap reached ({self.spent}/{self.limit})")
        result = client.event_odds(event_id)
        self.spent += result.cost
        return result


def costs(args) -> int:
    client, budget = _client(), _Budget(args.max_credits)
    now = datetime.now(timezone.utc)
    events = sorted(client.list_events(now, now + HORIZON).events, key=lambda e: e.commence_time)
    print(f"events in 36h: {len(events)} (free call)")
    status, served, quota = client.bulk_props_probe()
    print(f"bulk /odds player_points: http={status} served_props={served} "
          f"remaining={quota.remaining} last={quota.last_cost}")
    if not events:
        print("no events to price; re-run on a game day")
        return 0
    for label, event in (("farthest", events[-1]), ("nearest", events[0])):
        try:
            result = budget.call(client, event.event_id)
        except OddsApiError as error:
            print(f"{label} event odds: {error}")
            continue
        hours = (event.commence_time - now) / timedelta(hours=1)
        print(f"{label} event odds: tip_in_h={hours:.1f} quotes={len(result.parsed.quotes)} "
              f"cost={result.cost} remaining={result.quota.remaining}")
    print(f"credits spent by this run: {budget.spent}")
    return 0


def posting(args) -> int:
    client, budget = _client(), _Budget(args.max_credits)
    path = Path(args.state_file)
    state = json.loads(path.read_text()) if path.exists() else {}
    now = datetime.now(timezone.utc)
    for event in client.list_events(now, now + HORIZON).events:
        record = state.setdefault(event.event_id, {"commence": event.commence_time.isoformat(), "first_seen": {}})
        if len(record["first_seen"]) >= len(BOOK_ALLOWLIST):
            continue
        try:
            result = budget.call(client, event.event_id)
        except OddsApiError as error:
            print(f"{event.event_id}: {error}")
            continue
        for book in {quote.book for quote in result.parsed.quotes}:
            record["first_seen"].setdefault(book, now.isoformat())
    path.write_text(json.dumps(state, indent=1, sort_keys=True))
    leads: dict[str, list[float]] = {}
    for record in state.values():
        tip = datetime.fromisoformat(record["commence"])
        for book, seen in record["first_seen"].items():
            leads.setdefault(book, []).append((tip - datetime.fromisoformat(seen)) / timedelta(hours=1))
    for book in sorted(leads):
        hours = sorted(leads[book])
        print(f"{book}: events={len(hours)} max_lead_h={hours[-1]:.1f} min_lead_h={hours[0]:.1f}")
    print(f"credits spent by this run: {budget.spent}")
    return 0


def names(args) -> int:
    from app.dependencies import build_dependencies
    from app.config.settings import load_settings
    from app.services.odds_identity import OddsEventMapper, RosterNameResolver

    client, budget = _client(), _Budget(args.max_credits)
    settings = load_settings(overrides={"DATABASE_URL": args.database_url})
    deps = build_dependencies(settings)
    season = settings.nba.current_season
    now = datetime.now(timezone.utc)
    listed = client.list_events(now, now + HORIZON).events
    mapped, unresolved = OddsEventMapper(
        resolver=deps.event_resolver, mapping_repository=deps.event_mapping_repository, season=season
    ).map(listed)
    print(f"events: {len(listed)} mapped={len(mapped)} unresolved={len(unresolved)}")
    resolver = RosterNameResolver(
        catalog=deps.athlete_catalog_service,
        mapping_repository=deps.athlete_mapping_repository,
        season=season,
    )
    for item in mapped:
        result = budget.call(client, item.event.event_id)
        posted = {quote.player_name for quote in result.parsed.quotes}
        if not posted:
            continue
        resolved = resolver.resolve(posted, item.team_ids)
        print(f"{item.game_id}: names={len(posted)} resolved={len(resolved)} "
              f"unresolved={sorted(posted - set(resolved))}")
        break
    print(f"credits spent by this run: {budget.spent}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-credits", type=int, default=100)
    sub = parser.add_subparsers(dest="spike", required=True)
    sub.add_parser("costs").set_defaults(run=costs)
    p = sub.add_parser("posting")
    p.add_argument("--state-file", default="odds_posting_state.json")
    p.set_defaults(run=posting)
    p = sub.add_parser("names")
    p.add_argument("--database-url", required=True)
    p.set_defaults(run=names)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
