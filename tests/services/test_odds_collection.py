"""Behavior of Odds API collection over a migrated SQLite database (#377)."""

from __future__ import annotations

from datetime import timedelta
import logging
from datetime import timezone

import pytest
from sqlalchemy import select

from app.models.athlete_mapping import ProviderAthleteMapping
from app.models.sportsbook_odds import (
    OddsApiPoll,
    OddsApiQuotaState,
    SportsbookPropLatest,
    SportsbookPropQuote,
)
from app.config.settings import OddsApiSettings
from app.providers.odds_api import OddsApiError, OddsQuota
from app.services.odds_collection import LeaseLost
from app.services.odds_schedule import Tier
from app.services.sportsbook_quotes import ResolvedQuote, SportsbookQuoteReader
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


# -- review fixes (#378) ------------------------------------------------------


def _latest_over(world, book="draftkings"):
    lines = SportsbookQuoteReader(world.engine).book_lines(GAME_ID)
    entry = lines.get(LEBRON, {}).get("PTS")
    if entry is None:
        return None
    return [b["over"] for b in entry["books"] if b["book"] == book][0]


def test_discovery_quota_stops_a_fresh_state_from_spending_the_last_credits(world):
    client = FakeClient(remaining=5)
    client.discovery_quota = OddsQuota(remaining=5, used=19995, last_cost=0)
    world.clock[0] = TIP - timedelta(minutes=9)  # a closing is due

    result = world.coordinator(client).run()

    assert (result.status, result.calls, result.tier) == ("no_work", 0, "halted")
    assert client.event_calls == []
    (state,) = _rows(world, OddsApiQuotaState)
    assert (state["remaining"], state["used"], state["tier"]) == (5, 19995, "halted")


def test_a_halted_collector_notices_the_monthly_reset_from_free_discovery(world):
    client = FakeClient()
    coordinator = world.coordinator(client)
    client.discovery_quota = OddsQuota(remaining=5, used=19995, last_cost=0)
    coordinator.run()
    assert _rows(world, OddsApiQuotaState)[0]["tier"] == "halted"

    client.discovery_quota = OddsQuota(remaining=20000, used=0, last_cost=0)
    world.clock[0] = NOW + timedelta(minutes=5)
    coordinator.run()

    (state,) = _rows(world, OddsApiQuotaState)
    assert (state["remaining"], state["tier"]) == (20000, "closing")  # off halt, one step


def test_a_call_is_not_made_when_discovery_runs_past_tip(world):
    client = FakeClient()
    world.clock[0] = TIP - timedelta(minutes=7)  # closing is due when the beat starts
    client.on_list = lambda: world.clock.__setitem__(0, TIP + timedelta(seconds=1))

    result = world.coordinator(client).run()

    assert client.event_calls == []
    assert (result.status, result.reason, result.calls) == ("partial", "tip_passed", 0)
    assert _rows(world, OddsApiPoll) == []


def test_a_response_that_crosses_tip_off_is_paid_for_but_never_stored(world):
    client = FakeClient()
    world.clock[0] = TIP - timedelta(minutes=7)
    client.on_odds = lambda: world.clock.__setitem__(0, TIP + timedelta(seconds=1))

    result = world.coordinator(client).run()

    assert (result.status, result.reason, result.credits) == ("partial", "received_after_tip", 5)
    (poll,) = _rows(world, OddsApiPoll)
    assert (poll["outcome"], poll["failure_reason"], poll["cost"], poll["quote_count"]) == (
        "failed", "received_after_tip", 5, 0,
    )
    assert _rows(world, SportsbookPropLatest) == []
    assert _rows(world, OddsApiQuotaState)[0]["remaining"] == 19995


def test_a_worker_that_lost_its_lease_cannot_overwrite_newer_quotes(world):
    stale = load("event_odds_five_markets")  # DraftKings Over -115
    fresh = load("event_odds_five_markets")
    fresh["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = -120
    worker_a = FakeClient(odds=stale)
    worker_b_client = FakeClient(odds=fresh)
    a = world.coordinator(worker_a)
    b = world.coordinator(worker_b_client)

    def a_pauses_past_expiry_while_b_takes_over():
        world.clock[0] = NOW + timedelta(minutes=6)
        assert b.run().status == "complete"

    worker_a.on_odds = a_pauses_past_expiry_while_b_takes_over

    result = a.run()

    assert (result.status, result.reason) == ("failed", "lease_lost")
    assert _latest_over(world) == -120
    assert len(_rows(world, OddsApiPoll)) == 1  # only B's poll committed
    assert _rows(world, OddsApiQuotaState)[0]["remaining"] == 19995


def test_a_postponement_reopens_hourly_polling_and_a_new_closing_snapshot(world):
    client = FakeClient()
    coordinator = world.coordinator(client)
    world.clock[0] = TIP - timedelta(minutes=9)
    coordinator.run()
    assert [p["is_closing"] for p in _rows(world, OddsApiPoll)] == [True]

    later = TIP + timedelta(hours=2)
    client.events[0]["commence_time"] = later.strftime("%Y-%m-%dT%H:%M:%SZ")
    world.clock[0] = TIP + timedelta(hours=1)
    reopened = coordinator.run()
    world.clock[0] = later - timedelta(minutes=9)
    closed_again = coordinator.run()

    assert (reopened.calls, closed_again.calls) == (1, 1)
    polls = _rows(world, OddsApiPoll)
    assert [(p["is_closing"], p["commence_time"].replace(tzinfo=timezone.utc)) for p in polls] == [
        (True, TIP), (False, later), (True, later),
    ]
    world.clock[0] = later - timedelta(minutes=8)
    assert coordinator.run().calls == 0  # closed for the new tip


def test_a_snapshot_whose_every_quote_is_omitted_removes_the_stale_line(world):
    client = FakeClient()
    coordinator = world.coordinator(client)
    coordinator.run()
    assert _latest_over(world) == -115

    ambiguous = load("event_odds_empty")
    draftkings = load("event_odds_five_markets")["bookmakers"][0]
    draftkings["markets"] = [
        {"key": "player_points", "last_update": "2026-10-21T22:00:00Z", "outcomes": [
            {"name": "Over", "description": "LeBron James", "price": -110, "point": 27.5},
            {"name": "Over", "description": "LeBron James", "price": -110, "point": 28.5},
        ]}
    ]
    ambiguous["bookmakers"] = [draftkings]
    client.odds = ambiguous
    world.clock[0] = NOW + timedelta(hours=1)
    coordinator.run()

    assert SportsbookQuoteReader(world.engine).book_lines(GAME_ID) == {}
    (state,) = [p for p in _rows(world, OddsApiPoll)][-1:]
    assert (state["outcome"], state["quote_count"]) == ("complete", 0)


def test_an_approved_mapping_outside_the_two_rosters_is_still_refused(world):
    world.athlete_repository.approve(
        "oddsapi", "name:stephencurry", CURRY, season="2026-27",
        operator_id="op", reason="correct Warriors mapping",
    )

    lakers_nuggets = world.name_resolver.resolve(["Stephen Curry"], (LAL, 1610612743))
    warriors_clippers = world.name_resolver.resolve(["Stephen Curry"], (1610612744, 1610612746))

    assert lakers_nuggets == {}
    assert warriors_clippers == {"Stephen Curry": CURRY}


def test_every_write_layer_refuses_a_stale_fence_even_with_the_same_owner_name(world):
    # Production workers share one owner string; only the fence tells them apart.
    a = world.coordinator(FakeClient())
    b = world.coordinator(FakeClient())
    a._load_state(NOW)
    a._fence = a._acquire_lease(NOW)
    b_fence = b._acquire_lease(NOW + timedelta(minutes=6))  # A's lease expired, B holds it
    assert (a._fence, b_fence) == (1, 2)
    quote = ResolvedQuote(LEBRON, "PTS", "draftkings", 27.5, -115, -105, NOW)
    writes = {
        "quota": lambda: a._save_quota(OddsQuota(1, 19999, 5), now=NOW),
        "tier": lambda: a._save_tier({}, Tier.HALTED, NOW, NOW),
        "failures": lambda: a._clear_failures(NOW, unresolved_players=3),
        "poll": lambda: a.store.record_poll(
            odds_event_id="e", game_id=GAME_ID, started_at=NOW, completed_at=NOW,
            cost=5, remaining=1, is_closing=False, quotes=[quote], fence=a._assert_lease,
        ),
        "failure": lambda: a._failed(
            NOW, {"consecutive_failures": 0, "tier": "normal"},
            OddsApiError("rate_limited", status=429, path="/odds"),
        ),
        "unstored poll": lambda: a.store.record_unstored_poll(
            odds_event_id="e", game_id=GAME_ID, started_at=NOW, completed_at=NOW,
            outcome="failed", is_closing=False, fence=a._assert_lease,
        ),
    }

    for name, write in writes.items():
        with pytest.raises(LeaseLost):
            write()

    (state,) = _rows(world, OddsApiQuotaState)
    assert (state["remaining"], state["tier"], state["unresolved_total"]) == (None, "normal", 0)
    assert (state["consecutive_failures"], state["backoff_until"]) == (0, None)
    assert _rows(world, OddsApiPoll) == [] and _rows(world, SportsbookPropLatest) == []


def test_a_beat_that_drifts_out_of_the_closing_window_never_stores_a_closing_flag(world):
    client = FakeClient()
    world.clock[0] = TIP - timedelta(minutes=5, seconds=1)  # closing is due
    client.on_list = lambda: world.clock.__setitem__(
        0, TIP - timedelta(minutes=4, seconds=59)  # two seconds of discovery latency
    )

    result = world.coordinator(client).run()

    assert (result.calls, result.reason) == (0, "window_passed")
    assert client.event_calls == []
    assert _rows(world, OddsApiPoll) == []


def test_the_guard_rechecks_remaining_credits_before_every_paid_call(world):
    events = load("events")[:2]
    events[1]["commence_time"] = (TIP + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Discovery says 1020, so both hourly polls fit above the 1000 reserve; a
    # shared-key consumer then spends 11 and our first response says 999.
    client = FakeClient(events=events, remaining=1004)
    client.discovery_quota = OddsQuota(1020, 18980, 0)
    world.clock[0] = TIP - timedelta(hours=5)
    settings = OddsApiSettings(enabled=True, api_key="k" * 24, assumed_lead_hours=1)

    result = world.coordinator(client, odds_settings=settings).run()

    assert (result.tier, len(client.event_calls)) == ("normal", 1)  # a second would dip below the reserve
    assert (result.status, result.remaining) == ("partial", 999)
    (state,) = _rows(world, OddsApiQuotaState)
    assert state["remaining"] == 999


def test_a_discarded_response_still_updates_the_stored_quota_first(world):
    client = FakeClient(remaining=10)
    world.clock[0] = TIP - timedelta(minutes=7)
    client.on_odds = lambda: world.clock.__setitem__(0, TIP + timedelta(seconds=1))

    world.coordinator(client).run()

    (state,) = _rows(world, OddsApiQuotaState)
    assert (state["remaining"], state["used"], state["last_cost"]) == (5, 19995, 5)
    assert [p["failure_reason"] for p in _rows(world, OddsApiPoll)] == ["received_after_tip"]
