"""Offline 2025-26 world for the closing-line backfill (#433).

The fixtures in ``tests/fixtures/odds_api`` named ``historical_*`` follow the
shapes the vendor documents for the historical endpoints
(https://the-odds-api.com/liveapi/guides/v4/): the live bodies wrapped in
``timestamp`` / ``previous_timestamp`` / ``next_timestamp`` / ``data``.  None is
a live capture; no key was used.

``build_world`` makes a migrated SQLite database with the season's Event
Catalog (three completed games over two game days), an athlete catalog, and
the box scores of those games.  The catalog is deliberately old (published
three months before the clock), and Austin Reaves is listed with the team he
finished the season on, not the one he played for in the Lakers game.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import create_engine, insert

from app.config.settings import RuntimeSettings
from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog
from app.models.closing_lines import ClosingLine, ClosingLinePurchase
from app.models.player_game_log import PlayerGameLog
from app.providers.odds_api import (
    HistoricalEventsResult,
    HistoricalOddsResponse,
    OddsApiError,
    OddsQuota,
    parse_events,
)
from app.services.athlete_mapping_repository import AthleteMappingRepository
from app.services.closing_line_backfill import (
    BoxScoreNames,
    ClosingLineBackfill,
    ClosingLineReResolve,
    SettledSeasonCatalog,
)
from app.services.closing_lines import ClosingLineStore, PostedLine, encode_posted_lines
from app.services.event_catalog_service import EventCatalogService
from app.services.event_mapping_repository import EventMappingRepository
from app.services.event_resolver import EventResolver
from app.services.odds_identity import OddsEventMapper
from tests.support.odds_world import load

SEASON = "2025-26"
PUBLISHED = datetime(2026, 7, 1, 12, tzinfo=UTC)
#: Three months after the catalog was last published: stale for any live use.
CLOCK = PUBLISHED + timedelta(days=90)

LAL, DEN, GSW, LAC, BOS, NYK = (
    1610612747,
    1610612743,
    1610612744,
    1610612746,
    1610612738,
    1610612752,
)
LEBRON, REAVES, HAYES = 2544, 1630559, 1629637
JOKIC, MURRAY, GORDON = 203999, 1627750, 203932
CURRY, PAYTON, KAWHI = 201939, 1627780, 202695
TATUM, BRUNSON = 1628369, 1628973

DEN_LAL, GSW_LAC, BOS_NYK = "0022500101", "0022500102", "0022500103"
DEN_LAL_EVENT = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
GSW_LAC_EVENT = "b2c3d4e5f60718293a4b5c6d7e8f90a1"
BOS_NYK_EVENT = "c3d4e5f60718293a4b5c6d7e8f90a1b2"

TIPS = {
    DEN_LAL: datetime(2025, 11, 5, 0, 30, tzinfo=UTC),
    GSW_LAC: datetime(2025, 11, 5, 2, 0, tzinfo=UTC),
    # Exactly on a 5-minute mark.
    BOS_NYK: datetime(2025, 11, 6, 0, 0, tzinfo=UTC),
}

#: (game id, home id, home name, tricode, away id, away name, tricode, tip,
#:  status text, status code)
_GAMES = (
    (DEN_LAL, DEN, "Nuggets", "DEN", LAL, "Lakers", "LAL", TIPS[DEN_LAL], "Final", 3),
    (GSW_LAC, GSW, "Warriors", "GSW", LAC, "Clippers", "LAC", TIPS[GSW_LAC], "Final", 3),
    (BOS_NYK, BOS, "Celtics", "BOS", NYK, "Knicks", "NYK", TIPS[BOS_NYK], "Final", 3),
)

#: (player id, display name, team id the catalog lists, tricode)
ATHLETES = (
    (LEBRON, "LeBron James", LAL, "LAL"),
    # Finished the season elsewhere: not on either team of the Lakers game.
    (REAVES, "Austin Reaves", GSW, "GSW"),
    (HAYES, "Jaxson Hayes", LAL, "LAL"),
    (JOKIC, "Nikola Jokić", DEN, "DEN"),
    (MURRAY, "Jamal Murray", DEN, "DEN"),
    (GORDON, "Aaron Gordon", DEN, "DEN"),
    (CURRY, "Stephen Curry", GSW, "GSW"),
    (PAYTON, "Gary Payton II", GSW, "GSW"),
    (KAWHI, "Kawhi Leonard", LAC, "LAC"),
    (TATUM, "Jayson Tatum", BOS, "BOS"),
    (BRUNSON, "Jalen Brunson", NYK, "NYK"),
)

#: (game, player, team, minutes) of the box scores.
BOX_SCORES = (
    (DEN_LAL, LEBRON, LAL, 35.0),
    (DEN_LAL, REAVES, LAL, 30.0),
    # Just under the coverage threshold.
    (DEN_LAL, HAYES, LAL, 19.9),
    (DEN_LAL, JOKIC, DEN, 36.0),
    (DEN_LAL, MURRAY, DEN, 33.0),
    # Exactly on the threshold: counted.
    (DEN_LAL, GORDON, DEN, 20.0),
    (GSW_LAC, CURRY, GSW, 34.0),
    (GSW_LAC, PAYTON, GSW, 22.0),
    (GSW_LAC, KAWHI, LAC, 31.0),
    (BOS_NYK, TATUM, BOS, 36.0),
    (BOS_NYK, BRUNSON, NYK, 38.0),
)


def _event_row(game):
    game_id, home, home_name, home_tri, away, away_name, away_tri, tip, status, code = game
    return {
        "nba_game_id": game_id,
        "season": SEASON,
        "home_team_id": home,
        "home_team_name": home_name,
        "home_team_tricode": home_tri,
        "away_team_id": away,
        "away_team_name": away_name,
        "away_team_tricode": away_tri,
        "scheduled_at": tip,
        "status_text": status,
        "status_code": code,
        "postponed_status": None,
        "postponement_evidence": None,
        "classification": "Regular Season",
    }


def _athlete(player_id, name, team_id, tricode):
    return {
        "season": SEASON,
        "player_id": player_id,
        "display_name": name,
        "roster_status": "active",
        "is_active": True,
        "is_active_for_season": True,
        "team_id": team_id,
        "team_name": tricode,
        "team_abbreviation": tricode,
        "published_at": PUBLISHED,
    }


def _log_row(game_id, player_id, team_id, minutes):
    names = {pid: name for pid, name, _, _ in ATHLETES}
    games = {game[0]: game for game in _GAMES}
    _, home, _, home_tri, away, _, away_tri, tip, _, _ = games[game_id]
    is_home = team_id == home
    return {
        "season": SEASON,
        "season_type": "Regular Season",
        "player_id": player_id,
        "game_id": game_id,
        "player_name": names[player_id],
        "game_date": tip.date(),
        "team_id": team_id,
        "team_tricode": home_tri if is_home else away_tri,
        "opponent_team_id": away if is_home else home,
        "opponent_team_tricode": away_tri if is_home else home_tri,
        "is_home": is_home,
        "minutes": minutes,
        "points": 20,
        "rebounds": 5,
        "assists": 4,
        "field_goals_made": 8,
        "field_goals_attempted": 16,
        "three_pointers_made": 2,
        "three_pointers_attempted": 6,
        "free_throws_made": 2,
        "free_throws_attempted": 2,
        "offensive_rebounds": 1,
        "defensive_rebounds": 4,
        "turnovers": 2,
        "steals": 1,
        "blocks": 0,
        "personal_fouls": 2,
    }


class FakeHistoricalClient:
    """The historical half of ``OddsApiClient`` over the recorded fixtures.

    It answers as the vendor documents: an events list holds the events whose
    ``commence_time`` falls in the requested window and costs 1 credit (0 when
    empty); event odds cost 10 credits (0 when the snapshot posted nothing).
    Every call is recorded so the tests can count them.
    """

    regions = "us"

    def __init__(self, *, remaining: int | None = 100000):
        self.remaining = remaining
        self.credits_spent = 0
        self.quota_calls = 0
        self.event_calls: list[tuple[datetime, datetime, datetime]] = []
        self.odds_calls: list[tuple[str, datetime, tuple[str, ...]]] = []
        self.listed = load("historical_events_nov_4")["data"] + load("historical_events_nov_5")["data"]
        self.bodies = {
            DEN_LAL_EVENT: load("historical_event_odds_den_lal"),
            GSW_LAC_EVENT: load("historical_event_odds_gsw_lac"),
            BOS_NYK_EVENT: load("historical_event_odds_bos_nyk_empty"),
        }
        #: ``{event id: OddsApiError}`` to raise instead of answering.
        self.errors: dict[str, OddsApiError] = {}
        #: Event ids whose paid answer is not JSON (the body arrives as ``None``).
        self.undecodable: set[str] = set()
        #: Raised by every events-list call when set.
        self.events_error: OddsApiError | None = None
        #: Every events list is a charged 200 whose body cannot be read.
        self.events_unreadable = False
        #: ... and the answer also lacks ``x-requests-last``.
        self.events_unreadable_without_cost = False
        #: Extra credits to charge per paid odds call (a plan that is wrong).
        self.surcharge = 0
        self.report_remaining = True
        #: The probe reports the account, but the per-call responses do not
        #: carry ``x-requests-remaining``.
        self.omit_remaining_on_calls = False

    def _quota(self, cost: int) -> OddsQuota:
        self.credits_spent += cost
        if self.remaining is not None:
            self.remaining -= cost
        reported = self.report_remaining and not self.omit_remaining_on_calls
        return OddsQuota(self.remaining if reported else None, None, cost)

    def quota(self) -> OddsQuota:
        self.quota_calls += 1
        return OddsQuota(self.remaining if self.report_remaining else None, None, 0)

    def historical_events(self, date, commence_from, commence_to):
        self.event_calls.append((date, commence_from, commence_to))
        if self.events_error is not None:
            raise self.events_error
        if self.events_unreadable_without_cost:
            return HistoricalEventsResult(
                (), None, OddsQuota(self.remaining, None, None), None, readable=False
            )
        if self.events_unreadable:
            return HistoricalEventsResult((), None, self._quota(1), 1, readable=False)
        events = parse_events(
            [
                item
                for item in self.listed
                if commence_from
                <= datetime.fromisoformat(item["commence_time"].replace("Z", "+00:00"))
                <= commence_to
            ]
        )
        cost = 1 if events else 0
        return HistoricalEventsResult(events, date, self._quota(cost), cost)

    def historical_event_odds(self, event_id, date, *, markets):
        self.odds_calls.append((event_id, date, tuple(markets)))
        if event_id in self.errors:
            raise self.errors[event_id]
        body = self.bodies[event_id]
        paid = bool(body["data"].get("bookmakers"))
        cost = (10 + self.surcharge) if paid else 0
        if event_id in self.undecodable:
            body = None
        return HistoricalOddsResponse(body, self._quota(cost), cost)


#: The snapshot the ledger rows of ``seed_game`` carry.
SNAPSHOT = datetime(2025, 11, 5, 1, 54, 40, tzinfo=UTC)


def seed_game(world, game_id, event_id, posted, *, lines=(), outcome="complete", unresolved=0):
    """One game as the first backfill left it: its quotes, its lines, its ledger row."""

    with world.engine.begin() as connection:
        connection.execute(
            insert(ClosingLinePurchase.__table__).values(
                canonical_game_id=game_id,
                market_category="PTS",
                odds_event_id=event_id,
                requested_for=SNAPSHOT,
                outcome=outcome,
                http_status=200,
                credits=10,
                remaining=50000,
                snapshot_at=SNAPSHOT,
                bought_at=CLOCK,
                quotes=encode_posted_lines([PostedLine(book, name, line) for book, name, line in posted]),
                unresolved_count=unresolved,
            )
        )
        for player_id, book, line in lines:
            connection.execute(
                insert(ClosingLine.__table__).values(
                    canonical_game_id=game_id,
                    canonical_player_id=player_id,
                    market_category="PTS",
                    book=book,
                    line=line,
                    snapshot_at=SNAPSHOT,
                    odds_event_id=event_id,
                    bought_at=CLOCK,
                )
            )


def add_player(world, player_id, name, team_id, game_id, *, minutes=25.0):
    """A synthetic player in the catalog and in one game's box score."""

    with world.engine.begin() as connection:
        connection.execute(
            insert(AthleteCatalog.__table__),
            [_athlete(player_id, name, team_id, "XXX")],
        )
        connection.execute(
            insert(PlayerGameLog.__table__),
            [{**_log_row(game_id, LEBRON, team_id, minutes), "player_id": player_id, "player_name": name}],
        )


def build_world(tmp_path, *, database_url=None):
    url = database_url or f"sqlite:///{tmp_path / 'closing.sqlite3'}"
    engine = create_engine(url)
    run_migrations(engine)
    settings = RuntimeSettings(environment="testing", database={"url": url})
    catalog_service = EventCatalogService(
        engine, nba_stats_provider=object(), settings=settings, clock=lambda: CLOCK
    )
    catalog_service.repository.publish(SEASON, pd.DataFrame([_event_row(game) for game in _GAMES]), PUBLISHED)
    with engine.begin() as connection:
        connection.execute(insert(AthleteCatalog.__table__), [_athlete(*athlete) for athlete in ATHLETES])
        connection.execute(
            insert(PlayerGameLog.__table__),
            [_log_row(*score) for score in BOX_SCORES]
            # Another season's row under a game id that is in the ledger: it
            # must never count toward 2025-26 coverage.
            + [{**_log_row(DEN_LAL, TATUM, BOS, 30.0), "season": "2024-25"}],
        )

    class Catalog:
        def get_catalog(self, season, *, active_only=False):
            from app.services.athlete_catalog_service import _read_catalog_rows

            return _read_catalog_rows(engine, season, active_only=active_only)

    athlete_repository = AthleteMappingRepository(engine)
    event_repository = EventMappingRepository(engine, settings=settings)
    resolver = EventResolver(
        SettledSeasonCatalog(catalog_service.repository),
        mapping_repository=event_repository,
        settings=settings,
    )
    # SQLite has one process here; a paid run on it is otherwise refused.
    store = ClosingLineStore(engine, lock_runs=False)
    world = SimpleNamespace(
        engine=engine,
        url=url,
        settings=settings,
        catalog_service=catalog_service,
        store=store,
        athlete_repository=athlete_repository,
        event_mapper=OddsEventMapper(resolver=resolver, mapping_repository=event_repository, season=SEASON),
        names=BoxScoreNames(engine=engine, catalog=Catalog(), mapping_repository=athlete_repository),
    )

    def backfill(client, *, max_credits=15000, keep_remaining=5000, names=None, clock=None, store=store):
        return ClosingLineBackfill(
            store=store,
            client=client,
            event_mapper=world.event_mapper,
            names=names or world.names,
            events=catalog_service.get_events(SEASON),
            max_credits=max_credits,
            keep_remaining=keep_remaining,
            clock=clock or (lambda: CLOCK),
        )

    def re_resolve(*, dry_run=False, mappings=None):
        """A re-resolve over the world, built as the script builds it."""

        return ClosingLineReResolve(
            store=store,
            events=catalog_service.get_events(SEASON),
            engine=engine,
            catalog=Catalog(),
            mappings=mappings or athlete_repository,
        ).run(dry_run=dry_run)

    world.backfill = backfill
    world.re_resolve = re_resolve
    return world
