"""PrizePicks lines from The Odds API's ``us_dfs`` region (crf04/statsplus#215).

One collector beat runs over a migrated SQLite database with the real
resolvers, the real projection archive and the real Player Pool reader; only
the Odds API transport is a fake serving the ``us_dfs`` fixture, which is
built from the vendor's documented shape (no live key existed).
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.projection_archive import (
    ClosingProjectionSet,
    LatestPlayerProjection,
    ProjectionObservation,
    ProviderPoll,
)
from app.models.sportsbook_odds import (
    OddsApiPoll,
    OddsApiQuotaState,
    SportsbookPropLatest,
)
from app.providers.dfs import (
    AthleteEvidence,
    EventEvidence,
    MarketThreshold,
    NBAMarketQuery,
    PlayerProjectionMarket,
    Selection,
    SelectionDirection,
    StatisticEvidence,
    TeamEvidence,
    _build_snapshot,
)
from app.providers.odds_api import BookQuote, OddsApiClient, OddsEvent
from app.providers.registry import EntryPayoutTable
from app.services.odds_dfs_projection import build_snapshot
from app.services.odds_identity import MappedEvent, ResolvedAthlete
from app.services.projection_archive import LatestOffering, ProjectionArchive
from app.services.statistic_catalog import StatisticCatalog, StatisticResolver
from tests.support.odds_world import (
    DEN,
    EVENT_ID,
    GAME_ID,
    JOKIC,
    LAL,
    LEBRON,
    MURRAY,
    NOW,
    REAVES,
    SEASON,
    TIP,
    FakeClient,
    FakeResponse,
    build_world,
    event_row,
    load,
    quota_headers,
)

PP_LEBRON = {"PTS": 27.5, "REB": 7.5, "AST": 7.5, "3PM": 2.5, "PRA": 42.5}


@pytest.fixture
def world(tmp_path):
    return build_world(tmp_path)


def _rows(world, model):
    with world.engine.connect() as connection:
        return connection.execute(select(model.__table__)).mappings().all()


def _players(pool):
    return {player.canonical_player_id: player for player in pool.players}


def _dfs_client(**kwargs):
    return FakeClient(regions="us_dfs", **kwargs)


# -- the outcome: lines the Matchup serves under the key the app marks "PP" --


def test_a_beat_leaves_prizepicks_lines_in_the_pool_under_provider_prizepicks(world):
    client = _dfs_client()
    # The paid response arrives seven seconds into the beat.
    client.on_odds = lambda: world.clock.__setitem__(0, NOW + timedelta(seconds=7))

    result = world.dfs_coordinator(client).run()

    assert (result.status, result.calls, result.credits, result.remaining) == (
        "complete",
        1,
        5,
        19995,
    )
    pool = world.pool_reader().get_pool_for_game(season=SEASON, game_id=GAME_ID)
    players = _players(pool)
    assert {pid: (p.name, p.team_id) for pid, p in players.items()} == {
        LEBRON: ("LeBron James", LAL),
        REAVES: ("Austin Reaves", LAL),
        JOKIC: ("Nikola Jokić", 1610612743),
        MURRAY: ("Jamal Murray", 1610612743),
    }
    assert dict(players[LEBRON].lines["prizepicks"]) == PP_LEBRON
    assert players[LEBRON].provenance == {"prizepicks": ("3PM", "AST", "PRA", "PTS", "REB")}
    assert {pid: dict(p.lines["prizepicks"]) for pid, p in players.items() if pid != LEBRON} == {
        REAVES: {"PTS": 15.5},
        JOKIC: {"PTS": 26.5, "REB": 12.5, "AST": 9.5},
        MURRAY: {"PTS": 21.5},
    }
    # Only the provider key the frontend already renders as "PP".
    assert {provider for p in players.values() for provider in p.lines} == {"prizepicks"}
    # The line is as old as the response that carried it, not as the beat.
    received = (NOW + timedelta(seconds=7)).isoformat()
    assert pool.freshness["providers"] == {"prizepicks": {"status": "fresh", "retrieved_at": received}}


def test_the_other_dfs_apps_in_the_same_response_are_never_stored(world):
    world.dfs_coordinator(_dfs_client()).run()

    pool = world.pool_reader().get_pool_for_game(season=SEASON, game_id=GAME_ID)
    lebron = _players(pool)[LEBRON]
    # Underdog and Dabble post 27.5 and Pick6 28.5 for LeBron in the response.
    assert set(lebron.lines) == {"prizepicks"}
    assert dict(lebron.lines["prizepicks"])["PTS"] == 27.5
    assert lebron.line_conflicts == {}
    providers = {row["provider"] for row in _rows(world, LatestPlayerProjection)} | {
        row["provider"] for row in _rows(world, ProjectionObservation)
    }
    assert providers == {"prizepicks"}
    assert _rows(world, SportsbookPropLatest) == []


def test_demon_and_goblin_alternate_lines_never_become_lines(world):
    body = load("event_odds_us_dfs")
    alternates = [
        market["key"] for market in body["bookmakers"][0]["markets"] if market["key"].endswith("_alternate")
    ]
    assert alternates == ["player_points_alternate"]  # the fixture carries them

    world.dfs_coordinator(_dfs_client()).run()

    pool = world.pool_reader().get_pool_for_game(season=SEASON, game_id=GAME_ID)
    lebron = _players(pool)[LEBRON]
    assert lebron.lines["prizepicks"]["PTS"] == 27.5  # not the 22.5 goblin or 32.5 demon
    assert lebron.line_conflicts == {}
    lines = {float(row["line_value"]) for row in _rows(world, ProjectionObservation)}
    assert not {22.5, 32.5} & lines


def test_names_that_are_not_on_the_games_rosters_are_counted_and_skipped(world):
    result = world.dfs_coordinator(_dfs_client()).run()

    assert result.unresolved_players == 2  # Curry (other game) and the rookie
    pool = world.pool_reader().get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert "Stephen Curry" not in {player.name for player in pool.players}
    (poll,) = _rows(world, OddsApiPoll)
    assert (poll["outcome"], poll["cost"], poll["remaining"], poll["unresolved_count"]) == (
        "complete",
        5,
        19995,
        2,
    )
    # 5 LeBron + 3 Jokic + 1 Reaves + 1 Murray markets reached the archive.
    assert poll["quote_count"] == 10


def test_both_sides_are_priced_from_the_registry_payout_not_the_response(world):
    body = load("event_odds_us_dfs")
    for outcome in body["bookmakers"][0]["markets"][0]["outcomes"]:
        outcome["price"] = -250  # the response's "default odds" mean nothing
    world.dfs_coordinator(_dfs_client(odds=body)).run()

    observations = _rows(world, ProjectionObservation)
    assert {
        (row["price_kind"], row["price_value"], row["price_scope"], row["targetable"])
        for row in observations
        if row["targetable"]
    } == {("multiplier", "3", "entry", True)}


class _Transport:
    """A fake ``requests`` session that serves the recorded bodies and keeps
    every request the beat makes."""

    def __init__(self):
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append((url.rsplit("/v4/sports/basketball_nba", 1)[1], dict(params or {})))
        if url.endswith("/events"):
            return FakeResponse(200, load("events")[:1], quota_headers(20000, 0, 0))
        return FakeResponse(200, load("event_odds_us_dfs"), quota_headers(19995, 5, 5))


def test_the_beat_over_the_real_client_requests_only_us_dfs_and_no_alternate_market(world):
    transport = _Transport()
    client = OddsApiClient("k" * 24, session=transport, regions="us_dfs")

    result = world.dfs_coordinator(client).run()

    assert (result.status, result.calls, result.credits) == ("complete", 1, 5)
    paths = [path for path, _ in transport.requests]
    assert paths == ["/events", f"/events/{EVENT_ID}/odds"]
    (_, discovery), (_, odds) = transport.requests
    assert "regions" not in discovery
    assert odds["regions"] == "us_dfs"
    assert odds["markets"].split(",") == [
        "player_points",
        "player_rebounds",
        "player_assists",
        "player_threes",
        "player_points_rebounds_assists",
    ]
    assert not any("alternate" in str(value) for value in odds.values())
    pool = world.pool_reader().get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert dict(_players(pool)[LEBRON].lines["prizepicks"]) == PP_LEBRON


# -- credits -----------------------------------------------------------------


def test_every_paid_poll_logs_its_cost_and_the_remaining_quota(world):
    messages = []
    coordinator = world.dfs_coordinator(
        _dfs_client(), notify=lambda level, message: messages.append((level, message))
    )

    coordinator.run()

    assert (
        logging.INFO,
        "Odds API poll region=us_dfs game=0022600001 kind=hourly cost=5 "
        "remaining=19995 quotes=10 unresolved_players=2",
    ) in messages
    assert (
        logging.INFO,
        "Odds API cycle tier=normal calls=1 credits=5 remaining=19995 "
        "unresolved_events=0 unresolved_players=2",
    ) in messages


def test_the_cadence_is_hourly_and_the_second_poll_does_not_duplicate_markets(world):
    client = _dfs_client()
    coordinator = world.dfs_coordinator(client)
    coordinator.run()

    world.clock[0] = NOW + timedelta(minutes=5)
    assert coordinator.run().calls == 0

    world.clock[0] = NOW + timedelta(hours=1)
    second = coordinator.run()

    assert (second.status, second.calls, second.credits) == ("complete", 1, 5)
    assert len(_rows(world, LatestPlayerProjection)) == 10
    assert len(_rows(world, OddsApiPoll)) == 2


# -- freshness: an hourly poll is live for 90 minutes, others for 15 ---------


def test_a_prizepicks_line_is_live_for_ninety_minutes_after_the_poll_and_no_longer(world):
    world.dfs_coordinator(_dfs_client()).run()
    reader = world.pool_reader()

    def players_at(minutes):
        world.clock[0] = NOW + timedelta(minutes=minutes)
        return reader.get_pool_for_game(season=SEASON, game_id=GAME_ID).players

    assert len(players_at(14)) == 4
    assert len(players_at(45)) == 4  # beyond the 15 minutes every other provider gets
    assert len(players_at(90)) == 4  # the inclusive endpoint
    assert players_at(91) == ()


# -- one slate, several games -------------------------------------------------


def _second_game_body():
    body = deepcopy(load("event_odds_us_dfs"))
    body.update(
        id="1f2e3d4c5b6a79880a1b2c3d4e5f6071",
        commence_time="2026-10-22T02:00:00Z",
        home_team="Golden State Warriors",
        away_team="LA Clippers",
    )
    pp = body["bookmakers"][0]
    pp["markets"] = [
        {
            "key": "player_points",
            "last_update": "2026-10-21T21:50:00Z",
            "outcomes": [
                {"name": "Over", "description": "Stephen Curry", "price": -119, "point": 29.5},
                {"name": "Under", "description": "Stephen Curry", "price": -119, "point": 29.5},
            ],
        }
    ]
    body["bookmakers"] = [pp]
    return body


def test_a_second_games_poll_confirms_its_own_lines_and_leaves_the_first_games_alone(world):
    client = _dfs_client(
        events=load("events")[:2],
        odds_by_event={"1f2e3d4c5b6a79880a1b2c3d4e5f6071": _second_game_body()},
    )
    # Each paid response is received a second after the one before, as it is
    # in production; two snapshots of one provider cannot share an instant.
    client.on_odds = lambda: world.clock.__setitem__(0, world.clock[0] + timedelta(seconds=1))
    world.clock[0] = NOW + timedelta(hours=2)  # both tips inside the 13-hour window
    coordinator = world.dfs_coordinator(client)

    result = coordinator.run()

    assert (result.calls, result.credits) == (2, 10)
    reader = world.pool_reader()
    first = reader.get_pool_for_game(season=SEASON, game_id=GAME_ID)
    second = reader.get_pool_for_game(season=SEASON, game_id="0022600002")
    assert dict(_players(first)[LEBRON].lines["prizepicks"]) == PP_LEBRON
    assert {p.name: dict(p.lines["prizepicks"]) for p in second.players} == {"Stephen Curry": {"PTS": 29.5}}
    # The Partial snapshot of the second game did not retire the first's lines.
    assert len(first.players) == 4
    both = reader.get_pool(season=SEASON, game_ids=[GAME_ID, "0022600002"])
    assert len(both.players) == 5


def test_a_later_poll_replaces_a_moved_line_without_touching_the_rest(world):
    coordinator = world.dfs_coordinator(_dfs_client())
    coordinator.run()
    moved = load("event_odds_us_dfs")
    for market in moved["bookmakers"][0]["markets"]:
        for outcome in market["outcomes"]:
            if outcome["description"] == "LeBron James" and market["key"] == "player_points":
                outcome["point"] = 28.5
    coordinator.client.odds = moved

    world.clock[0] = NOW + timedelta(hours=1)
    coordinator.run()

    pool = world.pool_reader().get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert dict(_players(pool)[LEBRON].lines["prizepicks"]) == {**PP_LEBRON, "PTS": 28.5}
    assert dict(_players(pool)[JOKIC].lines["prizepicks"])["PTS"] == 26.5


# -- what is not a PrizePicks line ---------------------------------------------


def test_a_response_without_prizepicks_is_a_real_poll_that_stores_nothing(world):
    body = load("event_odds_us_dfs")
    body["bookmakers"] = body["bookmakers"][1:]  # Underdog, Pick6 and Dabble only
    client = _dfs_client(odds=body)

    result = world.dfs_coordinator(client).run()

    assert (result.status, result.calls, result.credits) == ("complete", 1, 5)
    assert world.pool_reader().get_pool_for_game(season=SEASON, game_id=GAME_ID).players == ()
    assert _rows(world, LatestPlayerProjection) == []
    (poll,) = _rows(world, OddsApiPoll)
    assert (poll["outcome"], poll["quote_count"]) == ("complete", 0)


def test_an_empty_response_is_a_free_poll_that_stores_nothing(world):
    client = _dfs_client(odds=load("event_odds_empty"))

    result = world.dfs_coordinator(client).run()

    assert (result.status, result.calls, result.credits) == ("complete", 1, 0)
    (poll,) = _rows(world, OddsApiPoll)
    assert (poll["outcome"], poll["cost"], poll["quote_count"]) == ("empty", 0, 0)
    assert _rows(world, ProviderPoll) == []


# -- a paid response that cannot be recorded -----------------------------------


def test_a_response_that_cannot_be_published_still_counts_as_the_hours_poll(world):
    messages = []
    coordinator = world.dfs_coordinator(
        _dfs_client(), notify=lambda level, message: messages.append((level, message))
    )

    def refuse(*_args, **_kwargs):
        raise RuntimeError("archive unavailable")

    coordinator.dfs_writer.record = refuse

    result = coordinator.run()

    assert (result.status, result.reason, result.calls, result.credits) == (
        "failed",
        "storage_error",
        1,
        5,
    )
    (poll,) = _rows(world, OddsApiPoll)
    assert (poll["outcome"], poll["failure_reason"], poll["cost"], poll["quote_count"]) == (
        "complete",
        "storage_error",
        5,
        0,
    )
    assert (logging.ERROR, "Odds API could not record PrizePicks lines; backing off") in messages
    (state,) = _rows(world, OddsApiQuotaState)
    assert (state["consecutive_failures"], state["last_failure_reason"]) == (1, "storage_error")

    # Not bought again: the backoff holds for half an hour, and past it the
    # hourly cadence is already satisfied by the poll that was paid for.
    client = coordinator.client
    for minutes in (5, 29, 31, 55):
        world.clock[0] = NOW + timedelta(minutes=minutes)
        again = coordinator.run()
        assert again.calls == 0, minutes
    assert client.event_calls == [EVENT_ID]

    # The fault clears; the next hour's poll publishes the lines.
    del coordinator.dfs_writer.record
    world.clock[0] = NOW + timedelta(hours=1)
    recovered = coordinator.run()
    assert (recovered.status, recovered.calls) == ("complete", 1)
    assert client.event_calls == [EVENT_ID, EVENT_ID]
    pool = world.pool_reader().get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert len(pool.players) == 4


def test_a_month_with_a_storage_failure_every_hour_still_costs_fourteen_calls_a_game(world):
    # Buying and failing to publish used to leave the cadence unsatisfied, so
    # a game was bought again after every failure (15 calls, 18,750 credits a
    # 250-game month).  A failed publication now counts like any poll.
    client = _dfs_client()
    coordinator = world.dfs_coordinator(client)
    coordinator.dfs_writer.record = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))

    now = TIP - timedelta(hours=13)
    while now < TIP + timedelta(minutes=30):
        world.clock[0] = now
        coordinator.run()
        now += timedelta(minutes=5)

    assert len(client.event_calls) <= 14
    assert len(client.event_calls) * 5 == sum(poll["cost"] for poll in _rows(world, OddsApiPoll))
    assert all(poll["failure_reason"] == "storage_error" for poll in _rows(world, OddsApiPoll))


def test_a_crash_between_the_ledger_row_and_publication_does_not_rebuy_the_game(world):
    class Crash(BaseException):
        pass

    coordinator = world.dfs_coordinator(_dfs_client())

    def crash(*_args, **_kwargs):
        raise Crash()

    coordinator.dfs_writer.record = crash
    with pytest.raises(Crash):
        coordinator.run()

    (poll,) = _rows(world, OddsApiPoll)
    assert (poll["outcome"], poll["failure_reason"], poll["cost"]) == ("complete", "unpublished", 5)
    del coordinator.dfs_writer.record
    world.clock[0] = NOW + timedelta(minutes=5)
    assert coordinator.run().calls == 0
    assert coordinator.client.event_calls == [EVENT_ID]


def test_a_response_that_crosses_tip_is_paid_for_once_and_never_stored(world):
    client = _dfs_client()
    world.clock[0] = TIP - timedelta(minutes=9)  # the closing fetch is due
    client.on_odds = lambda: world.clock.__setitem__(0, TIP + timedelta(seconds=1))

    result = world.dfs_coordinator(client).run()

    assert (result.status, result.reason, result.calls, result.credits) == (
        "partial",
        "received_after_tip",
        1,
        5,
    )
    (poll,) = _rows(world, OddsApiPoll)  # one row, not a ledger row and a second
    assert (poll["outcome"], poll["failure_reason"], poll["cost"]) == (
        "failed",
        "received_after_tip",
        5,
    )
    assert _rows(world, LatestPlayerProjection) == []
    assert _rows(world, ProviderPoll) == []


def test_a_worker_whose_lease_was_taken_over_while_resolving_names_publishes_nothing(world):
    messages = []
    coordinator = world.dfs_coordinator(
        _dfs_client(), notify=lambda level, message: messages.append((level, message))
    )
    taker = world.dfs_coordinator(_dfs_client())
    resolve = coordinator.name_resolver.resolve_athletes

    def resolve_after_takeover(names, team_ids):
        # The paid response is in and its quota is saved; the lease then lapses
        # and another worker (same owner string, next fence) takes it.
        world.clock[0] = NOW + timedelta(minutes=6)
        taker._acquire_lease(world.clock[0])
        return resolve(names, team_ids)

    coordinator.name_resolver = type(
        "Resolver", (), {"resolve_athletes": staticmethod(resolve_after_takeover)}
    )()

    result = coordinator.run()

    assert (result.status, result.reason) == ("failed", "lease_lost")
    # Losing the lease is not a storage fault: no alert, no backoff.
    assert not [m for level, m in messages if level >= logging.ERROR]
    (state,) = _rows(world, OddsApiQuotaState)
    assert state["consecutive_failures"] == 0
    assert _rows(world, ProviderPoll) == []
    assert _rows(world, LatestPlayerProjection) == []
    # The purchase is still this game's poll: the successor does not buy it again.
    (poll,) = _rows(world, OddsApiPoll)
    assert (poll["outcome"], poll["failure_reason"], poll["cost"]) == ("complete", "unpublished", 5)


def test_a_takeover_between_the_ledger_row_and_publication_publishes_nothing(world):
    coordinator = world.dfs_coordinator(_dfs_client())
    taker = world.dfs_coordinator(_dfs_client())
    record = coordinator.store.record_unstored_poll

    def record_then_lose_the_lease(**kwargs):
        poll_id = record(**kwargs)
        world.clock[0] = NOW + timedelta(minutes=6)
        taker._acquire_lease(world.clock[0])
        return poll_id

    coordinator.store.record_unstored_poll = record_then_lose_the_lease

    result = coordinator.run()

    assert (result.status, result.reason) == ("failed", "lease_lost")
    assert _rows(world, LatestPlayerProjection) == []
    (poll,) = _rows(world, OddsApiPoll)
    assert poll["failure_reason"] == "unpublished"  # the paid poll is still counted


def test_after_a_publication_fault_other_games_wait_out_the_backoff(world):
    client = _dfs_client(
        events=load("events")[:2],
        odds_by_event={"1f2e3d4c5b6a79880a1b2c3d4e5f6071": _second_game_body()},
    )
    world.clock[0] = NOW + timedelta(hours=2)
    coordinator = world.dfs_coordinator(client)
    coordinator.dfs_writer.record = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))

    first = coordinator.run()
    assert (first.status, first.calls) == ("failed", 1)  # the beat stops at the fault

    del coordinator.dfs_writer.record
    for minutes in (5, 29):
        world.clock[0] = NOW + timedelta(hours=2, minutes=minutes)
        assert coordinator.run().calls == 0  # the second game is due, and waits
    world.clock[0] = NOW + timedelta(hours=2, minutes=31)
    resumed = coordinator.run()
    assert (resumed.status, resumed.calls) == ("complete", 1)
    assert len(client.event_calls) == 2 and client.event_calls[0] != client.event_calls[1]


def test_a_fault_right_after_the_purchase_never_buys_the_game_twice(world):
    from app.services.odds_collection import run_once

    coordinator = world.dfs_coordinator(_dfs_client())
    save_quota = coordinator._save_quota

    def refuse(*_args, **_kwargs):
        raise RuntimeError("database hiccup")

    coordinator._save_quota = refuse
    assert run_once(coordinator).reason == "unexpected_error"
    (poll,) = _rows(world, OddsApiPoll)
    assert (poll["outcome"], poll["failure_reason"], poll["cost"]) == ("complete", "unpublished", 5)

    coordinator._save_quota = save_quota
    for minutes in (5, 30, 55):
        world.clock[0] = NOW + timedelta(minutes=minutes)
        assert coordinator.run().calls == 0
    assert coordinator.client.event_calls == [EVENT_ID]


def test_a_fault_while_resolving_names_never_buys_the_game_twice(world):
    from app.services.odds_collection import run_once

    coordinator = world.dfs_coordinator(_dfs_client())

    def refuse(*_args, **_kwargs):
        raise RuntimeError("catalog read failed")

    coordinator.name_resolver = type("R", (), {"resolve_athletes": staticmethod(refuse)})()
    assert run_once(coordinator).reason == "unexpected_error"

    world.clock[0] = NOW + timedelta(minutes=30)
    assert coordinator.run().calls == 0
    assert coordinator.client.event_calls == [EVENT_ID]


def test_a_resolver_fault_does_not_retire_lines_the_response_still_carries(world):
    from app.services.athlete_mapping_errors import AthleteMappingPersistenceError

    coordinator = world.dfs_coordinator(_dfs_client())
    coordinator.run()  # LeBron's five lines are posted at 12:00
    record = world.athlete_repository.record_resolution

    def record_unless_lebron(resolution, **kwargs):
        if resolution.provider_evidence.provider_id == "name:lebronjames":
            raise AthleteMappingPersistenceError("store unavailable")
        return record(resolution, **kwargs)

    world.athlete_repository.record_resolution = record_unless_lebron

    world.clock[0] = NOW + timedelta(hours=1)
    result = coordinator.run()  # an unchanged response; LeBron's name cannot resolve

    assert (result.status, result.unresolved_players) == ("complete", 3)
    pool = world.pool_reader().get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert dict(_players(pool)[LEBRON].lines["prizepicks"]) == PP_LEBRON  # still posted
    # The lines were not re-confirmed, so they age out on their own clock.
    assert pool.freshness["providers"]["prizepicks"]["retrieved_at"] == NOW.isoformat()
    world.athlete_repository.record_resolution = record

    _start_lakers_game(world)
    world.clock[0] = TIP + timedelta(minutes=3)
    coordinator.run()
    closing, is_closing = world.pool_reader().get_pool_for_game_with_closing_state(
        season=SEASON, game_id=GAME_ID
    )
    assert is_closing is True
    assert dict(_players(closing)[LEBRON].lines["prizepicks"]) == PP_LEBRON


def test_a_quota_fault_after_the_purchase_still_costs_fourteen_calls_a_game(world):
    from app.services.odds_collection import run_once

    client = _dfs_client()
    coordinator = world.dfs_coordinator(client)
    save = coordinator._save_quota
    failed = []

    def fail_once(quota, *, now):
        if quota.last_cost and not failed:
            failed.append(now)
            raise RuntimeError("quota persistence interrupted after the paid response")
        return save(quota, now=now)

    coordinator._save_quota = fail_once
    now = TIP - timedelta(hours=13)
    while now < TIP:
        world.clock[0] = now
        run_once(coordinator)
        now += timedelta(minutes=5)

    assert failed
    assert len(client.event_calls) == 14
    assert 20000 - client.remaining == 70  # what the vendor charged
    assert sum(poll["cost"] for poll in _rows(world, OddsApiPoll)) == 70  # what the ledger says


def test_a_worker_superseded_just_before_the_archive_write_publishes_nothing(world):
    stale = world.dfs_coordinator(_dfs_client())
    successor = world.dfs_coordinator(_dfs_client())
    record = stale.dfs_writer.record

    def pause_then_write(*args, **kwargs):
        # The stale worker stalls; its lease lapses and the successor runs a
        # whole beat.  The purchase is in the ledger, so the successor makes
        # no call of its own.
        world.clock[0] = NOW + timedelta(minutes=6)
        assert successor.run().calls == 0
        return record(*args, **kwargs)

    stale.dfs_writer.record = pause_then_write

    result = stale.run()

    assert (result.status, result.reason) == ("failed", "lease_lost")
    assert _rows(world, LatestPlayerProjection) == []
    assert _rows(world, ProviderPoll) == []


def test_a_stalled_closing_fetch_cannot_publish_after_the_successor_closed_the_game(world):
    world.clock[0] = TIP - timedelta(minutes=9)
    stale = world.dfs_coordinator(_dfs_client())
    successor = world.dfs_coordinator(_dfs_client())
    record = stale.dfs_writer.record

    def stall_past_tip(*args, **kwargs):
        _start_lakers_game(world)
        world.clock[0] = TIP + timedelta(minutes=3)
        assert successor.run().calls == 0
        assert len(_rows(world, ClosingProjectionSet)) == 1  # frozen, empty
        return record(*args, **kwargs)

    stale.dfs_writer.record = stall_past_tip

    result = stale.run()

    assert (result.status, result.reason) == ("failed", "lease_lost")
    # Nothing slipped into Latest behind the frozen closing set.
    assert _rows(world, LatestPlayerProjection) == []
    assert _rows(world, ProviderPoll) == []


def test_a_mapping_store_fault_on_the_closing_fetch_keeps_the_players_closing_lines(world):
    from app.services.athlete_mapping_errors import AthleteMappingPersistenceError

    coordinator = world.dfs_coordinator(_dfs_client())
    coordinator.run()
    record = world.athlete_repository.record_resolution

    def fail_for_lebron(resolution, **kwargs):
        if resolution.provider_evidence.name == "LeBron James":
            raise AthleteMappingPersistenceError("injected transient mapping write failure")
        return record(resolution, **kwargs)

    world.athlete_repository.record_resolution = fail_for_lebron
    world.clock[0] = TIP - timedelta(minutes=9)
    assert coordinator.run().calls == 1  # the same response, LeBron's name unresolved

    assert not [r for r in _rows(world, ProjectionObservation) if r["market_status"] == "suspended"]
    world.athlete_repository.record_resolution = record
    _start_lakers_game(world)
    world.clock[0] = TIP + timedelta(minutes=3)
    coordinator.run()
    closing, is_closing = world.pool_reader().get_pool_for_game_with_closing_state(
        season=SEASON, game_id=GAME_ID
    )
    assert is_closing is True
    assert dict(_players(closing)[LEBRON].lines["prizepicks"]) == PP_LEBRON


# -- the game starts -----------------------------------------------------------


def _start_lakers_game(world):
    started = event_row(GAME_ID, 1610612743, "Nuggets", "DEN", LAL, "Lakers", "LAL", TIP)
    started.update(status_text="Q1 11:42", status_code=2)
    other = event_row(
        "0022600002",
        1610612744,
        "Warriors",
        "GSW",
        1610612746,
        "Clippers",
        "LAC",
        TIP + timedelta(minutes=90),
    )
    world.catalog_service.repository.publish(
        SEASON, pd.DataFrame([started, other]), TIP + timedelta(minutes=2)
    )


def test_started_games_are_closed_for_prizepicks_and_the_closing_pool_serves_the_lines(world):
    coordinator = world.dfs_coordinator(_dfs_client())
    world.clock[0] = TIP - timedelta(minutes=9)
    coordinator.run()
    assert _rows(world, ClosingProjectionSet) == []

    _start_lakers_game(world)
    world.clock[0] = TIP + timedelta(minutes=3)
    coordinator.run()

    (closing,) = _rows(world, ClosingProjectionSet)
    assert (closing["provider"], closing["canonical_game_id"]) == ("prizepicks", GAME_ID)
    pool, is_closing = world.pool_reader().get_pool_for_game_with_closing_state(
        season=SEASON, game_id=GAME_ID
    )
    assert is_closing is True
    assert dict(_players(pool)[LEBRON].lines["prizepicks"]) == PP_LEBRON
    assert pool.freshness["state"] == "closing"

    # Closing is taken once, and no paid call is made at or after tip.
    coordinator.run()
    assert len(_rows(world, ClosingProjectionSet)) == 1
    assert coordinator.client.event_calls == [EVENT_ID]


# -- an hourly provider beside a fast one --------------------------------------


def _record_underdog(world, *, retrieved_at):
    """A Complete Underdog snapshot for the Lakers game, as the DFS collector
    would archive it every few minutes."""

    catalog = StatisticCatalog.load_default()
    team = TeamEvidence(canonical_id=LAL)
    market = PlayerProjectionMarket(
        provider="underdog",
        market_id="ud-lebron-points",
        athlete=AthleteEvidence(provider_id="ud-lebron", canonical_id=LEBRON, name="LeBron James", team=team),
        event=EventEvidence(provider_id="ud-game", canonical_id=GAME_ID),
        team=team,
        statistic=StatisticEvidence(label="Points"),
        threshold=MarketThreshold(Decimal("27.5"), unit="count"),
        scoring_period=None,
        selections=(
            Selection(selection_id="ud-higher", direction=SelectionDirection.HIGHER, american_price=-110),
        ),
    )
    market = replace(market, statistic_match=StatisticResolver(catalog).resolve_market(market))
    snapshot = _build_snapshot(
        provider="underdog",
        markets=[market],
        retrieved_at=retrieved_at,
        fetched_count=1,
        eligible_count=1,
        normalized_count=1,
        pagination_complete=True,
        fanout_complete=True,
    )
    ProjectionArchive(world.engine, catalog).ingest_snapshot(
        snapshot,
        query=NBAMarketQuery(season=SEASON),
        accepted_at=retrieved_at,
    )


def test_the_hourly_provider_does_not_make_the_whole_pool_read_stale(world):
    world.dfs_coordinator(_dfs_client()).run()  # PrizePicks at 12:00
    _record_underdog(world, retrieved_at=NOW + timedelta(minutes=50))
    world.clock[0] = NOW + timedelta(minutes=55)

    pool = world.pool_reader().get_pool_for_game(season=SEASON, game_id=GAME_ID)

    underdog_at = (NOW + timedelta(minutes=50)).isoformat()
    # Each provider reports its own time and status; the aggregate is the
    # oldest of the providers on the default window, so 55 minutes of
    # PrizePicks age is not the pool's age.
    assert pool.freshness["providers"] == {
        "prizepicks": {"status": "fresh", "retrieved_at": NOW.isoformat()},
        "underdog": {"status": "fresh", "retrieved_at": underdog_at},
    }
    assert pool.freshness["retrieved_at"] == underdog_at
    assert pool.freshness["observed_at"] == underdog_at
    assert pool.game_states == {GAME_ID: {"state": "live", "observed_at": underdog_at}}
    assert pool.freshness["status"] == "fresh"
    lebron = _players(pool)[LEBRON]
    assert set(lebron.lines) == {"prizepicks", "underdog"}


def test_the_hourly_provider_sets_the_aggregate_when_it_is_the_only_evidence(world):
    world.dfs_coordinator(_dfs_client()).run()
    world.clock[0] = NOW + timedelta(minutes=55)

    pool = world.pool_reader().get_pool_for_game(season=SEASON, game_id=GAME_ID)

    assert pool.freshness["retrieved_at"] == NOW.isoformat()
    assert pool.game_states == {GAME_ID: {"state": "live", "observed_at": NOW.isoformat()}}


def test_other_providers_keep_the_fifteen_minute_window(world):
    _record_underdog(world, retrieved_at=NOW)
    reader = world.pool_reader()

    world.clock[0] = NOW + timedelta(minutes=15)
    assert len(reader.get_pool_for_game(season=SEASON, game_id=GAME_ID).players) == 1
    world.clock[0] = NOW + timedelta(minutes=16)
    assert reader.get_pool_for_game(season=SEASON, game_id=GAME_ID).players == ()


# -- the snapshot builder, on its own ------------------------------------------


def _event():
    return MappedEvent(
        event=OddsEvent(EVENT_ID, TIP, "Denver Nuggets", "Los Angeles Lakers"),
        game_id=GAME_ID,
        team_ids=(DEN, LAL),
        scheduled_at=TIP,
    )


def _quote(book, name="LeBron James", market="player_points", category="PTS", line=27.5):
    return BookQuote(book, market, category, name, line, -119, -119, None)


_LEBRON = {"LeBron James": ResolvedAthlete(LEBRON, LAL, "LeBron James")}


def _build(quotes, athletes=_LEBRON, payout=None, withdrawn=()):
    return build_snapshot(
        _event(),
        quotes,
        athletes,
        retrieved_at=NOW,
        statistics=StatisticResolver(StatisticCatalog.load_default()),
        payout=payout,
        withdrawn=withdrawn,
    )


def test_the_builder_turns_only_prizepicks_quotes_of_resolved_players_into_markets():
    snapshot = _build(
        [
            _quote("prizepicks"),
            _quote("prizepicks", "Stephen Curry", line=29.5),  # unresolved: no market
            _quote("prizepicks", market="player_blocks", category="BLK", line=1.5),  # unmapped
        ]
    )

    assert snapshot.provider == "prizepicks"
    assert snapshot.status.value == "partial"
    (market,) = snapshot.markets
    assert (market.market_id, float(market.threshold.value)) == (
        f"{EVENT_ID}:player_points:name:lebronjames",
        27.5,
    )
    assert market.athlete.canonical_id == LEBRON and market.event.canonical_id == GAME_ID
    assert (snapshot.coverage.fetched_count, snapshot.coverage.normalized_count) == (3, 1)


@pytest.mark.parametrize("book", ["underdog", "pick6", "dabble_us_dfs", "draftkings"])
def test_the_builder_never_turns_another_apps_quote_into_a_market(book):
    assert _build([_quote(book)]) is None
    snapshot = _build([_quote("prizepicks", line=27.5), _quote(book, line=26.5)])
    (market,) = snapshot.markets
    assert float(market.threshold.value) == 27.5


@pytest.mark.parametrize(
    "market,category",
    [
        ("player_points_alternate", "PTS"),
        ("player_rebounds_alternate", "REB"),
        ("player_assists_alternate", "AST"),
        ("player_threes_alternate", "3PM"),
        ("player_points_rebounds_assists_alternate", "PRA"),
    ],
)
def test_the_builder_never_turns_an_alternate_market_into_a_market(market, category):
    # One point, so nothing but the market key tells it from a standard line.
    assert _build([_quote("prizepicks", market=market, category=category, line=22.5)]) is None


def test_both_sides_take_the_price_of_the_payout_table_the_builder_is_given():
    table = EntryPayoutTable(reference_entry_size=2, multipliers={2: Decimal("4.5")})

    (market,) = _build([_quote("prizepicks")], payout=table).markets
    (unpriced,) = _build([_quote("prizepicks")], payout=None).markets

    assert {
        (s.direction.value, str(s.price_value), s.price_kind.value, s.price_scope.value)
        for s in market.selections
    } == {
        ("higher", "4.5", "multiplier", "entry"),
        ("lower", "4.5", "multiplier", "entry"),
    }
    # With no payout table the sides are honestly unpriced (and so not targetable).
    assert [s.price_kind.value for s in unpriced.selections] == ["unpriced", "unpriced"]


def test_the_builder_has_nothing_to_record_without_a_resolved_prizepicks_quote():
    assert _build([_quote("underdog")], athletes={}) is None
    assert _build([_quote("prizepicks")], athletes={}) is None


def test_a_line_the_response_carries_for_an_unresolved_player_is_neither_confirmed_nor_retired():
    offering = LatestOffering(
        market_id=f"{EVENT_ID}:player_points:name:lebronjames",
        athlete_provider_id="name:lebronjames",
        canonical_player_id=LEBRON,
        player_name="LeBron James",
        team_id=LAL,
        market_category="PTS",
        line_value=27.5,
    )

    # The name did not resolve this time (a mapping store hiccup, say), but the
    # response still carries the line: nothing to confirm, nothing to retire.
    assert _build([_quote("prizepicks")], athletes={}, withdrawn=[offering]) is None
    # Carried under a different market key it is genuinely absent: retired.
    rebounds = _quote("prizepicks", market="player_rebounds", category="REB", line=7.5)
    snapshot = _build([rebounds], athletes={}, withdrawn=[offering])
    assert [(m.market_id, m.status.value) for m in snapshot.markets] == [(offering.market_id, "suspended")]


def test_the_builder_names_a_withdrawn_line_again_as_a_suspended_market():
    gone = LatestOffering(
        market_id=f"{EVENT_ID}:player_rebounds:name:lebronjames",
        athlete_provider_id="name:lebronjames",
        canonical_player_id=LEBRON,
        player_name="LeBron James",
        team_id=LAL,
        market_category="REB",
        line_value=7.5,
    )
    still = LatestOffering(
        market_id=f"{EVENT_ID}:player_points:name:lebronjames",
        athlete_provider_id="name:lebronjames",
        canonical_player_id=LEBRON,
        player_name="LeBron James",
        team_id=LAL,
        market_category="PTS",
        line_value=27.5,
    )

    snapshot = _build([_quote("prizepicks", line=28.5)], withdrawn=[gone, still])

    states = {m.market_id: (m.status.value, float(m.threshold.value)) for m in snapshot.markets}
    assert states == {
        f"{EVENT_ID}:player_points:name:lebronjames": ("available", 28.5),  # the new line wins
        f"{EVENT_ID}:player_rebounds:name:lebronjames": ("suspended", 7.5),
    }
    assert snapshot.coverage.normalized_count == 1  # only the live line is "posted"
    # A response that carries nothing for the game still retires what it had.
    assert [m.status.value for m in _build([], withdrawn=[gone]).markets] == ["suspended"]


# -- a line the next response drops --------------------------------------------


def _without(body, *, player, market):
    body = deepcopy(body)
    for entry in body["bookmakers"][0]["markets"]:
        if entry["key"] == market:
            entry["outcomes"] = [o for o in entry["outcomes"] if o["description"] != player]
    return body


def test_a_line_the_next_response_drops_is_retired_at_that_poll(world):
    coordinator = world.dfs_coordinator(_dfs_client())
    coordinator.run()
    coordinator.client.odds = _without(
        _without(load("event_odds_us_dfs"), player="LeBron James", market="player_points"),
        player="Jamal Murray",
        market="player_points",
    )

    world.clock[0] = NOW + timedelta(hours=1)
    coordinator.run()

    pool = world.pool_reader().get_pool_for_game(season=SEASON, game_id=GAME_ID)
    players = _players(pool)
    assert dict(players[LEBRON].lines["prizepicks"]) == {
        "REB": 7.5,
        "AST": 7.5,
        "3PM": 2.5,
        "PRA": 42.5,
    }
    assert MURRAY not in players  # his only line went
    assert dict(players[JOKIC].lines["prizepicks"])["PTS"] == 26.5
    (poll, _first) = sorted(_rows(world, OddsApiPoll), key=lambda r: -r["poll_id"])
    assert poll["quote_count"] == 8


def test_a_withdrawn_line_does_not_return_in_the_closing_set(world):
    coordinator = world.dfs_coordinator(_dfs_client())
    coordinator.run()
    coordinator.client.odds = _without(
        load("event_odds_us_dfs"), player="LeBron James", market="player_points"
    )
    world.clock[0] = TIP - timedelta(minutes=9)  # the closing fetch no longer has it
    coordinator.run()

    _start_lakers_game(world)
    world.clock[0] = TIP + timedelta(minutes=3)
    coordinator.run()

    pool, is_closing = world.pool_reader().get_pool_for_game_with_closing_state(
        season=SEASON, game_id=GAME_ID
    )
    assert is_closing is True
    assert "PTS" not in dict(_players(pool)[LEBRON].lines["prizepicks"])
    assert dict(_players(pool)[LEBRON].lines["prizepicks"]) == {
        "REB": 7.5,
        "AST": 7.5,
        "3PM": 2.5,
        "PRA": 42.5,
    }


def test_a_response_without_prizepicks_retires_that_games_lines_only(world):
    client = _dfs_client(
        events=load("events")[:2],
        odds_by_event={"1f2e3d4c5b6a79880a1b2c3d4e5f6071": _second_game_body()},
    )
    client.on_odds = lambda: world.clock.__setitem__(0, world.clock[0] + timedelta(seconds=1))
    world.clock[0] = NOW + timedelta(hours=2)
    coordinator = world.dfs_coordinator(client)
    coordinator.run()

    gone = load("event_odds_us_dfs")
    gone["bookmakers"] = gone["bookmakers"][1:]  # PrizePicks no longer answers; the others do
    client.odds = gone
    world.clock[0] = NOW + timedelta(hours=3)
    coordinator.run()

    reader = world.pool_reader()
    assert reader.get_pool_for_game(season=SEASON, game_id=GAME_ID).players == ()
    second = reader.get_pool_for_game(season=SEASON, game_id="0022600002")
    assert [p.name for p in second.players] == ["Stephen Curry"]


def test_an_empty_response_means_not_posted_yet_and_retires_nothing(world):
    coordinator = world.dfs_coordinator(_dfs_client())
    coordinator.run()
    coordinator.client.odds = load("event_odds_empty")

    world.clock[0] = NOW + timedelta(hours=1)
    coordinator.run()

    pool = world.pool_reader().get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert dict(_players(pool)[LEBRON].lines["prizepicks"]) == PP_LEBRON


# -- the closing pool's aggregate -----------------------------------------------


def test_the_closing_pools_aggregate_time_is_not_the_hourly_providers(world):
    coordinator = world.dfs_coordinator(_dfs_client())
    coordinator.run()  # PrizePicks at 12:00
    underdog_at = TIP - timedelta(minutes=1)
    _record_underdog(world, retrieved_at=underdog_at)
    _start_lakers_game(world)
    ProjectionArchive(world.engine, StatisticCatalog.load_default()).freeze_closing_projection_set(
        provider="underdog",
        query=NBAMarketQuery(season=SEASON),
        canonical_game_id=GAME_ID,
        started_at=TIP,
        created_at=TIP + timedelta(minutes=2),
    )
    world.clock[0] = TIP + timedelta(minutes=3)
    coordinator.run()  # freezes the PrizePicks set

    pool = world.pool_reader().get_pool_for_game(season=SEASON, game_id=GAME_ID)

    assert pool.freshness["state"] == "closing"
    assert pool.freshness["providers"]["prizepicks"]["retrieved_at"] == NOW.isoformat()
    assert pool.freshness["providers"]["underdog"]["retrieved_at"] == underdog_at.isoformat()
    assert pool.freshness["retrieved_at"] == underdog_at.isoformat()
    assert pool.game_states[GAME_ID]["observed_at"] == underdog_at.isoformat()
