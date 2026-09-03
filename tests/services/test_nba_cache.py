"""TDD contract tests for the Redis outage breaker in ``NBAGameCache``."""

from __future__ import annotations

import logging

import redis.exceptions

from app.config.settings import CacheSettings, RuntimeSettings
from app.services.nba_cache import NBAGameCache


def _settings() -> RuntimeSettings:
    return RuntimeSettings(environment="testing", cache=CacheSettings(enabled=True))


class ControlledClock:
    """A clock the test advances explicitly instead of sleeping."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRedisClient:
    """Small hand-written fake standing in for ``redis.Redis``."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.get_calls = 0
        self.setex_calls = 0
        self.delete_calls = 0
        self.info_calls = 0
        self.store: dict[str, bytes] = {}

    def get(self, key: str):
        self.get_calls += 1
        if self.error is not None:
            raise self.error
        return self.store.get(key)

    def setex(self, key: str, ttl: int, value: bytes) -> bool:
        self.setex_calls += 1
        if self.error is not None:
            raise self.error
        self.store[key] = value
        return True

    def delete(self, key: str) -> int:
        self.delete_calls += 1
        if self.error is not None:
            raise self.error
        return 1 if self.store.pop(key, None) is not None else 0

    def ping(self) -> bool:
        return True

    def info(self) -> dict:
        self.info_calls += 1
        if self.error is not None:
            raise self.error
        return {}

    def dbsize(self) -> int:
        return len(self.store)


def test_a_connection_error_opens_the_breaker_and_bypasses_the_client(caplog):
    client = FakeRedisClient(error=redis.exceptions.ConnectionError("down"))
    clock = ControlledClock()
    cache = NBAGameCache(client, settings=_settings(), clock=clock)

    with caplog.at_level(logging.WARNING):
        assert cache.get("key") is None
    assert client.get_calls == 1

    result = cache.get("key")
    assert result is None
    assert client.get_calls == 1

    assert cache.set("key", {"a": 1}, ttl=60) is False
    assert client.setex_calls == 0

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_the_breaker_closes_after_the_cooldown_elapses():
    client = FakeRedisClient(error=redis.exceptions.ConnectionError("down"))
    clock = ControlledClock()
    cache = NBAGameCache(client, settings=_settings(), clock=clock)

    assert cache.get("key") is None
    assert client.get_calls == 1

    clock.advance(30)
    client.error = None
    assert cache.get("key") is None
    assert client.get_calls == 2


def test_a_timeout_error_opens_the_breaker_the_same_way():
    client = FakeRedisClient(error=redis.exceptions.TimeoutError("slow"))
    clock = ControlledClock()
    cache = NBAGameCache(client, settings=_settings(), clock=clock)

    assert cache.get("key") is None
    assert client.get_calls == 1

    assert cache.get("key") is None
    assert client.get_calls == 1


def test_a_non_connection_redis_error_does_not_open_the_breaker():
    client = FakeRedisClient(error=redis.exceptions.ResponseError("bad command"))
    clock = ControlledClock()
    cache = NBAGameCache(client, settings=_settings(), clock=clock)

    assert cache.get("key") is None
    assert client.get_calls == 1

    assert cache.get("key") is None
    assert client.get_calls == 2


def test_cache_stats_report_whether_the_circuit_is_open():
    client = FakeRedisClient()
    clock = ControlledClock()
    cache = NBAGameCache(client, settings=_settings(), clock=clock)

    assert cache.get_cache_stats()["circuit_open"] is False
    assert client.info_calls == 1

    client.error = redis.exceptions.ConnectionError("down")
    cache.get("key")

    assert cache.get_cache_stats()["circuit_open"] is True
    assert client.info_calls == 1
