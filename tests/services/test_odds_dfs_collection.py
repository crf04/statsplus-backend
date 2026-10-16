"""PrizePicks lines from The Odds API's ``us_dfs`` region (crf04/statsplus#215).

One collector beat runs over a migrated SQLite database with the real
resolvers, the real projection archive and the real Player Pool reader; only
the Odds API transport is a fake serving the ``us_dfs`` fixture, which is
built from the vendor's documented shape (no live key existed).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import logging

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
from app.services.projection_archive import ProjectionArchive
from app.services.statistic_catalog import StatisticCatalog, StatisticResolver
from tests.support.odds_world import (
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
    build_world,
    event_row,
    load,
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

    result = world.dfs_coordinator(client).run()

    assert (result.status, result.calls, result.credits, result.remaining) == (
        "complete", 1, 5, 19995,
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
    assert pool.freshness["providers"] == {
        "prizepicks": {"status": "fresh", "retrieved_at": NOW.isoformat()}
    }


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
        market["key"]
        for market in body["bookmakers"][0]["markets"]
        if market["key"].endswith("_alternate")
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
        "complete", 5, 19995, 2,
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


def test_no_sportsbook_region_is_ever_requested_by_the_beat(world):
    client = _dfs_client()

    world.dfs_coordinator(client).run()

    assert client.regions == "us_dfs"
    assert _rows(world, SportsbookPropLatest) == []


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
    assert {p.name: dict(p.lines["prizepicks"]) for p in second.players} == {
        "Stephen Curry": {"PTS": 29.5}
    }
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


def test_a_response_that_cannot_be_recorded_is_logged_failed_and_not_rebought(world):
    messages = []
    coordinator = world.dfs_coordinator(
        _dfs_client(), notify=lambda level, message: messages.append((level, message))
    )

    def refuse(*_args, **_kwargs):
        raise RuntimeError("archive unavailable")

    coordinator.dfs_writer.record = refuse

    result = coordinator.run()

    assert (result.status, result.reason, result.calls, result.credits) == (
        "failed", "storage_error", 1, 5,
    )
    (poll,) = _rows(world, OddsApiPoll)
    assert (poll["outcome"], poll["failure_reason"], poll["cost"]) == ("failed", "storage_error", 5)
    assert (logging.ERROR, "Odds API could not record PrizePicks lines; backing off") in messages
    (state,) = _rows(world, OddsApiQuotaState)
    assert (state["consecutive_failures"], state["last_failure_reason"]) == (1, "storage_error")

    # The paid response is not re-bought for half an hour, not at every beat.
    for minutes in (5, 25, 29):
        world.clock[0] = NOW + timedelta(minutes=minutes)
        again = coordinator.run()
        assert (again.status, again.reason, again.calls) == ("backoff", "backoff_active", 0)
    assert coordinator.client.event_calls == [EVENT_ID]

    # The fault clears; the first beat past the backoff buys and stores it.
    del coordinator.dfs_writer.record
    world.clock[0] = NOW + timedelta(minutes=31)
    recovered = coordinator.run()
    assert (recovered.status, recovered.calls) == ("complete", 1)
    pool = world.pool_reader().get_pool_for_game(season=SEASON, game_id=GAME_ID)
    assert len(pool.players) == 4


# -- the game starts -----------------------------------------------------------


def _start_lakers_game(world):
    started = event_row(
        GAME_ID, 1610612743, "Nuggets", "DEN", LAL, "Lakers", "LAL", TIP
    )
    started.update(status_text="Q1 11:42", status_code=2)
    other = event_row(
        "0022600002", 1610612744, "Warriors", "GSW", 1610612746, "Clippers", "LAC",
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
        athlete=AthleteEvidence(
            provider_id="ud-lebron", canonical_id=LEBRON, name="LeBron James", team=team
        ),
        event=EventEvidence(provider_id="ud-game", canonical_id=GAME_ID),
        team=team,
        statistic=StatisticEvidence(label="Points"),
        threshold=MarketThreshold(Decimal("27.5"), unit="count"),
        scoring_period=None,
        selections=(
            Selection(
                selection_id="ud-higher", direction=SelectionDirection.HIGHER, american_price=-110
            ),
        ),
    )
    market = replace(
        market, statistic_match=StatisticResolver(catalog).resolve_market(market)
    )
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
