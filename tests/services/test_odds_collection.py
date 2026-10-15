"""Behavior of Odds API collection over a migrated SQLite database (#377)."""

from __future__ import annotations

from datetime import timedelta
import logging

import pytest
from sqlalchemy import select

from app.models.athlete_mapping import ProviderAthleteMapping
from app.models.sportsbook_odds import (
    OddsApiPoll,
    OddsApiQuotaState,
    SportsbookPropLatest,
    SportsbookPropQuote,
)
from app.providers.odds_api import OddsApiError
from app.services.sportsbook_quotes import SportsbookQuoteReader
from tests.support.odds_world import (
    CURRY,
    EVENT_ID,
    GAME_ID,
    JOKIC,
    LAL,
    LEBRON,
    NOW,
    TIP,
    FakeClient,
    build_world,
    load,
)


@pytest.fixture
def world(tmp_path):
    return build_world(tmp_path)


def _rows(world, model):
    with world.engine.connect() as connection:
        return connection.execute(select(model.__table__)).mappings().all()


# -- name resolution within the two rosters ---------------------------------


def test_names_resolve_only_within_the_two_rosters(world):
    resolved = world.name_resolver.resolve(
        [
            "LeBron James",
            "Nikola Jokic",  # book prints no accent; catalog has "Nikola Jokić"
            "Nikola Jokić",
            "Stephen Curry",  # real player, but on neither roster of this game
            "Kasparas Jakucionis",  # rookie missing from the catalog
        ],
        (LAL, 1610612743),
    )

    assert resolved == {
        "LeBron James": LEBRON,
        "Nikola Jokic": JOKIC,
        "Nikola Jokić": JOKIC,
    }


def test_the_same_name_resolves_per_game_roster_not_globally(world):
    # Curry resolves in his own game and is refused in Lakers at Nuggets.
    assert world.name_resolver.resolve(["Stephen Curry"], (LAL, 1610612743)) == {}
    assert world.name_resolver.resolve(["Stephen Curry"], (1610612744, 1610612746)) == {
        "Stephen Curry": CURRY
    }


def test_two_equally_exact_athletes_on_the_rosters_are_ambiguous_and_skipped(tmp_path):
    from tests.support.odds_world import ATHLETES, DEN

    twin = (999001, "Jamal Murray", LAL, "LAL")
    world = build_world(tmp_path, athletes=ATHLETES + (twin,))

    resolved = world.name_resolver.resolve(["Jamal Murray", "LeBron James"], (LAL, DEN))

    assert resolved == {"LeBron James": LEBRON}


def test_resolved_names_are_persisted_as_oddsapi_mappings(world):
    world.name_resolver.resolve(["Nikola Jokic"], (LAL, 1610612743))

    mappings = _rows(world, ProviderAthleteMapping)
    assert [
        (row["provider"], row["provider_athlete_id"], row["canonical_player_id"])
        for row in mappings
    ] == [("oddsapi", "name:nikolajokic", JOKIC)]


# -- one beat end to end ----------------------------------------------------


def test_a_beat_stores_resolved_quotes_and_counts_the_unresolved(world):
    client = FakeClient()
    result = world.coordinator(client).run()

    assert (result.status, result.calls, result.credits) == ("complete", 1, 5)
    assert result.unresolved_events == 0
    assert result.unresolved_players == 2  # Curry (wrong team) and the rookie
    assert client.event_calls == [EVENT_ID]

    (poll,) = _rows(world, OddsApiPoll)
    assert (poll["canonical_game_id"], poll["outcome"], poll["cost"], poll["remaining"]) == (
        GAME_ID,
        "complete",
        5,
        19995,
    )
    assert (poll["quote_count"], poll["unresolved_count"], poll["is_closing"]) == (11, 2, False)

    latest = _rows(world, SportsbookPropLatest)
    assert len(latest) == 11
    lebron_points = sorted(
        (row["book"], float(row["line"]), row["over_price"], row["under_price"])
        for row in latest
        if row["canonical_player_id"] == LEBRON and row["market_category"] == "PTS"
    )
    assert lebron_points == [
        ("betmgm", 28.5, 100, -125),
        ("draftkings", 27.5, -115, -105),
        ("fanduel", 27.5, -110, -110),
        ("williamhill_us", 27.5, -105, None),
    ]
    assert {row["book"] for row in latest} == {
        "betmgm",
        "draftkings",
        "fanduel",
        "williamhill_us",
    }

    (state,) = _rows(world, OddsApiQuotaState)
    assert (state["remaining"], state["used"], state["last_cost"], state["tier"]) == (
        19995,
        5,
        5,
        "normal",
    )
    assert state["unresolved_total"] == 2


def test_the_beat_is_idempotent_within_the_hour(world):
    client = FakeClient()
    coordinator = world.coordinator(client)
    coordinator.run()

    world.clock[0] = NOW + timedelta(minutes=5)
    again = coordinator.run()

    assert (again.status, again.calls) == ("no_work", 0)
    assert client.event_calls == [EVENT_ID]


def test_the_next_hour_polls_again_and_identical_quotes_add_no_history(world):
    client = FakeClient()
    coordinator = world.coordinator(client)
    coordinator.run()
    history_after_first = len(_rows(world, SportsbookPropQuote))

    world.clock[0] = NOW + timedelta(hours=1)
    second = coordinator.run()

    assert (second.status, second.calls) == ("complete", 1)
    assert history_after_first == 11
    assert len(_rows(world, SportsbookPropQuote)) == 11
    assert len(_rows(world, OddsApiPoll)) == 2


def test_the_closing_poll_is_taken_in_the_last_ten_minutes_and_marked(world):
    client = FakeClient()
    coordinator = world.coordinator(client)
    world.clock[0] = TIP - timedelta(minutes=9)

    result = coordinator.run()

    assert result.calls == 1
    (poll,) = _rows(world, OddsApiPoll)
    assert poll["is_closing"] is True
    assert all(row["is_closing"] for row in _rows(world, SportsbookPropLatest))
    book_lines = SportsbookQuoteReader(world.engine).book_lines(GAME_ID)
    assert book_lines[LEBRON]["PTS"]["closing"] is True

    world.clock[0] = TIP - timedelta(minutes=4)
    assert coordinator.run().calls == 0  # closing is taken once


def test_nothing_is_fetched_at_or_after_tip(world):
    client = FakeClient()
    coordinator = world.coordinator(client)
    for moment in (TIP, TIP + timedelta(minutes=1)):
        world.clock[0] = moment
        assert coordinator.run().calls == 0
    assert client.event_calls == []


def test_an_empty_response_is_recorded_as_free_and_stores_no_quotes(world):
    client = FakeClient(odds=load("event_odds_empty"))

    result = world.coordinator(client).run()

    assert (result.status, result.calls, result.credits) == ("complete", 1, 0)
    (poll,) = _rows(world, OddsApiPoll)
    assert (poll["outcome"], poll["cost"], poll["quote_count"]) == ("empty", 0, 0)
    assert _rows(world, SportsbookPropLatest) == []


def test_a_rate_limit_backs_off_and_the_next_beat_makes_no_call(world):
    client = FakeClient()
    client.odds_error = OddsApiError("rate_limited", status=429, path="/odds")
    coordinator = world.coordinator(client)

    first = coordinator.run()
    assert (first.status, first.reason) == ("failed", "rate_limited")
    (state,) = _rows(world, OddsApiQuotaState)
    assert state["consecutive_failures"] == 1
    assert state["last_failure_reason"] == "rate_limited"

    world.clock[0] = NOW + timedelta(seconds=30)
    during = coordinator.run()
    assert during.status == "backoff"
    assert client.list_calls == 1

    client.odds_error = None
    world.clock[0] = NOW + timedelta(minutes=2)
    recovered = coordinator.run()
    assert (recovered.status, recovered.calls) == ("complete", 1)
    (state,) = _rows(world, OddsApiQuotaState)
    assert state["consecutive_failures"] == 0


def test_a_rejected_key_halts_for_six_hours(world):
    client = FakeClient()
    client.error = OddsApiError("unauthorized", status=401, path="/events")
    alerts = []
    coordinator = world.coordinator(client, notify=lambda level, message: alerts.append((level, message)))

    result = coordinator.run()

    assert (result.status, result.reason) == ("halted", "unauthorized")
    assert alerts == [
        (logging.ERROR, "Odds API rejected the key or plan (unauthorized); halted for 6h")
    ]
    world.clock[0] = NOW + timedelta(hours=5, minutes=55)
    assert coordinator.run().status == "backoff"
    world.clock[0] = NOW + timedelta(hours=6, minutes=1)
    client.error = None
    assert coordinator.run().status == "complete"


def test_a_held_lease_makes_the_beat_busy(world):
    first = world.coordinator(FakeClient())
    second = world.coordinator(FakeClient())
    token = first._acquire_lease(NOW)
    assert token == 1

    assert second.run().status == "busy"

    first._release_lease(token)
    assert second.run().status == "complete"


def test_full_team_names_map_and_an_unmapped_event_is_skipped_never_guessed(world):
    events = load("events")  # Lakers at Nuggets, "LA Clippers" at Warriors, Sonics
    client = FakeClient(events=events)

    result = world.coordinator(client).run()

    # "LA Clippers" and "Los Angeles Lakers" reach the catalog's short names
    # through tricodes; the Sonics are not a team, so no call is made for them.
    assert sorted(client.event_calls) == sorted(
        [EVENT_ID, "1f2e3d4c5b6a79880a1b2c3d4e5f6071"]
    )
    assert result.unresolved_events == 1


def test_credits_below_the_floor_halt_without_a_paid_call(world):
    client = FakeClient()
    coordinator = world.coordinator(client)
    coordinator._load_state(NOW)
    coordinator._save_quota(type("Q", (), {"remaining": 5, "used": 19995, "last_cost": 5})(), now=NOW)

    result = coordinator.run()

    assert (result.status, result.calls, result.tier) == ("no_work", 0, "halted")
    assert client.event_calls == []


def test_the_closing_call_may_spend_the_reserve_but_never_to_zero(world):
    client = FakeClient(remaining=10)
    coordinator = world.coordinator(client)
    coordinator._load_state(NOW)
    coordinator._save_quota(
        type("Q", (), {"remaining": 10, "used": 19990, "last_cost": 0})(), now=NOW
    )
    world.clock[0] = TIP - timedelta(minutes=9)

    result = coordinator.run()

    assert (result.status, result.reason, result.calls) == (
        "halted",
        "credits_exhausted",
        1,
    )
    (state,) = _rows(world, OddsApiQuotaState)
    assert (state["remaining"], state["tier"]) == (5, "halted")
    world.clock[0] = TIP - timedelta(minutes=8)
    assert coordinator.run().calls == 0
