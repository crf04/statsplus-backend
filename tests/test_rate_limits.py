"""Rate limits, the OpenAI fallback cap, and admin-only provider health
(crf04/statsplus-backend#359, contract crf04/statsplus#157).

The limiter counts for real: the app's store is Flask-Limiter's memory
storage, which the same code path uses for Redis.  Only the "Redis is down"
test points the app at an address nothing listens on.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone
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
from app.services import llm_service as llm_service_module
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
    """Verify a bearer token ``<uid>`` or ``<uid>:<n>`` as that Firebase uid."""
    import app.utils.auth as auth

    claims = {"admin": True, "role": "admin"} if admin else {}
    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {
            "uid": token.split(":")[0],
            "email": f"{token.split(':')[0]}@example.com",
            **claims,
        },
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


def test_31st_preview_in_a_minute_is_the_exact_429_and_another_user_is_unaffected(client, signed_in, served):
    for _ in range(30):
        assert client.post("/api/user/targets/preview", json={}, headers=_as("user-a")).status_code == 200

    limited = client.post("/api/user/targets/preview", json={}, headers=_as("user-a"))

    assert limited.status_code == 429
    assert limited.get_json() == RATE_LIMITED_BODY
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    other = client.post("/api/user/targets/preview", json={}, headers=_as("user-b"))
    assert other.status_code == 200


def test_a_user_is_limited_by_uid_whatever_ip_they_come_from(client, signed_in, served):
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
def test_each_authenticated_route_allows_exactly_its_limit(client, signed_in, served, method, path, allowed):
    kwargs = {"json": {"query": "points"}} if method == "post" else {}

    statuses = _statuses(client, method, path, allowed + 1, headers=_as("user-a"), **kwargs)

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


def test_health_and_preflights_are_never_limited(app, client):
    app.add_url_rule("/health", "health_probe", lambda: ({"status": "ok"}, 200))

    assert _statuses(client, "get", "/health", 70) == [200] * 70
    assert 429 not in _statuses(
        client, "options", "/api/health/db", 70, headers={"Origin": "http://localhost:3000"}
    )


def test_collector_routes_are_not_limited_as_anonymous_callers(client):
    path = "/api/collector/status"

    statuses = _statuses(client, "post", path, 70, json={})

    assert 429 not in statuses
    assert set(statuses) == {401}


def test_a_signed_in_caller_on_an_optional_auth_route_is_limited_by_uid(client, signed_in):
    # /api/players authenticates optionally: 300/min per uid, not 60/min per IP.
    statuses = _statuses(
        client,
        "get",
        "/api/players",
        301,
        headers=_as("user-a", **{"X-Forwarded-For": "198.51.100.1"}),
    )

    assert 429 not in statuses[:300]
    assert statuses[300] == 429
    same_ip_other_user = client.get(
        "/api/players", headers=_as("user-b", **{"X-Forwarded-For": "198.51.100.1"})
    )
    same_user_other_ip = client.get(
        "/api/players", headers=_as("user-a", **{"X-Forwarded-For": "203.0.113.5"})
    )
    assert same_ip_other_user.status_code != 429
    assert same_user_other_ip.status_code == 429


def test_each_app_counts_for_itself(app, client, signed_in, served, dependencies):
    _statuses(client, "post", "/api/user/targets/preview", 30, json={}, headers=_as("user-a"))

    second_app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": dependencies.settings,
            "DEPENDENCIES": dependencies,
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )

    first_apps_next = client.post("/api/user/targets/preview", json={}, headers=_as("user-a"))
    fresh = second_app.test_client().post("/api/user/targets/preview", json={}, headers=_as("user-a"))
    assert first_apps_next.status_code == 429
    assert fresh.status_code == 200


def test_a_refreshed_token_is_the_same_user(client, signed_in, served):
    for refresh in range(30):
        client.post("/api/user/targets/preview", json={}, headers=_as(f"user-a:{refresh}"))

    limited = client.post("/api/user/targets/preview", json={}, headers=_as("user-a:new-token"))

    assert limited.status_code == 429


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
            client,
            "post",
            "/api/user/targets/preview",
            31,
            json={},
            headers=_as("user-a"),
        )

    assert statuses == [200] * 31
    assert any(
        record.levelno == logging.WARNING and "Rate limit store unavailable" in record.getMessage()
        for record in caplog.records
    )


# --- OpenAI fallback cap ---------------------------------------------------


def test_budget_allows_50_calls_per_user_per_utc_day():
    clock = {"now": datetime(2026, 10, 14, 23, 59, tzinfo=UTC)}
    budget = LLMDailyBudget(MemoryStorage(), now=lambda: clock["now"])

    first_50 = [budget.try_consume("user-a") for _ in range(50)]

    assert first_50 == [True] * 50
    assert budget.try_consume("user-a") is False
    assert budget.try_consume("user-b") is True
    clock["now"] = datetime(2026, 10, 15, 0, 1, tzinfo=UTC)
    assert budget.try_consume("user-a") is True


def test_budget_day_is_the_utc_day_whatever_zone_the_clock_reports():
    new_york = timezone(timedelta(hours=-4))
    clock = {"now": datetime(2026, 10, 14, 21, 0, tzinfo=new_york)}  # 01:00 UTC Oct 15
    budget = LLMDailyBudget(MemoryStorage(), limit=1, now=lambda: clock["now"])
    assert budget.try_consume("user-a") is True

    # Midnight in New York has passed, but it is 05:00 UTC on the same UTC day.
    clock["now"] = datetime(2026, 10, 15, 1, 0, tzinfo=new_york)
    assert budget.try_consume("user-a") is False
    # 20:30 on the 15th in New York is 00:30 UTC on the 16th: a new day.
    clock["now"] = datetime(2026, 10, 15, 20, 30, tzinfo=new_york)
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
        return QueryComponents(
            player_name="LeBron James",
            confidence=0.4,
            raw_query=query,
            intent="game_logs",
            confidence_breakdown=SimpleNamespace(should_use_llm=True),
        )

    def resolve_player_name(self, name):
        return name


class FakeOpenAI:
    """The OpenAI SDK client: counts calls, answers each with a parsed query."""

    instances: list[FakeOpenAI] = []

    def __init__(self, api_key=None, fail=False, **options):
        self.calls = 0
        self.fail = fail
        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse))
        FakeOpenAI.instances.append(self)

    def _parse(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise ConnectionError("openai is down")
        parsed = LLMParsedQuery(
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
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=parsed))]
        )


@pytest.fixture
def fake_openai(monkeypatch):
    FakeOpenAI.instances = []
    monkeypatch.setattr(nl_service_module, "BaseQueryParser", FakeParser)
    monkeypatch.setattr(llm_service_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(llm_service_module.time, "sleep", lambda seconds: None)
    return FakeOpenAI


def _nl_service(runtime_settings, *, limit, max_retries=1):
    settings = runtime_settings.model_copy(
        update={"llm": LLMSettings(api_key="sk-test", enable_fallback=True, max_retries=max_retries)}
    )
    return NLService(
        Mock(),
        settings=settings,
        llm_budget=LLMDailyBudget(MemoryStorage(), limit=limit),
    )


def test_nl_query_past_the_daily_openai_cap_answers_200_with_the_rule_based_parse(
    client, signed_in, dependencies, runtime_settings, fake_openai
):
    dependencies.nl_service = _nl_service(runtime_settings, limit=2)

    bodies = [
        client.post("/api/nl-query", json={"query": "lebron stuff"}, headers=_as("user-a")) for _ in range(3)
    ]

    assert [response.status_code for response in bodies] == [200, 200, 200]
    assert [response.get_json()["parsed_by"] for response in bodies] == [
        "hybrid",
        "hybrid",
        "nlp",
    ]
    assert bodies[2].get_json()["original_query"] == "lebron stuff"
    assert bodies[2].get_json()["player_name"] == "LeBron James"
    assert fake_openai.instances[0].calls == 2
    # Another user still has a budget.
    other = client.post("/api/nl-query", json={"query": "lebron stuff"}, headers=_as("user-b"))
    assert other.get_json()["parsed_by"] == "hybrid"
    assert fake_openai.instances[0].calls == 3


def test_every_openai_attempt_counts_against_the_cap_retries_included(
    client, signed_in, dependencies, runtime_settings, fake_openai, monkeypatch
):
    dependencies.nl_service = _nl_service(runtime_settings, limit=2, max_retries=2)
    openai = fake_openai.instances[0]
    openai.fail = True

    first = client.post("/api/nl-query", json={"query": "lebron stuff"}, headers=_as("user-a"))
    second = client.post("/api/nl-query", json={"query": "lebron stuff"}, headers=_as("user-a"))

    # The first query's two attempts spent the whole budget of 2.
    assert openai.calls == 2
    assert first.status_code == second.status_code == 200
    assert second.get_json()["parsed_by"] == "nlp"


# --- admin-only provider health -------------------------------------------

HEALTH_ROUTES = [
    "/api/health/nba-api",
    "/api/health/pbp-api",
    "/api/health/pbp-stats",
    "/api/health/detailed",
]


@pytest.mark.parametrize("path", HEALTH_ROUTES)
def test_provider_health_refuses_anonymous_and_non_admin_callers(client, signed_in, path):
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


def test_bodies_up_to_one_megabyte_are_accepted_and_larger_ones_refused(client, signed_in, served):
    near_cap = client.post(
        "/api/nl-query",
        data=b'{"query": "' + b"x" * (900 * 1024) + b'"}',
        content_type="application/json",
        headers=_as("user-a"),
    )
    oversized = client.post(
        "/api/nl-query",
        data=b"x" * (1024 * 1024 + 1),
        content_type="application/json",
        headers=_as("user-a"),
    )

    assert near_cap.status_code == 200
    assert oversized.status_code == 413
    assert oversized.get_json() == {
        "error": {
            "code": "payload_too_large",
            "message": "The request body is too large.",
        }
    }


def _gzip_catalog_body(incompressible_bytes: int) -> bytes:
    import base64
    import gzip
    import json
    import os

    pad = base64.b64encode(os.urandom(incompressible_bytes)).decode()
    return gzip.compress(json.dumps({"payload": pad}).encode(), compresslevel=1)


def _post_catalog(client, monkeypatch, body: bytes):
    from app.routes import collection_routes

    monkeypatch.setattr(collection_routes, "_collector_claims_any", lambda *a, **k: object())
    return client.post(
        "/api/collector/catalog/req-1",
        data=body,
        headers={"Content-Encoding": "gzip"},
        content_type="application/octet-stream",
    )


def test_collector_envelope_over_one_megabyte_is_read_in_full(client, monkeypatch):
    body = _gzip_catalog_body(1_100_000)
    assert 1024 * 1024 < len(body) < 2 * 1024 * 1024

    response = _post_catalog(client, monkeypatch, body)

    # The envelope was read, decompressed and parsed: the first thing wrong
    # with it is the missing catalog version, not its size.
    assert response.status_code == 400
    assert response.get_json()["error"]["message"] == ("A catalog version and payload are required.")


def test_collector_envelope_over_its_own_two_megabyte_bound_is_refused(client, monkeypatch):
    body = _gzip_catalog_body(2_100_000)
    assert len(body) > 2 * 1024 * 1024

    response = _post_catalog(client, monkeypatch, body)

    assert response.status_code == 400
    assert response.get_json()["error"]["message"] == ("The compressed request body is too large.")


def test_the_openai_sdk_makes_no_calls_the_budget_does_not_count(runtime_settings, monkeypatch):
    import httpx
    import openai

    requests = []

    def unavailable(request):
        requests.append(request)
        return httpx.Response(500, json={"error": {"message": "overloaded"}})

    monkeypatch.setattr(
        llm_service_module,
        "OpenAI",
        lambda **kwargs: openai.OpenAI(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(unavailable))
        ),
    )
    monkeypatch.setattr(llm_service_module.time, "sleep", lambda seconds: None)
    settings = runtime_settings.model_copy(
        update={"llm": LLMSettings(api_key="sk-test", enable_fallback=True, max_retries=1)}
    )
    service = llm_service_module.LLMService(settings=settings)

    with pytest.raises(llm_service_module.LLMError):
        service.parse_query("lebron stuff")

    assert len(requests) == 1
