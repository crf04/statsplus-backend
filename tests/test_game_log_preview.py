"""The game-log link preview image (crf04/statsplus#285, contract
crf04/statsplus ``docs/contracts/game-log-preview.md``)."""

from __future__ import annotations

import hashlib
import io
import json
from collections import Counter
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
import redis
import requests
from PIL import Image
from sqlalchemy.exc import OperationalError

import app.utils.auth as auth
from app.errors import SeasonUnavailableError
from app.routes import game_routes
from app.services.game_log_preview import (
    GameLogPreviewService,
    LineSource,
    LineType,
    PreviewFacts,
    Tile,
    draw,
    draw_fallback,
    parse_line,
    preview_facts,
)
from app.services.game_logs_source import StoredGameLogsSource
from app.services.game_service import GameService
from app.services.player_game_log_repository import PlayerGameLogRepository

CONTRACT = json.loads((Path(__file__).parent / "fixtures" / "game_log_preview_cases.json").read_text())
CASES = CONTRACT["cases"]
FILTER_CASES = CONTRACT["filter_cases"]
LINE_CASES = CONTRACT["line_cases"]
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

    facts = preview_facts(response, LineType(case["line_type"]), link_line=parse_line(case.get("link_line")))

    assert {
        "line": None if facts.line is None else str(facts.line),
        "source": None if facts.source is None else facts.source.value,
        "provider": facts.provider,
        "hits": facts.hits,
        "games": facts.games,
    } == case["expect"]


def test_the_headline_counts_every_game_and_the_bars_are_the_newest_fifteen_oldest_first():
    # Twenty games, newest first: the five oldest are the only ones over 22.0.
    facts = preview_facts(_response([*range(15, 0, -1), 30, 30, 30, 30, 30]), LineType.PTS)

    assert (facts.games, facts.hits) == (20, 5)
    assert facts.values == tuple(range(1, 16))


def _sample(points, minutes, *, season, season_game_count, lines=None):
    """Rows newest first with PTS and MIN, against ``season`` averages."""
    return {
        "game_logs": [{"PTS": p, "MIN": m} for p, m in zip(points, minutes, strict=True)],
        "season_averages": [season],
        "season_game_count": season_game_count,
        "posted_line": {"lines": lines or {}},
    }


def test_the_tiles_are_sample_averages_changed_against_the_unrounded_season():
    response = _sample(
        [30, 20, 25, 14],
        [36, 30, 34, 31],
        season={"PTS": 20.0, "MIN": 32.0},
        season_game_count=10,
        lines={"PTS": {"value": 24.5, "provider": "prizepicks"}},
    )

    # Per game 89/4 = 22.25 is +11.25% on 20.0, shown 22.3 but compared unrounded.
    # Per 36 is the totals' ratio, 89/131*36 = 24.458, against 20/32*36 = 22.5.
    assert preview_facts(response, LineType.PTS).tiles == (
        Tile("per game", Decimal("22.3"), 11),
        Tile("per 36", Decimal("24.5"), 9),
        Tile("min", Decimal("32.8"), 2),
    )


def test_a_change_rounds_half_up_and_no_change_reads_zero():
    response = _sample(
        [10, 10, 10, 10, 10], [33, 33, 33, 33, 32], season={"PTS": 10.0, "MIN": 32.0}, season_game_count=6
    )

    # Minutes 32.8 on 32.0 is +2.5%; per 36 50/164*36 = 10.976 on 11.25 is -2.4%.
    assert preview_facts(response, LineType.PTS).tiles == (
        Tile("per game", Decimal("10.0"), 0),
        Tile("per 36", Decimal("11.0"), -2),
        Tile("min", Decimal("32.8"), 3),
    )


@pytest.mark.parametrize(
    ("assists", "minutes", "season_minutes", "per_36"),
    [
        # 2/16 against 1/11 is exactly +37.5%; 3/40 against 1/13 exactly -2.5%.
        (2, 16, 11, Tile("per 36", Decimal("4.5"), 38)),
        (3, 40, 13, Tile("per 36", Decimal("2.7"), -3)),
    ],
)
def test_an_exact_half_per_36_change_rounds_away_from_zero(assists, minutes, season_minutes, per_36):
    response = {
        "game_logs": [{"AST": assists, "MIN": minutes}],
        "season_averages": [{"AST": 1, "MIN": season_minutes}],
        "season_game_count": 10,
    }

    assert preview_facts(response, LineType.AST).tiles[1] == per_36


def test_a_whole_season_link_shows_no_change():
    response = _sample([30, 20], [36, 30], season={"PTS": 20.0, "MIN": 32.0}, season_game_count=2)

    assert [tile.change_percent for tile in preview_facts(response, LineType.PTS).tiles] == [None, None, None]


def test_a_season_value_of_zero_shows_no_change_for_that_tile():
    response = _sample([1, 0], [20, 10], season={"PTS": 0.0, "MIN": 20.0}, season_game_count=9)

    assert preview_facts(response, LineType.PTS).tiles == (
        Tile("per game", Decimal("0.5"), None),
        Tile("per 36", Decimal("1.2"), None),
        Tile("min", Decimal("15.0"), -25),
    )


def test_a_sample_without_minutes_has_no_per_36_tile():
    response = _sample([2, 0], [0, 0], season={"PTS": 4.0, "MIN": 10.0}, season_game_count=9)

    assert preview_facts(response, LineType.PTS).tiles == (
        Tile("per game", Decimal("1.0"), -75),
        Tile("min", Decimal("0.0"), -100),
    )


def test_the_facts_survive_the_cache_round_trip():
    response = _sample(
        [30, 20, 25, 14], [36, 30, 34, 31], season={"PTS": 20.0, "MIN": 32.0}, season_game_count=10
    )
    facts = preview_facts(response, LineType.PTS, "Jalen Johnson", "2025-26 season")

    assert PreviewFacts.from_json(facts.to_json()) == facts


def test_the_labels_are_the_contract_file():
    # crf04/statsplus docs/contracts/opponent-filter-labels.json; change both together.
    labels = Path(__file__).parent.parent / "app" / "assets" / "opponent_filter_labels.json"

    assert hashlib.sha1(labels.read_bytes()).hexdigest() == "8d33bf70ce5732c0db5612634ab812e94a914585"


def test_a_zero_line_makes_no_hit_claim():
    facts = preview_facts(_response([1, 0], line_type="BLK", season_averages={"BLK": 0.0}), LineType.BLK)

    assert (str(facts.line), facts.hits) == ("0.0", None)


# -- the drawn card ------------------------------------------------------------

CARD = PreviewFacts(
    player_name="Jalen Brunson",
    line_type=LineType.PTS,
    line=Decimal("22.0"),
    source=LineSource.SEASON_AVERAGE,
    provider=None,
    filters="2025-26 season",
    games=8,
    hits=1,
    tiles=(
        Tile("per game", Decimal("22.3"), 12),
        Tile("per 36", Decimal("24.5"), 0),
        Tile("min", Decimal("32.8"), -12),
    ),
    values=(30.0,) * 8,
)
GREEN, RED, TILE_SURFACE = (76, 175, 125), (194, 78, 78), (22, 19, 13)
TILES_ROW = (64, 292, 1136, 404)
OVER_LINE = (600, 205, 1136, 250)
FILTERS_LINE = (64, 236, 1136, 285)
RIGHT_MARGIN_AT_FILTERS = (1137, 236, 1200, 285)
STAT_LINE = (64, 190, 560, 236)


def _drawn(facts, box=(0, 0, 1200, 630)):
    return Image.open(io.BytesIO(draw(facts))).convert("RGB").crop(box)


def test_each_tile_is_drawn():
    def tile_count(facts):
        row = list(_drawn(facts, (0, 300, 1200, 301)).get_flattened_data())
        return sum(color == TILE_SURFACE and row[x - 1] != TILE_SURFACE for x, color in enumerate(row))

    assert tile_count(CARD) == 3
    assert tile_count(replace(CARD, tiles=CARD.tiles[::2])) == 2


@pytest.mark.parametrize(
    ("change", "green", "red"),
    [(12, True, False), (-12, False, True), (0, False, False)],
    ids=["+", "-", "0"],
)
def test_a_rise_is_green_a_fall_red_and_no_change_neither(change, green, red):
    colors = Counter(
        _drawn(
            replace(CARD, tiles=(Tile("per game", Decimal("22.3"), change),)), TILES_ROW
        ).get_flattened_data()
    )

    assert (colors[GREEN] > 0, colors[RED] > 0) == (green, red)


def test_a_rise_carries_a_plus_sign():
    def ink(change):
        tile = _drawn(replace(CARD, tiles=(Tile("per game", Decimal("22.3"), change),)), TILES_ROW)
        return sum(color != TILE_SURFACE for color in tile.get_flattened_data())

    # "+12%" outweighs "-12%" by the plus sign's upright stroke.
    assert ink(12) > ink(-12)


def test_the_over_share_rounds_half_up():
    one_of_eight = _drawn(CARD, OVER_LINE)

    # 1 of 8 is 12.5%: drawn as 13 of 100 is, not as 12 of 100.
    assert one_of_eight == _drawn(replace(CARD, hits=13, games=100), OVER_LINE)
    assert one_of_eight != _drawn(replace(CARD, hits=12, games=100), OVER_LINE)


def test_the_filters_are_drawn():
    assert _drawn(CARD, FILTERS_LINE) != _drawn(replace(CARD, filters=""), FILTERS_LINE)


def test_long_filters_stop_at_the_margin():
    long = replace(CARD, filters="With " + ", ".join(["Trae Young"] * 12))

    assert _drawn(long, RIGHT_MARGIN_AT_FILTERS) == _drawn(replace(CARD, filters=""), RIGHT_MARGIN_AT_FILTERS)
    assert _drawn(long, FILTERS_LINE) != _drawn(replace(CARD, filters=""), FILTERS_LINE)


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


def test_the_bars_are_the_newest_fifteen_with_the_oldest_leftmost(client, logs):
    # Rows arrive newest first: of the newest fifteen, only the oldest, 30
    # points, is over 22.0; the five older games beyond them are not drawn.
    logs.return_value = _response([1] * 14 + [30] + [30] * 5)

    pixels = _image(client.get(PREVIEW)).convert("RGB")
    # Just above the baseline every bar is drawn: green over the line, red not.
    row = [pixels.getpixel((x, 600)) for x in range(1200)]
    runs = []
    previous = None
    for color in row:
        kind = {(76, 175, 125): "over", (194, 78, 78): "under"}.get(color)
        if kind is not None and kind != previous:
            runs.append(kind)
        previous = kind

    assert runs == ["over"] + ["under"] * 14


def test_a_refused_token_is_ignored(client, monkeypatch, firebase_enforced, logs):
    def refuse(token):
        raise auth.InvalidTokenError("waitlisted")

    monkeypatch.setattr(auth, "verify_firebase_token", refuse)

    response = client.get(PREVIEW, headers={"Authorization": "Bearer waitlisted"})

    assert _is_card(response, "public, max-age=3600")


@pytest.mark.parametrize("case", FILTER_CASES, ids=[case["filters"] for case in FILTER_CASES])
def test_the_card_names_the_filters_as_the_link_title_does(client, dependencies, store, logs, case):
    dependencies.game_service.default_season.return_value = "2025-26"

    response = client.get(f"/api/games/game_logs/preview.png?{case['query']}&line_date=2026-10-21")

    assert response.headers["Cache-Control"] == "public, max-age=3600"
    (facts,) = [PreviewFacts.from_json(value) for key, value in store.values.items() if "renders" not in key]
    assert facts.filters == case["filters"]


def test_whitespace_in_the_name_or_filters_is_drawn_as_one_space(client, logs):
    link = "/api/games/game_logs/preview.png?player_name={}&season_filter=2025-26&players_on%5B%5D={}&line_date=2026-10-21"

    spaced = client.get(link.format("Jalen%0ABrunson", "Trae%0A%09+Young"))
    plain = client.get(link.format("Jalen+Brunson", "Trae+Young"))

    assert _is_card(spaced, "public, max-age=3600")
    assert spaced.data == plain.data


def test_an_omitted_season_and_the_default_season_are_separate_cards(client, dependencies, store, logs):
    dependencies.game_service.default_season.return_value = "2025-26"

    client.get("/api/games/game_logs/preview.png?player_name=Jalen+Johnson&line_date=2026-10-21")
    client.get(
        "/api/games/game_logs/preview.png?player_name=Jalen+Johnson&season_filter=2025-26&line_date=2026-10-21"
    )

    assert logs.call_count == 2


def test_facts_in_the_first_schema_are_read_again(client, store, logs):
    client.get(PREVIEW)
    v1 = {"player_name": "Jalen Brunson", "line_type": "PTS", "line": "22.0", "source": "season_average"}
    v1 |= {"provider": None, "values": [1.0], "hits": 0}
    for key in [key for key in store.values if "renders" not in key]:
        store.values[key] = json.dumps(v1).encode()

    response = client.get(PREVIEW)

    assert _is_card(response, "public, max-age=3600")
    assert logs.call_count == 2


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


@pytest.mark.parametrize(
    "case", [case for case in LINE_CASES if case["valid"]], ids=lambda case: case["line"]
)
def test_a_valid_link_line_is_drawn_without_trailing_zeros(client, store, logs, case):
    response = client.get(PREVIEW + "&line=" + quote(case["line"]))

    assert _is_card(response, "public, max-age=3600")
    (facts,) = [PreviewFacts.from_json(value) for key, value in store.values.items() if "renders" not in key]
    assert (str(facts.line), facts.source) == (case["shown"], LineSource.LINK)
    expected = replace(CARD, line=Decimal(case["shown"]), source=LineSource.LINK)
    assert _image(response).convert("RGB").crop(STAT_LINE) == _drawn(expected, STAT_LINE)


@pytest.mark.parametrize(
    "case", [case for case in LINE_CASES if not case["valid"]], ids=lambda case: repr(case["line"])
)
def test_an_invalid_link_line_is_400(client, logs, case):
    response = client.get(PREVIEW + "&line=" + quote(case["line"]))

    assert (response.status_code, response.headers["Cache-Control"]) == (400, "no-store")
    body = response.get_json()["error"]
    assert (body["code"], body["details"]["filters"][0]["parameter"]) == ("invalid_input", "line")
    assert logs.call_count == 0


def _stored_line(store):
    (facts,) = [PreviewFacts.from_json(value) for key, value in store.values.items() if "renders" not in key]
    return str(facts.line), facts.source


# Beyond the contract's line_cases: values a float or exponent form would misread.
@pytest.mark.parametrize(
    ("line", "shown"), [("100", "100"), ("24.1", "24.1"), ("0.01", "0.01"), ("999.99", "999.99")]
)
def test_a_link_line_reads_back_exactly_as_decimal_text(client, store, logs, line, shown):
    response = client.get(PREVIEW + "&line=" + line)

    assert _is_card(response, "public, max-age=3600")
    assert _stored_line(store) == (shown, LineSource.LINK)


def _stat_line_ink_right(response):
    """The rightmost column of the stat line's gold text."""
    crop = _image(response).convert("RGB").crop(STAT_LINE)
    pixels = crop.load()
    return max(x for x in range(crop.width) if any(pixels[x, y][0] > 120 for y in range(crop.height)))


def test_the_link_line_value_is_drawn_after_the_stat(client, logs):
    logs.return_value = _response([30, 25, 24.5, 20], season_averages={})
    alone = client.get(PREVIEW)
    short = client.get(PREVIEW + "&line=26")
    long = client.get(PREVIEW + "&line=24.5")

    assert _image(short).crop(STAT_LINE) != _image(long).crop(STAT_LINE)
    # "PTS" alone, then "PTS - 26", then the wider "PTS - 24.5".
    assert _stat_line_ink_right(alone) < _stat_line_ink_right(short) < _stat_line_ink_right(long)
    assert _stat_line_ink_right(long) - _stat_line_ink_right(alone) > 60


def test_a_repeated_link_line_takes_the_first(client, store, logs):
    assert client.get(PREVIEW + "&line=24.5&line=26").status_code == 200
    assert _stored_line(store) == ("24.5", LineSource.LINK)

    assert client.get(PREVIEW + "&line=abc&line=24.5").status_code == 400
    assert logs.call_count == 1


@pytest.mark.parametrize(
    "line", ["\uff12\uff14.\uff15", "\u0662\u0664.\u0665"], ids=["fullwidth", "arabic-indic"]
)
def test_non_ascii_digits_are_400(client, logs, line):
    response = client.get(PREVIEW + "&line=" + quote(line))

    assert (response.status_code, response.get_json()["error"]["details"]["filters"][0]["parameter"]) == (
        400,
        "line",
    )
    assert logs.call_count == 0


def test_an_invalid_link_line_is_400_before_the_default_season_is_read(client, dependencies, logs):
    response = client.get("/api/games/game_logs/preview.png?player_name=Jalen+Brunson&line=abc")

    assert (response.status_code, response.get_json()["error"]["details"]["filters"][0]["parameter"]) == (
        400,
        "line",
    )
    assert dependencies.game_service.default_season.call_count == 0
    assert logs.call_count == 0


def test_link_lines_that_read_the_same_share_an_entry(client, logs):
    client.get(PREVIEW + "&line=24.50")
    client.get(PREVIEW + "&line=24.5")
    assert logs.call_count == 1

    client.get(PREVIEW + "&line=25")
    client.get(PREVIEW)
    assert logs.call_count == 3


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


def test_a_request_from_the_previous_minute_does_not_reopen_a_spent_budget(app, client, dependencies):
    dependencies.game_service.get_filtered_logs.return_value = _response([30, 20])
    now = [1_800_000_060.0]
    app.extensions["game_log_previews"] = GameLogPreviewService(
        parse=game_routes._game_log_query_from,
        logs=game_routes._filtered_logs,
        redis_client=None,
        clock=lambda: now[0],
    )

    def render(game_filter):
        return client.get(
            f"{PREVIEW}&game_filter={game_filter}", headers={"X-Forwarded-For": f"198.51.100.{game_filter}"}
        )

    for n in range(1, 31):
        render(n)
    now[0] = 1_800_000_059.0
    stalled = render(31)
    now[0] = 1_800_000_061.0
    after = render(32)

    assert _is_card(stalled, "public, max-age=60")
    assert _is_card(after, "public, max-age=60")


@pytest.mark.parametrize(
    ("query", "parameter"),
    [
        ("&line_type=None", "line_type"),
        ("&line_type=pts", "line_type"),
        ("&minutes_filter=abc", "minutes_filter"),
        ("&rank_filter%5B%5D=5", "rank_filter"),
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
