"""Rate limits, the OpenAI fallback cap, and admin-only provider health
(crf04/statsplus-backend#359, contract crf04/statsplus#157).

The limiter counts for real: the app's store is Flask-Limiter's memory
storage, which the same code path uses for Redis.  Only the "Redis is down"
test points the app at an address nothing listens on.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from limits.storage import MemoryStorage

from app import create_app
from app.config.settings import (
    AuthenticationSettings,
    CacheSettings,
    LLMSettings,
    RuntimeSettings,
)
from app.services import nl_service as nl_service_module
from app.services.llm_budget import LLMDailyBudget
from app.services.llm_service import LLMParsedQuery
from app.services.nl_query.parser import QueryComponents
from app.services.nl_service import NLService

RATE_LIMITED_BODY = {
    "error": {
        "code": "rate_limited",
        "message": "Too many requests. Try again in a few seconds.",
    }
}


def _sign_in(monkeypatch, *, admin: bool = False) -> None:
    """Verify any bearer token as a Firebase user whose uid is the token."""
    import app.utils.auth as auth

    claims = {"admin": True, "role": "admin"} if admin else {}
    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {"uid": token, "email": f"{token}@example.com", **claims},
    )


def _as(uid: str, **headers: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}", **headers}


@pytest.fixture
def signed_in(monkeypatch):
    _sign_in(monkeypatch)


@pytest.fixture
def served(dependencies):
    """Services behind the limited routes answer instead of failing."""
    dependencies.target_preview_service = Mock()
    dependencies.target_preview_service.preview.return_value = {"players": []}
    dependencies.target_backtest_service = Mock()
    dependencies.target_backtest_service.backtest.return_value = ({"players": []}, "hit")
    dependencies.nl_service.process_query.return_value = {"parsed_by": "nlp"}
    dependencies.dfs_board_response_service = Mock()
    return dependencies


def _statuses(client, method, path, count, **kwargs):
    return [getattr(client, method)(path, **kwargs).status_code for _ in range(count)]


# --- the 429 contract ------------------------------------------------------


def test_31st_preview_in_a_minute_is_the_exact_429_and_another_user_is_unaffected(
    client, signed_in, served
):
    for _ in range(30):
        assert (
            client.post("/api/user/targets/preview", json={}, headers=_as("user-a")).status_code
            == 200
        )

    limited = client.post("/api/user/targets/preview", json={}, headers=_as("user-a"))

    assert limited.status_code == 429
    assert limited.get_json() == RATE_LIMITED_BODY
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    other = client.post("/api/user/targets/preview", json={}, headers=_as("user-b"))
    assert other.status_code == 200


def test_a_user_is_limited_by_uid_whatever_ip_they_come_from(
    client, signed_in, served
):
    for hop in range(30):
        client.post(
            "/api/user/targets/preview",
            json={},
            headers=_as("user-a", **{"X-Forwarded-For": f"198.51.100.{hop}"}),
        )

    limited = client.post(
        "/api/user/targets/preview",
        json={},
        headers=_as("user-a", **{"X-Forwarded-For": "203.0.113.200"}),
    )

    assert limited.status_code == 429


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("post", "/api/user/targets/preview", 30),
        ("get", "/api/user/targets/7/backtest", 60),
        ("post", "/api/nl-query", 30),
        ("get", "/api/dfs/board", 30),
        ("get", "/api/user/profile", 300),
    ],
)
def test_each_authenticated_route_allows_exactly_its_limit(
    client, signed_in, served, method, path, allowed
):
    kwargs = {"json": {"query": "points"}} if method == "post" else {}

    statuses = _statuses(
        client, method, path, allowed + 1, headers=_as("user-a"), **kwargs
    )

    assert 429 not in statuses[:allowed]
    assert statuses[allowed] == 429


def test_unauthenticated_routes_are_limited_per_ip_behind_the_proxy(client):
    path = "/api/health/db"
    proxied = {"X-Forwarded-For": "198.51.100.7, 10.0.0.2"}

    statuses = _statuses(client, "get", path, 61, headers=proxied)

    assert 429 not in statuses[:60]
    assert statuses[60] == 429
    # Same proxy address, a different first hop: a different client.
    neighbour = client.get(path, headers={"X-Forwarded-For": "198.51.100.8, 10.0.0.2"})
    assert neighbour.status_code != 429
    # Same first hop reaching us through another proxy address is the same client.
    same_client = client.get(path, headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.9"})
    assert same_client.status_code == 429


def test_health_and_preflights_are_never_limited(client):
    assert 429 not in _statuses(client, "get", "/health", 70)
    assert 429 not in _statuses(
        client, "options", "/api/health/db", 70, headers={"Origin": "http://localhost:3000"}
    )


# --- Redis unavailable -----------------------------------------------------


def test_requests_are_allowed_and_a_warning_logged_when_redis_is_down(
    monkeypatch, runtime_settings, dependencies, signed_in, served, caplog
):
    settings = RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=True, url="redis://127.0.0.1:1/0"),
    )
    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": dependencies,
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )
    client = app.test_client()

    with caplog.at_level(logging.WARNING):
        statuses = _statuses(
            client, "post", "/api/user/targets/preview", 31,
            json={}, headers=_as("user-a"),
        )

    assert statuses == [200] * 31
    assert any(
        record.levelno == logging.WARNING
        and "Rate limit store unavailable" in record.getMessage()
        for record in caplog.records
    )


# --- OpenAI fallback cap ---------------------------------------------------


def test_budget_allows_50_calls_per_user_per_utc_day():
    clock = {"now": datetime(2026, 10, 14, 23, 59, tzinfo=timezone.utc)}
    budget = LLMDailyBudget(MemoryStorage(), now=lambda: clock["now"])

    first_50 = [budget.try_consume("user-a") for _ in range(50)]

    assert first_50 == [True] * 50
    assert budget.try_consume("user-a") is False
    assert budget.try_consume("user-b") is True
    clock["now"] = datetime(2026, 10, 15, 0, 1, tzinfo=timezone.utc)
    assert budget.try_consume("user-a") is True


def test_budget_allows_the_call_and_warns_when_its_store_fails(caplog):
    class DeadStorage:
        def incr(self, *args, **kwargs):
            raise ConnectionError("redis is down")

    with caplog.at_level(logging.WARNING):
        allowed = LLMDailyBudget(DeadStorage()).try_consume("user-a")

    assert allowed is True
    assert "LLM fallback budget store unavailable" in caplog.text


class FakeParser:
    """A parser whose every query is below the LLM confidence threshold."""

    def __init__(self, engine, settings=None):
        pass

    def parse(self, query):
        breakdown = SimpleNamespace(should_use_llm=True)
        return QueryComponents(
            player_name="LeBron James",
            confidence=0.4,
            raw_query=query,
            intent="game_logs",
            confidence_breakdown=breakdown,
        )

    def resolve_player_name(self, name):
        return name


class FakeOpenAI:
    calls: list[str] = []

    def __init__(self, settings=None):
        pass

    def parse_query(self, query, player_context):
        FakeOpenAI.calls.append(query)
        return LLMParsedQuery(
            player_name="LeBron James",
            team_name=None,
            game_count=None,
            date_range=None,
            location=None,
            minutes_filter=None,
            self_filters=[],
            opponent_filters=[],
            players_on=[],
            players_off=[],
            season=None,
            intent="game_logs",
            time_period=None,
            confidence=0.95,
        )


def test_nl_query_past_the_daily_openai_cap_answers_200_with_the_rule_based_parse(
    monkeypatch, client, signed_in, dependencies, runtime_settings
):
    monkeypatch.setattr(nl_service_module, "BaseQueryParser", FakeParser)
    monkeypatch.setattr(nl_service_module, "LLMService", FakeOpenAI)
    FakeOpenAI.calls = []
    llm_settings = runtime_settings.model_copy(
        update={"llm": LLMSettings(api_key="sk-test", enable_fallback=True)}
    )
    dependencies.nl_service = NLService(
        Mock(),
        settings=llm_settings,
        llm_budget=LLMDailyBudget(MemoryStorage(), limit=2),
    )

    bodies = [
        client.post(
            "/api/nl-query", json={"query": "lebron stuff"}, headers=_as("user-a")
        )
        for _ in range(3)
    ]

    assert [response.status_code for response in bodies] == [200, 200, 200]
    assert [response.get_json()["parsed_by"] for response in bodies] == [
        "hybrid",
        "hybrid",
        "nlp",
    ]
    assert bodies[2].get_json()["original_query"] == "lebron stuff"
    assert bodies[2].get_json()["player_name"] == "LeBron James"
    assert FakeOpenAI.calls == ["lebron stuff", "lebron stuff"]
    # Another user still has a budget.
    other = client.post(
        "/api/nl-query", json={"query": "lebron stuff"}, headers=_as("user-b")
    )
    assert other.get_json()["parsed_by"] == "hybrid"


# --- admin-only provider health -------------------------------------------

HEALTH_ROUTES = [
    "/api/health/nba-api",
    "/api/health/pbp-api",
    "/api/health/pbp-stats",
    "/api/health/detailed",
]


@pytest.mark.parametrize("path", HEALTH_ROUTES)
def test_provider_health_refuses_anonymous_and_non_admin_callers(
    client, signed_in, path
):
    anonymous = client.get(path)
    signed_in_user = client.get(path, headers=_as("user-a"))

    assert anonymous.status_code == 401
    assert signed_in_user.status_code == 403
    assert signed_in_user.get_json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize(
    ("path", "provider"),
    [
        ("/api/health/nba-api", "nba_stats"),
        ("/api/health/pbp-api", "pbp_stats"),
        ("/api/health/pbp-stats", "pbp_stats"),
    ],
)
def test_provider_health_answers_an_admin(client, monkeypatch, path, provider):
    _sign_in(monkeypatch, admin=True)

    response = client.get(path, headers=_as("admin-a"))

    assert response.status_code == 200
    assert response.get_json()["provider"] == provider


def test_detailed_health_answers_an_admin(client, monkeypatch):
    _sign_in(monkeypatch, admin=True)

    response = client.get("/api/health/detailed", headers=_as("admin-a"))

    assert response.status_code == 200
    assert response.get_json()["status"] == "healthy"


# --- request size ----------------------------------------------------------


def test_bodies_over_one_megabyte_are_refused(client, signed_in, served):
    small = client.post(
        "/api/nl-query", json={"query": "x" * 1000}, headers=_as("user-a")
    )
    oversized = client.post(
        "/api/nl-query",
        data=b"x" * (1024 * 1024 + 1),
        content_type="application/json",
        headers=_as("user-a"),
    )

    assert small.status_code == 200
    assert oversized.status_code == 413
    assert oversized.get_json() == {
        "error": {
            "code": "payload_too_large",
            "message": "The request body is too large.",
        }
    }
