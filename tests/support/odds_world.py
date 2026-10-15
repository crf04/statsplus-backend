"""Offline Odds API world for the sportsbook tests (#377).

The recorded fixtures in ``tests/fixtures/odds_api`` follow the shape of The
Odds API v4 guide.  ``world`` builds a migrated SQLite database holding a
2026-27 Event Catalog (Lakers at Nuggets, Warriors home vs Clippers) and an
athlete catalog, plus the real resolvers over them.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from requests.structures import CaseInsensitiveDict
from sqlalchemy import create_engine, insert

from app.config.settings import OddsApiSettings, RuntimeSettings
from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog
from app.providers.odds_api import (
    EventOddsResult,
    EventsResult,
    OddsApiError,
    OddsQuota,
    parse_event_odds,
    parse_events,
)
from app.services.athlete_mapping_repository import AthleteMappingRepository
from app.services.event_catalog_service import EventCatalogService
from app.services.event_mapping_repository import EventMappingRepository
from app.services.event_resolver import EventResolver
from app.services.odds_collection import OddsCollectionCoordinator
from app.services.odds_identity import OddsEventMapper, RosterNameResolver

FIXTURES = Path(__file__).parents[1] / "fixtures" / "odds_api"
SEASON = "2026-27"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
TIP = datetime(2026, 10, 22, 0, 30, tzinfo=timezone.utc)
GAME_ID = "0022600001"
EVENT_ID = "9e6c2f0a1b7d4c3e8a5f6b7c8d9e0f12"
LAL, DEN, GSW, LAC = 1610612747, 1610612743, 1610612744, 1610612746
LEBRON, REAVES, JOKIC, MURRAY, CURRY = 2544, 1630559, 203999, 1627750, 201939


def load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = CaseInsensitiveDict(headers or {})

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def quota_headers(remaining=19995, used=5, last=5):
    return {
        "x-requests-remaining": str(remaining),
        "x-requests-used": str(used),
        "x-requests-last": str(last),
    }


class FakeClient:
    """Stands in for ``OddsApiClient`` using the real parsers on fixtures."""

    def __init__(self, *, events=None, odds=None, remaining=20000):
        # Default: the Lakers game only.  ``load("events")`` has all three.
        self.events = load("events")[:1] if events is None else events
        self.odds = load("event_odds_five_markets") if odds is None else odds
        self.remaining = remaining
        self.event_calls: list[str] = []
        self.list_calls = 0
        self.error: OddsApiError | None = None
        self.odds_error: OddsApiError | None = None
        self.discovery_quota = OddsQuota()
        #: Called after discovery / after a paid response is built, to move a
        #: test clock or run another worker while this call is "in flight".
        self.on_list = None
        self.on_odds = None

    def list_events(self, commence_from, commence_to):
        self.list_calls += 1
        if self.error is not None:
            raise self.error
        result = EventsResult(parse_events(self.events), self.discovery_quota)
        if self.on_list is not None:
            self.on_list()
        return result

    def event_odds(self, event_id):
        self.event_calls.append(event_id)
        if self.odds_error is not None:
            raise self.odds_error
        # The recorded body is the Lakers game's; any other event is empty.
        parsed = parse_event_odds(
            self.odds if event_id == EVENT_ID else load("event_odds_empty")
        )
        cost = 5 if parsed.quotes else 0
        self.remaining -= cost
        result = EventOddsResult(
            parsed, OddsQuota(self.remaining, 20000 - self.remaining, cost), cost
        )
        if self.on_odds is not None:
            self.on_odds()
        return result


def _athlete(season, player_id, name, team_id, abbreviation):
    return {
        "season": season,
        "player_id": player_id,
        "display_name": name,
        "roster_status": "active",
        "is_active": True,
        "is_active_for_season": True,
        "team_id": team_id,
        "team_name": abbreviation,
        "team_abbreviation": abbreviation,
        "published_at": NOW,
    }


ATHLETES = (
    (LEBRON, "LeBron James", LAL, "LAL"),
    (REAVES, "Austin Reaves", LAL, "LAL"),
    (JOKIC, "Nikola Jokić", DEN, "DEN"),
    (MURRAY, "Jamal Murray", DEN, "DEN"),
    (CURRY, "Stephen Curry", GSW, "GSW"),
)


def event_row(game_id, home_id, home, home_tri, away_id, away, away_tri, tip):
    return {
        "nba_game_id": game_id,
        "season": SEASON,
        "home_team_id": home_id,
        "home_team_name": home,
        "home_team_tricode": home_tri,
        "away_team_id": away_id,
        "away_team_name": away,
        "away_team_tricode": away_tri,
        "scheduled_at": tip,
        "status_text": "Scheduled",
        "status_code": 1,
        "postponed_status": None,
        "postponement_evidence": None,
        "classification": "Regular Season",
    }


def build_world(tmp_path, *, clock_holder=None, athletes=ATHLETES):
    """Return a namespace with engine, resolvers, and a coordinator factory."""

    url = f"sqlite:///{tmp_path / 'odds.sqlite3'}"
    engine = create_engine(url)
    run_migrations(engine)
    settings = RuntimeSettings(environment="testing", database={"url": url})
    catalog_service = EventCatalogService(
        engine, nba_stats_provider=object(), settings=settings, clock=lambda: NOW
    )
    catalog_service.repository.publish(
        SEASON,
        pd.DataFrame(
            [
                event_row(
                    GAME_ID, DEN, "Nuggets", "DEN", LAL, "Lakers", "LAL", TIP
                ),
                event_row(
                    "0022600002", GSW, "Warriors", "GSW", LAC, "Clippers", "LAC",
                    datetime(2026, 10, 22, 2, 0, tzinfo=timezone.utc),
                ),
            ]
        ),
        NOW,
    )
    with engine.begin() as connection:
        connection.execute(
            insert(AthleteCatalog.__table__),
            [_athlete(SEASON, *athlete) for athlete in athletes],
        )

    class Catalog:
        def get_catalog(self, season, *, active_only=False):
            from app.services.athlete_catalog_service import _read_catalog_rows

            return _read_catalog_rows(engine, season, active_only=active_only)

    athlete_repository = AthleteMappingRepository(engine)
    event_repository = EventMappingRepository(engine, settings=settings)
    resolver = EventResolver(
        catalog_service, mapping_repository=event_repository, settings=settings
    )
    world = SimpleNamespace(
        engine=engine,
        settings=settings,
        catalog_service=catalog_service,
        athlete_catalog=Catalog(),
        athlete_repository=athlete_repository,
        event_mapper=OddsEventMapper(
            resolver=resolver, mapping_repository=event_repository, season=SEASON
        ),
        name_resolver=RosterNameResolver(
            catalog=Catalog(), mapping_repository=athlete_repository, season=SEASON
        ),
        clock=clock_holder or [NOW],
    )

    def coordinator(client, *, odds_settings=None, notify=None):
        return OddsCollectionCoordinator(
            engine,
            client=client,
            event_mapper=world.event_mapper,
            name_resolver=world.name_resolver,
            events_between=catalog_service.get_events_between,
            season=SEASON,
            settings=odds_settings or OddsApiSettings(enabled=True, api_key="k" * 24),
            clock=lambda: world.clock[0],
            notify=notify or (lambda level, message: None),
        )

    world.coordinator = coordinator
    return world
