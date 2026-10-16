"""On/off splits: the /api/on_off route over a faked provider and Redis."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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
    def __init__(self):
        self.data = {}
        self.expiries = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key] = value
        self.expiries[key] = ex


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
    sleeps = []
    instance = OnOffService(
        provider, RuntimeSettings(environment="testing",
                                  cache=CacheSettings(on_off_min_interval_seconds=2)),
        redis_client=redis, clock=clock, sleep=sleeps.append,
        monotonic=lambda: 100.0,
    )
    instance.sleeps = sleeps
    return instance


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
        "team=DEN&season=2025-26&off[]=1&off[]=2&off[]=3&off[]=4&off[]=5&off[]=6",
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


def test_upstream_calls_are_spaced_by_the_minimum_interval(provider, redis, clock):
    ticks = iter([100.0, 100.5, 102.5])
    sleeps = []
    service = OnOffService(
        provider, RuntimeSettings(environment="testing", cache=CacheSettings(on_off_min_interval_seconds=2)),
        redis_client=redis, clock=clock, sleep=sleeps.append,
        monotonic=lambda: next(ticks),
    )

    service.get("DEN", "2025-26", off=[1])  # t=100.0, no wait
    service.get("DEN", "2025-26", off=[2])  # t=100.5, waits 1.5s

    assert sleeps == [1.5]


ENTRIES = [
    {"canonical_player_id": 203999, "tricode": "DEN", "canonical_status": "Questionable"},
    {"canonical_player_id": 1628369, "tricode": "BOS", "canonical_status": "Out"},
    {"canonical_player_id": 5, "tricode": "LAL", "canonical_status": "Probable"},
    {"canonical_player_id": None, "tricode": "LAL", "canonical_status": "Out"},
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
                     "failed": 0, "aborted": False}
    assert len(provider.calls) == 4

    clock.now = NOW + timedelta(hours=23)
    second = service.warm(requests)
    assert second == {"skipped_reason": None, "fetched": 0, "fresh": 4,
                      "failed": 0, "aborted": False}
    assert len(provider.calls) == 4

    clock.now = NOW + timedelta(hours=25)
    assert service.warm(requests)["fetched"] == 4
    assert len(provider.calls) == 8


def test_warm_aborts_after_three_consecutive_failures(service, provider):
    provider.fail = True

    result = service.warm(injury_warm_requests(ENTRIES, "2025-26") * 2)

    assert result == {"skipped_reason": None, "fetched": 0, "fresh": 0,
                      "failed": 3, "aborted": True}
    assert len(provider.calls) == 3


def test_warm_without_a_cache_makes_no_upstream_calls(provider):
    service = OnOffService(
        provider, RuntimeSettings(environment="testing", cache=CacheSettings(enabled=False)),
        redis_client=None,
    )

    result = service.warm(injury_warm_requests(ENTRIES, "2025-26"))

    assert result["skipped_reason"] == "cache_disabled"
    assert provider.calls == []


def test_warm_script_warms_only_todays_stored_report(service, provider):
    from scripts.on_off_warm import warm

    def repo(retrieved_at):
        snapshot = SimpleNamespace(normalized_entries=tuple(ENTRIES), retrieved_at=retrieved_at)
        return SimpleNamespace(get_latest_source=lambda source: snapshot)

    assert warm(repo(NOW - timedelta(days=1)), service, "2025-26", NOW) == {
        "state": "injury_report_not_today",
        "retrieved_at": "2026-10-15T18:00:00+00:00",
    }
    assert provider.calls == []
    result = warm(repo(NOW - timedelta(hours=2)), service, "2025-26", NOW)
    assert (result["state"], result["requests"], result["fetched"]) == ("warmed", 4, 4)
    assert warm(SimpleNamespace(get_latest_source=lambda s: None), service,
                "2025-26", NOW) == {"state": "no_injury_report"}
