"""On/off splits: the /api/on_off route over a faked provider and Redis."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config.settings import CacheSettings, RuntimeSettings
from app.errors import ProviderUnavailableError
from app.services.on_off import OnOffService, injury_warm_requests
from app.services.pbp_stats_adapter import PBPTotalsAdapter

FIXTURE = Path(__file__).parents[1] / "fixtures" / "pbpstats_wowy_den_jokic_off.json"
NOW = datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc)
DEN = 1610612743
MURRAY = {
    "player_id": 1627750,
    "name": "Jamal Murray",
    "minutes": 946,
    "possessions": 1856,
    "usage": 29.62,
    "totals": {"pts": 677, "reb": 112, "ast": 222, "fg3m": 75, "fga": 493},
    "per36": {"pts": 25.76, "reb": 4.26, "ast": 8.45, "fg3m": 2.85, "fga": 18.76},
}


class FakeProvider:
    """The PBPStatsProvider interface, serving the recorded payload."""

    def __init__(self):
        self.calls = []
        self.fail = False

    def get_wowy(self, *, team_id, season, on_player_ids=(), off_player_ids=(),
                 season_type="Regular Season"):
        self.calls.append((team_id, season, tuple(on_player_ids), tuple(off_player_ids)))
        if self.fail:
            raise ProviderUnavailableError("PBP Stats could not be reached.")
        return PBPTotalsAdapter.parse_wowy(json.loads(FIXTURE.read_text()))


class FakeRedis:
    """Dict-backed Redis with a manual clock that ``sleep`` advances."""

    def __init__(self):
        self.data = {}
        self.expiries = {}
        self.now_ms = 0
        self.slots = {}
        self.fail_cache_writes = False
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now_ms += seconds * 1000

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None, nx=False, px=None):
        if px is not None:  # a throttle slot
            if nx and self.slots.get(key, 0) > self.now_ms:
                return None
            self.slots[key] = self.now_ms + px
            return True
        if self.fail_cache_writes:
            raise ConnectionError("redis down")
        self.data[key] = value
        self.expiries[key] = ex
        return True

    def pttl(self, key):
        return int(self.slots.get(key, 0) - self.now_ms)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def redis():
    return FakeRedis()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(provider, redis, clock):
    return OnOffService(
        provider, RuntimeSettings(environment="testing",
                                  cache=CacheSettings(on_off_min_interval_seconds=2)),
        redis_client=redis, clock=clock, sleep=redis.sleep,
    )


@pytest.fixture
def api(app, dependencies, service, authenticate):
    dependencies.on_off_service = service
    headers = authenticate()
    client = app.test_client()
    return lambda query: client.get(f"/api/on_off?{query}", headers=headers)


QUERY = "team=DEN&season=2025-26&off[]=203999"


def test_on_off_returns_totals_per36_and_provenance(api, provider):
    body = api(QUERY).get_json()

    assert body["status"] == "ok"
    assert (body["source"], body["team"], body["season"]) == ("pbpstats", "DEN", "2025-26")
    assert (body["on"], body["off"]) == ([], [203999])
    assert body["cached_at"] == "2026-10-16T18:00:00+00:00"
    assert body["stale"] is False
    assert body["players"][0] == MURRAY
    # Players with no minutes in this lineup state are dropped.
    assert [p["player_id"] for p in body["players"]] == [1627750, 203932, 1642354, 1631223]
    assert body["players"][3]["per36"] == {
        "pts": 10.29, "reb": 12.86, "ast": 5.14, "fg3m": 0.0, "fga": 12.86,
    }
    assert provider.calls == [(DEN, "2025-26", (), (203999,))]


def test_second_read_is_a_cache_hit_without_an_upstream_call(api, provider):
    first = api(QUERY).get_json()
    second = api(QUERY).get_json()

    assert second == first
    assert len(provider.calls) == 1


def test_on_and_off_sets_are_order_insensitive_and_part_of_the_key(api, provider):
    api("team=DEN&season=2025-26&on[]=1627750&off[]=203999&off[]=203932")
    api("team=den&season=2025-26&on[]=1627750&off[]=203932&off[]=203999")
    api("team=DEN&season=2025-26&off[]=203999&off[]=203932")

    assert provider.calls == [
        (DEN, "2025-26", (1627750,), (203932, 203999)),
        (DEN, "2025-26", (), (203932, 203999)),
    ]


def test_failed_fetch_serves_cached_copy_marked_stale(api, provider, clock):
    fresh = api(QUERY).get_json()
    clock.now = NOW + timedelta(hours=25)
    provider.fail = True

    body = api(QUERY).get_json()

    assert body["status"] == "ok"
    assert body["stale"] is True
    assert body["cached_at"] == fresh["cached_at"]
    assert body["players"] == fresh["players"]
    assert len(provider.calls) == 2


def test_failed_fetch_without_a_cached_copy_is_unavailable(api, provider):
    provider.fail = True

    response = api(QUERY)

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "unavailable", "source": "pbpstats", "team": "DEN",
        "season": "2025-26", "on": [], "off": [203999], "cached_at": None,
        "stale": False, "players": [],
    }


def test_expired_entry_is_refetched_and_rewritten(api, provider, clock, redis):
    api(QUERY)
    clock.now = NOW + timedelta(hours=24, seconds=1)

    body = api(QUERY).get_json()

    assert body["stale"] is False
    assert body["cached_at"] == "2026-10-17T18:00:01+00:00"
    assert len(provider.calls) == 2
    (key,) = redis.data
    assert key == "on_off:v1:DEN:2025-26:on=:off=203999"
    assert redis.expiries[key] == 86400 + 7 * 86400


@pytest.mark.parametrize(
    "query",
    [
        "season=2025-26&off[]=203999",
        "team=XXX&season=2025-26&off[]=203999",
        "team=DEN&season=2025&off[]=203999",
        "team=DEN&season=2025-26",
        "team=DEN&season=2025-26&off[]=abc",
        "team=DEN&season=2025-26&on[]=5&off[]=5",
        "team=DEN&season=2025-26&on[]=1&on[]=2&on[]=3&on[]=4&on[]=5&on[]=6",
        "team=DEN&season=2025-26&off[]="
        + "&off[]=".join(str(n) for n in range(1, 17)),
    ],
)
def test_invalid_requests_are_400_and_never_reach_the_provider(api, provider, query):
    assert api(query).status_code == 400
    assert provider.calls == []


def test_without_a_cache_every_read_fetches_and_failure_is_unavailable(
    provider, clock
):
    service = OnOffService(
        provider, RuntimeSettings(environment="testing", cache=CacheSettings(enabled=False)),
        redis_client=None, clock=clock, sleep=lambda s: None,
    )

    assert service.get("DEN", "2025-26", off=[203999])["players"][0] == MURRAY
    service.get("DEN", "2025-26", off=[203999])
    assert len(provider.calls) == 2
    provider.fail = True
    assert service.get("DEN", "2025-26", off=[203999])["status"] == "unavailable"


def test_in_process_throttle_spaces_calls_when_there_is_no_cache(provider, clock):
    ticks = iter([100.0, 100.5, 102.5])
    sleeps = []
    service = OnOffService(
        provider,
        RuntimeSettings(environment="testing",
                        cache=CacheSettings(on_off_min_interval_seconds=2)),
        redis_client=None, clock=clock, sleep=sleeps.append,
        monotonic=lambda: next(ticks),
    )

    service.get("DEN", "2025-26", off=[1])  # t=100.0, no wait
    service.get("DEN", "2025-26", off=[2])  # t=100.5, waits 1.5s

    assert sleeps == [1.5]


def test_two_instances_sharing_redis_are_spaced_by_the_interval(provider, redis, clock):
    def instance():
        return OnOffService(
            provider,
            RuntimeSettings(environment="testing",
                            cache=CacheSettings(on_off_min_interval_seconds=2)),
            redis_client=redis, clock=clock, sleep=redis.sleep,
        )

    first, second = instance(), instance()

    first.get("DEN", "2025-26", off=[1])
    assert redis.sleeps == []
    second.get("DEN", "2025-26", off=[2])  # other worker, same slot
    assert redis.sleeps == [2.0]
    first.get("DEN", "2025-26", off=[3])
    assert redis.sleeps == [2.0, 2.0]
    assert len(provider.calls) == 3


def test_redis_failure_after_an_admission_still_spaces_local_calls(provider, clock):
    class FlakyRedis(FakeRedis):
        def set(self, key, value, ex=None, nx=False, px=None):
            if px is not None and self.slots:
                raise ConnectionError("redis down")
            return super().set(key, value, ex=ex, nx=nx, px=px)

    redis = FlakyRedis()
    ticks = iter([10.0, 10.5, 12.5])
    sleeps = []
    service = OnOffService(
        provider,
        RuntimeSettings(environment="testing",
                        cache=CacheSettings(on_off_min_interval_seconds=2)),
        redis_client=redis, clock=clock, sleep=sleeps.append,
        monotonic=lambda: next(ticks),
    )

    service.get("DEN", "2025-26", off=[1])  # admitted by Redis at t=10.0
    service.get("DEN", "2025-26", off=[2])  # Redis fails; local fallback waits

    assert sleeps == [1.5]
    assert len(provider.calls) == 2


def _held_slot_service(provider, redis, clock):
    redis.slots["on_off:throttle"] = 10**12  # someone else always holds it
    return OnOffService(
        provider,
        RuntimeSettings(environment="testing",
                        cache=CacheSettings(on_off_min_interval_seconds=2)),
        redis_client=redis, clock=clock, sleep=redis.sleeps.append,
    )


def test_exhausted_throttle_slot_does_not_fetch(provider, redis, clock):
    service = _held_slot_service(provider, redis, clock)

    body = service.get("DEN", "2025-26", off=[1])

    assert provider.calls == []
    assert body["status"] == "unavailable"
    assert len(redis.sleeps) == 50


def test_exhausted_throttle_slot_serves_the_stale_copy(provider, redis, clock, service):
    fresh = service.get("DEN", "2025-26", off=[1])
    clock.now = NOW + timedelta(hours=25)
    service = _held_slot_service(provider, redis, clock)

    body = service.get("DEN", "2025-26", off=[1])

    assert len(provider.calls) == 1
    assert (body["status"], body["stale"], body["cached_at"]) == (
        "ok", True, fresh["cached_at"],
    )


def test_on_off_requires_authentication(app, dependencies, service, authenticate, provider):
    dependencies.on_off_service = service
    authenticate()  # Firebase is configured; this request sends no token

    response = app.test_client().get(f"/api/on_off?{QUERY}")

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"
    assert provider.calls == []


def test_off_set_may_name_a_whole_injured_rotation(api, provider):
    off = "&".join(f"off[]={n}" for n in range(1, 16))

    assert api(f"team=DEN&season=2025-26&{off}").status_code == 200
    assert provider.calls[0][3] == tuple(range(1, 16))


def test_two_on_players_are_sent_as_one_combination(api, provider):
    body = api("team=DEN&season=2025-26&on[]=1627750&on[]=203999").get_json()

    assert body["on"] == [203999, 1627750]
    assert provider.calls == [(DEN, "2025-26", (203999, 1627750), ())]


ENTRIES = [
    {"player_id": 203999, "team": "DEN", "status": "Questionable"},
    {"player_id": 1628369, "team": "BOS", "status": "Out"},
    {"player_id": 5, "team": "LAL", "status": "Probable"},
    {"player_id": None, "team": "LAL", "status": "Out"},
]


def test_injury_warm_requests_cover_listed_players_both_ways():
    requests = injury_warm_requests(ENTRIES, "2025-26")

    assert [(r.team, r.on, r.off) for r in requests] == [
        ("DEN", (), (203999,)), ("DEN", (203999,), ()),
        ("BOS", (), (1628369,)), ("BOS", (1628369,), ()),
    ]


def test_warm_is_idempotent_within_the_ttl(service, provider, clock):
    requests = injury_warm_requests(ENTRIES, "2025-26")

    first = service.warm(requests)
    assert first == {"skipped_reason": None, "fetched": 4, "fresh": 0,
                     "failed": 0, "aborted": False, "cache_write_failed": False}
    assert len(provider.calls) == 4

    clock.now = NOW + timedelta(hours=23)
    second = service.warm(requests)
    assert second == {"skipped_reason": None, "fetched": 0, "fresh": 4,
                      "failed": 0, "aborted": False, "cache_write_failed": False}
    assert len(provider.calls) == 4

    clock.now = NOW + timedelta(hours=25)
    assert service.warm(requests)["fetched"] == 4
    assert len(provider.calls) == 8


def test_warm_aborts_after_three_consecutive_failures(service, provider):
    provider.fail = True

    result = service.warm(injury_warm_requests(ENTRIES, "2025-26") * 2)

    assert result == {"skipped_reason": None, "fetched": 0, "fresh": 0,
                      "failed": 3, "aborted": True, "cache_write_failed": False}
    assert len(provider.calls) == 3


def test_warm_stops_when_a_fetched_split_cannot_be_stored(service, provider, redis):
    redis.fail_cache_writes = True

    result = service.warm(injury_warm_requests(ENTRIES, "2025-26"))

    assert result == {"skipped_reason": None, "fetched": 0, "fresh": 0,
                      "failed": 0, "aborted": True, "cache_write_failed": True}
    assert len(provider.calls) == 1


def test_warm_without_a_cache_makes_no_upstream_calls(provider):
    service = OnOffService(
        provider, RuntimeSettings(environment="testing", cache=CacheSettings(enabled=False)),
        redis_client=None,
    )

    result = service.warm(injury_warm_requests(ENTRIES, "2025-26"))

    assert result["skipped_reason"] == "cache_disabled"
    assert provider.calls == []


class FakeListing:
    """StoredInjuryListing's ``list_injuries`` for a given date."""

    def __init__(self, injuries_by_date):
        self.injuries_by_date = injuries_by_date
        self.asked = []

    def list_injuries(self, requested_date, team):
        self.asked.append((requested_date, team))
        return {"date": requested_date, "team": team,
                "injuries": self.injuries_by_date.get(requested_date, [])}


def test_warm_script_warms_the_listing_for_the_target_day(service, provider):
    from scripts.on_off_warm import warm

    listing = FakeListing({"2026-10-16": ENTRIES})

    result = warm(listing, service, "2025-26", date(2026, 10, 16))
    assert (result["state"], result["requests"], result["fetched"]) == ("warmed", 4, 4)
    assert listing.asked == [("2026-10-16", None)]

    rerun = warm(listing, service, "2025-26", date(2026, 10, 16))
    assert (rerun["fetched"], rerun["fresh"]) == (0, 4)
    assert len(provider.calls) == 4


def test_warm_script_does_nothing_when_nobody_is_listed(service, provider):
    from scripts.on_off_warm import warm

    result = warm(FakeListing({}), service, "2025-26", date(2026, 10, 17))

    assert result == {"state": "no_listed_players", "requests": 0}
    assert provider.calls == []


def test_warm_script_exits_nonzero_when_the_cache_cannot_be_written(
    monkeypatch, service, redis
):
    from scripts import on_off_warm

    redis.fail_cache_writes = True
    dependencies = SimpleNamespace(
        on_off_service=service,
        injury_listing_service=FakeListing({"2026-10-16": ENTRIES}),
        event_catalog_service=None,
        engine=SimpleNamespace(dispose=lambda: None),
    )
    monkeypatch.setattr("app.dependencies.build_dependencies", lambda settings: dependencies)

    assert on_off_warm.main(["--date", "2026-10-16", "--season", "2025-26"]) == 1


def test_warm_script_exits_nonzero_when_the_report_is_unavailable(monkeypatch, service):
    from scripts import on_off_warm

    class Down:
        def list_injuries(self, requested_date, team):
            raise ProviderUnavailableError("The injury report is not enabled.")

    dependencies = SimpleNamespace(
        on_off_service=service, injury_listing_service=Down(),
        event_catalog_service=None, engine=SimpleNamespace(dispose=lambda: None),
    )
    monkeypatch.setattr("app.dependencies.build_dependencies", lambda settings: dependencies)

    assert on_off_warm.main(["--date", "2026-10-16", "--season", "2025-26"]) == 1
