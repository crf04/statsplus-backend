"""The game-log link preview image (crf04/statsplus#285, contract
crf04/statsplus ``docs/contracts/game-log-preview.md``)."""

from __future__ import annotations

import io
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import redis
import requests
from PIL import Image
from sqlalchemy.exc import OperationalError

import app.utils.auth as auth
from app.errors import SeasonUnavailableError
from app.routes import game_routes
from app.services.game_log_preview import GameLogPreviewService, LineType, draw_fallback, preview_facts
from app.services.game_logs_source import StoredGameLogsSource
from app.services.game_service import GameService
from app.services.player_game_log_repository import PlayerGameLogRepository

CASES = json.loads((Path(__file__).parent / "fixtures" / "game_log_preview_cases.json").read_text())["cases"]
PREVIEW = (
    "/api/games/game_logs/preview.png?player_name=Jalen+Brunson&season_filter=2025-26&line_date=2026-10-21"
)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _response(values, *, line_type="PTS", lines=None, season_averages=None):
    """A ``get_filtered_logs`` response: rows newest first, as the route returns them."""
    return {
        "game_logs": [{line_type: value} for value in values],
        "averages": [],
        "season_averages": [season_averages if season_averages is not None else {line_type: 22.04}],
        "season_game_count": len(values),
        "posted_line": {"lines": lines or {}},
        "provenance": {},
    }


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_every_contract_case(case):
    response = _response(
        case["values"],
        line_type=case["line_type"],
        lines=case["lines"],
        season_averages=case["season_averages"],
    )

    facts = preview_facts(response, LineType(case["line_type"]))

    assert {
        "line": None if facts.line is None else str(facts.line),
        "source": None if facts.source is None else facts.source.value,
        "provider": facts.provider,
        "hits": facts.hits,
        "games": len(facts.values),
    } == case["expect"]


def test_bars_are_the_newest_ten_oldest_first():
    facts = preview_facts(_response([11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]), LineType.PTS)

    assert facts.values == (2, 3, 4, 5, 6, 7, 8, 9, 10, 11)


def test_a_zero_line_makes_no_hit_claim():
    facts = preview_facts(_response([1, 0], line_type="BLK", season_averages={"BLK": 0.0}), LineType.BLK)

    assert (str(facts.line), facts.hits) == ("0.0", None)


# -- the route -----------------------------------------------------------------


class FakeRedis:
    """The few Redis commands the preview uses, held in memory."""

    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.values[key] = value.encode() if isinstance(value, str) else value

    def pipeline(self):
        return _Pipeline(self)


class _Pipeline:
    def __init__(self, store):
        self.store = store
        self.results = []

    def incr(self, key):
        self.store.values[key] = int(self.store.values.get(key, 0)) + 1
        self.results.append(self.store.values[key])

    def expire(self, key, ttl):
        self.results.append(True)

    def execute(self):
        return self.results


class DownRedis:
    def __getattr__(self, name):
        raise redis.exceptions.ConnectionError("down")


@pytest.fixture
def firebase_enforced(monkeypatch):
    """Firebase is configured, so a route with auth refuses a missing token."""
    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(auth, "verify_firebase_token", lambda token: {"uid": token})


@pytest.fixture
def store(dependencies):
    dependencies.redis_client = FakeRedis()
    return dependencies.redis_client


@pytest.fixture
def logs(dependencies, store):
    read = dependencies.game_service.get_filtered_logs
    read.return_value = _response([30, 25, 24.5, 20, 26, 24, 28, 19, 31, 22, 40])
    return read


def _image(response):
    return Image.open(io.BytesIO(response.data))


def _is_card(response, cache_control):
    return (
        response.status_code,
        response.mimetype,
        response.headers["Cache-Control"],
        response.data[:8],
        _image(response).size,
    ) == (200, "image/png", cache_control, PNG_MAGIC, (1200, 630))


def test_a_signed_out_caller_gets_the_data_card(client, firebase_enforced, logs):
    response = client.get(PREVIEW)

    assert _is_card(response, "public, max-age=3600")
    assert response.data != draw_fallback()


def test_the_card_is_the_same_with_or_without_a_token(client, firebase_enforced, logs):
    signed_in = client.get(PREVIEW, headers={"Authorization": "Bearer x"})
    signed_out = client.get(PREVIEW)

    assert _is_card(signed_in, "public, max-age=3600")
    assert signed_in.data == signed_out.data


def test_the_oldest_game_is_the_leftmost_bar(client, logs):
    # Rows arrive newest first: the oldest game, 30 points, is the only one over 22.0.
    logs.return_value = _response([1] * 9 + [30])

    pixels = _image(client.get(PREVIEW)).convert("RGB")
    # Just above the baseline, every bar is drawn: green over the line, red not.
    row = [pixels.getpixel((x, 586)) for x in range(1200)]
    green = [x for x, color in enumerate(row) if color == (76, 175, 125)]
    red = [x for x, color in enumerate(row) if color == (194, 78, 78)]

    assert green and red
    assert green[-1] < red[0]


def test_a_refused_token_is_ignored(client, monkeypatch, firebase_enforced, logs):
    def refuse(token):
        raise auth.InvalidTokenError("waitlisted")

    monkeypatch.setattr(auth, "verify_firebase_token", refuse)

    response = client.get(PREVIEW, headers={"Authorization": "Bearer waitlisted"})

    assert _is_card(response, "public, max-age=3600")


def test_the_page_query_reaches_the_read_without_line_type(client, logs):
    client.get(PREVIEW + "&line_type=REB&game_filter=5")

    player_name, query = logs.call_args.args
    assert (player_name, query.season_filter, query.game_filter, str(query.line_date)) == (
        "Jalen Brunson",
        "2025-26",
        5,
        "2026-10-21",
    )


def test_no_matching_games_is_a_card(client, logs):
    logs.return_value = _response([])

    response = client.get(PREVIEW)

    assert _is_card(response, "public, max-age=3600")
    assert response.data != draw_fallback()


def test_a_cache_hit_reads_no_game_logs(client, logs):
    first = client.get(PREVIEW + "&line_type=REB&game_filter=5")
    # Parameter order and parameters the parser ignores do not split the cache.
    second = client.get(
        "/api/games/game_logs/preview.png?game_filter=5&utm_source=x&line_date=2026-10-21"
        "&line_type=REB&season_filter=2025-26&player_name=Jalen+Brunson"
    )

    assert logs.call_count == 1
    assert _is_card(second, "public, max-age=3600")
    assert second.data == first.data


def test_another_line_type_or_line_date_is_its_own_entry(client, logs):
    client.get(PREVIEW)
    client.get(PREVIEW + "&line_type=REB")
    client.get(PREVIEW.replace("2026-10-21", "2026-10-22"))

    assert logs.call_count == 3


def test_a_link_without_line_date_reads_again_after_et_midnight(app, client, store, logs):
    now = [1_792_639_800.0]  # 2026-10-21 23:30 ET
    app.extensions["game_log_previews"] = GameLogPreviewService(
        parse=game_routes._game_log_query_from,
        logs=game_routes._filtered_logs,
        redis_client=store,
        clock=lambda: now[0],
    )
    link = "/api/games/game_logs/preview.png?player_name=Jalen+Brunson&season_filter=2025-26"

    client.get(link)
    now[0] += 3600  # 2026-10-22 00:30 ET, within the facts' hour
    client.get(link)

    assert logs.call_count == 2


def test_past_the_global_render_budget_the_answer_is_the_fallback(app, client, store, logs):
    # One fixed minute, so the test never straddles two budget windows.
    app.extensions["game_log_previews"] = GameLogPreviewService(
        parse=game_routes._game_log_query_from,
        logs=game_routes._filtered_logs,
        redis_client=store,
        clock=lambda: 1_800_000_000.0,
    )

    def render(game_filter):
        # Each caller its own IP, so only the global budget can refuse.
        return client.get(
            f"{PREVIEW}&game_filter={game_filter}", headers={"X-Forwarded-For": f"198.51.100.{game_filter}"}
        )

    statuses = [render(n).headers["Cache-Control"] for n in range(1, 121)]
    over = render(121)
    hit = render(1)

    assert statuses == ["public, max-age=3600"] * 120
    assert _is_card(over, "public, max-age=60")
    assert over.data == draw_fallback()
    # A cached card is no render and is served past the budget.
    assert _is_card(hit, "public, max-age=3600")
    assert logs.call_count == 120


@pytest.mark.parametrize(
    "failure",
    [
        requests.exceptions.Timeout("slow"),
        requests.exceptions.ConnectionError("refused"),
        OperationalError("SELECT 1", {}, Exception("database is down")),
    ],
)
def test_a_provider_or_database_failure_is_the_uncached_fallback(client, logs, failure):
    card = logs.return_value
    logs.side_effect = failure

    failed = client.get(PREVIEW)
    logs.side_effect = None
    logs.return_value = card
    recovered = client.get(PREVIEW)

    assert _is_card(failed, "public, max-age=60")
    assert failed.data == draw_fallback()
    assert _is_card(recovered, "public, max-age=3600")
    assert recovered.data != draw_fallback()
    assert logs.call_count == 2


class Cancelled(Exception):
    """A DBAPI error whose SQLSTATE is Postgres ``query_canceled``."""

    pgcode = "57014"


def _real_game_service(mock_db_engine, *, publication_reader, snapshot_player_game_logs):
    """The game service the app runs, over a publication that fails as given."""
    repository = PlayerGameLogRepository(
        mock_db_engine,
        statistic_catalog=SimpleNamespace(statistics=()),
        stats_surface_max_age=timedelta(hours=1),
        stats_surface_season="2025-26",
        publication_reader=SimpleNamespace(snapshot_player_game_logs=snapshot_player_game_logs),
    )
    service = GameService(
        mock_db_engine,
        game_logs_source=StoredGameLogsSource(repository),
        publication_reader=publication_reader,
    )
    service.get_player_id = lambda *args: 1
    return service


def _timed_out(**kwargs):
    raise OperationalError("SELECT pointer", {}, Cancelled("statement timeout"))


def test_a_query_timeout_is_the_fallback(client, dependencies, mock_db_engine):
    dependencies.game_service = _real_game_service(
        mock_db_engine, publication_reader=None, snapshot_player_game_logs=_timed_out
    )

    response = client.get(PREVIEW)

    assert _is_card(response, "public, max-age=60")
    assert response.data == draw_fallback()


@pytest.mark.parametrize(
    "failure",
    [
        OperationalError("SELECT publication", {}, Exception("database is down")),
        OperationalError("SELECT publication", {}, Cancelled("statement timeout")),
        SeasonUnavailableError(season="2026-27", published_season="2025-26", stream="player_game_logs"),
    ],
)
def test_failing_to_resolve_the_default_season_is_the_fallback(client, dependencies, mock_db_engine, failure):
    def snapshot(*args, **kwargs):
        raise failure

    dependencies.game_service = _real_game_service(
        mock_db_engine,
        publication_reader=SimpleNamespace(snapshot=snapshot),
        snapshot_player_game_logs=_timed_out,
    )

    response = client.get("/api/games/game_logs/preview.png?player_name=Jalen+Brunson")

    assert _is_card(response, "public, max-age=60")
    assert response.data == draw_fallback()


def test_with_redis_down_the_card_is_still_drawn(client, dependencies):
    dependencies.redis_client = DownRedis()
    dependencies.game_service.get_filtered_logs.return_value = _response([30, 20])

    response = client.get(PREVIEW)

    assert _is_card(response, "public, max-age=3600")


@pytest.mark.parametrize("unavailable", [None, DownRedis()], ids=["not configured", "down"])
def test_without_redis_each_process_renders_30_a_minute(app, client, dependencies, unavailable):
    dependencies.game_service.get_filtered_logs.return_value = _response([30, 20])
    app.extensions["game_log_previews"] = GameLogPreviewService(
        parse=game_routes._game_log_query_from,
        logs=game_routes._filtered_logs,
        redis_client=unavailable,
        clock=lambda: 1_800_000_000.0,
    )

    def render(game_filter):
        return client.get(
            f"{PREVIEW}&game_filter={game_filter}", headers={"X-Forwarded-For": f"198.51.100.{game_filter}"}
        )

    statuses = [render(n).headers["Cache-Control"] for n in range(1, 31)]
    over = render(31)

    assert statuses == ["public, max-age=3600"] * 30
    assert _is_card(over, "public, max-age=60")
    assert over.data == draw_fallback()


@pytest.mark.parametrize(
    ("query", "parameter"),
    [
        ("&line_type=None", "line_type"),
        ("&line_type=pts", "line_type"),
        ("&minutes_filter=abc", "minutes_filter"),
    ],
)
def test_a_bad_filter_or_line_type_is_400(client, logs, query, parameter):
    response = client.get(PREVIEW + query)

    assert (response.status_code, response.headers["Cache-Control"]) == (400, "no-store")
    body = response.get_json()["error"]
    assert (body["code"], body["details"]["filters"][0]["parameter"]) == ("invalid_input", parameter)
    assert logs.call_count == 0


def test_a_missing_player_name_is_400(client, logs):
    response = client.get("/api/games/game_logs/preview.png")

    assert (response.status_code, response.get_json()["error"]["code"]) == (400, "invalid_input")


def test_an_unknown_player_is_404(client, logs):
    logs.side_effect = ValueError("No matching player found for Nobody.")

    response = client.get(PREVIEW)

    assert (
        response.status_code,
        response.headers["Cache-Control"],
        response.get_json()["error"]["code"],
    ) == (
        404,
        "no-store",
        "resource_not_found",
    )


def test_the_61st_request_from_one_ip_in_a_minute_is_429(client, logs):
    proxied = {"X-Forwarded-For": "203.0.113.9, 10.0.0.2"}

    statuses = [client.get(PREVIEW, headers=proxied).status_code for _ in range(60)]
    limited = client.get(PREVIEW, headers=proxied)

    assert statuses == [200] * 60
    assert (limited.status_code, limited.headers["Cache-Control"], limited.get_json()["error"]["code"]) == (
        429,
        "no-store",
        "rate_limited",
    )
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
