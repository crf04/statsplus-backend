"""The link preview image of a game-log link (crf04/statsplus#285).

Chat apps unfurl a shared game-log link by fetching its ``og:image`` signed
out, so this draws the link's results as a 1200x630 PNG: how many of the
games the link's filters match went over the line, the sample's per-game,
per-36 and minutes averages against the whole season, and the newest games as
bars.  The contract is
crf04/statsplus ``docs/contracts/game-log-preview.md``; its line and hit rules
match the Game Logs page's default line and chart counts, and the literal
cases in ``tests/fixtures/game_log_preview_cases.json`` hold both sides to
them.

Redis keeps the card's facts, not the PNG, for an hour.  Uncached renders
share one global budget per minute, or a smaller per-process one while Redis
is unavailable; past it, and on a provider, database or query-timeout
failure, the answer is a brand image that is never cached in Redis.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Any, Literal

import redis
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy.exc import SQLAlchemyError

from app.domain.slate_time import slate_date_for_instant
from app.errors import (
    InvalidInputError,
    ProviderUnavailableError,
    QueryTimeoutError,
    SeasonUnavailableError,
    sanitize_public_value,
)
from app.models.game_logs import GameLogQuery
from app.utils.redis_breaker import RedisCircuitBreaker

logger = logging.getLogger(__name__)

#: Bumped whenever the facts or the rules that derive them change.
PREVIEW_SCHEMA = 2
FACTS_TTL_SECONDS = 3600
#: Uncached renders per minute, across every caller and worker.
RENDER_BUDGET_PER_MINUTE = 120
#: Uncached renders per minute in one process while Redis is unavailable.
LOCAL_RENDER_BUDGET_PER_MINUTE = 30
CARD_CACHE_CONTROL = "public, max-age=3600"
FALLBACK_CACHE_CONTROL = "public, max-age=60"
MAX_BARS = 15
WIDTH, HEIGHT = 1200, 630


class LineType(StrEnum):
    """The frontend Line Type list, ``None`` excepted: each is a game-log column."""

    PTS = "PTS"
    FD_PTS = "FD_PTS"
    PRA = "PRA"
    PA = "PA"
    RA = "RA"
    PR = "PR"
    AST = "AST"
    REB = "REB"
    FTM = "FTM"
    FTA = "FTA"
    STL = "STL"
    BLK = "BLK"
    STKS = "STKS"
    FG3M = "FG3M"
    FG3A = "FG3A"
    FG2M = "FG2M"
    FG2A = "FG2A"
    FGM = "FGM"
    FGA = "FGA"
    TOV = "TOV"


class LineSource(StrEnum):
    POSTED = "posted"
    SEASON_AVERAGE = "season_average"


#: Line Types are posted-line market keys except FG3M, whose market is 3PM.
_MARKET_FOR_LINE_TYPE = {LineType.FG3M: "3PM"}


def parse_line_type(value: str | None) -> LineType:
    """The ``line_type`` parameter; omitted is PTS, anything unknown is refused."""

    if value is None:
        return LineType.PTS
    try:
        return LineType(value)
    except ValueError as error:
        raise InvalidInputError(
            "line_type is not a supported Line Type.",
            detail=error,
            public_details={
                "filters": [
                    {
                        "parameter": "line_type",
                        "values": [sanitize_public_value(value)],
                        "supported_values": [line_type.value for line_type in LineType],
                    }
                ]
            },
        ) from error


@dataclass(frozen=True)
class Tile:
    """One average over the sample, to one decimal, and its change against the season."""

    caption: Literal["per game", "per 36", "min"]
    value: Decimal
    change_percent: int | None


@dataclass(frozen=True)
class PreviewFacts:
    """Everything the card shows.

    ``games`` and ``hits`` cover every matching game; ``values`` are the
    newest ``MAX_BARS`` of them, oldest first.
    """

    player_name: str
    line_type: LineType
    line: Decimal | None
    source: LineSource | None
    provider: str | None
    filters: str
    games: int
    hits: int | None
    tiles: tuple[Tile, ...]
    values: tuple[float, ...]

    def to_json(self) -> str:
        facts = asdict(self)
        facts["line"] = None if self.line is None else str(self.line)
        for tile in facts["tiles"]:
            tile["value"] = str(tile["value"])
        return json.dumps(facts, separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: str | bytes) -> PreviewFacts:
        facts = json.loads(payload)
        return cls(
            player_name=facts["player_name"],
            line_type=LineType(facts["line_type"]),
            line=None if facts["line"] is None else Decimal(facts["line"]),
            source=None if facts["source"] is None else LineSource(facts["source"]),
            provider=facts["provider"],
            filters=facts["filters"],
            games=facts["games"],
            hits=facts["hits"],
            tiles=tuple(
                Tile(tile["caption"], Decimal(tile["value"]), tile["change_percent"])
                for tile in facts["tiles"]
            ),
            values=tuple(float(value) for value in facts["values"]),
        )


@dataclass(frozen=True)
class PreviewImage:
    png: bytes
    cache_control: str


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _one_decimal(value: float) -> Decimal:
    """JavaScript ``toFixed(1)``: half up on the exact binary value."""

    return Decimal(value).quantize(Decimal("0.1"), ROUND_HALF_UP)


def _default_line(
    response: Mapping[str, Any], line_type: LineType
) -> tuple[Decimal | None, LineSource | None, str | None]:
    """The posted default for the stat's market, else the season average."""

    lines = (response.get("posted_line") or {}).get("lines") or {}
    posted = lines.get(_MARKET_FOR_LINE_TYPE.get(line_type, line_type.value)) or {}
    value = _finite(posted.get("value"))
    provider = posted.get("provider")
    if value is not None and isinstance(provider, str) and provider:
        return _one_decimal(value), LineSource.POSTED, provider
    season = response.get("season_averages") or [{}]
    average = _finite(season[0].get(line_type.value))
    if average is not None:
        return _one_decimal(average), LineSource.SEASON_AVERAGE, None
    return None, None, None


def _whole_percent(numerator: Decimal, denominator: Decimal) -> int:
    return int((numerator / denominator * 100).quantize(Decimal(1), ROUND_HALF_UP))


def _tiles(response: Mapping[str, Any], line_type: LineType) -> tuple[Tile, ...]:
    """Per game, per 36 and minutes over the sample, each against the whole season.

    The change is shown only when the filters narrow the sample below the
    season's games, and never against a season value of 0 or none.
    """

    rows = response.get("game_logs") or []
    if not rows:
        return ()
    games = Decimal(len(rows))
    stat_total = sum(Decimal(_finite(row.get(line_type.value)) or 0) for row in rows)
    minutes_total = sum(Decimal(_finite(row.get("MIN")) or 0) for row in rows)
    season = (response.get("season_averages") or [{}])[0]
    season_stat = _finite(season.get(line_type.value))
    season_minutes = _finite(season.get("MIN"))
    season_per_36 = None
    if season_stat is not None and season_minutes:
        season_per_36 = Decimal(season_stat) / Decimal(season_minutes) * 36
    season_games = response.get("season_game_count")
    narrowed = isinstance(season_games, int) and len(rows) < season_games

    def tile(caption: Literal["per game", "per 36", "min"], value: Decimal, season_value: Any) -> Tile:
        change = None
        if narrowed and season_value:
            season_value = Decimal(season_value)
            change = _whole_percent(value - season_value, season_value)
        return Tile(caption, value.quantize(Decimal("0.1"), ROUND_HALF_UP), change)

    tiles = [tile("per game", stat_total / games, season_stat)]
    if minutes_total:
        tiles.append(tile("per 36", stat_total / minutes_total * 36, season_per_36))
    tiles.append(tile("min", minutes_total / games, season_minutes))
    return tuple(tiles)


def preview_facts(
    response: Mapping[str, Any], line_type: LineType, player_name: str = "", filters: str = ""
) -> PreviewFacts:
    """The card's facts from one ``get_filtered_logs`` response (rows newest first)."""

    rows = response.get("game_logs") or []
    every = [_finite(row.get(line_type.value)) or 0.0 for row in rows]
    line, source, provider = _default_line(response, line_type)
    hits = None
    if line is not None and line > 0:
        hits = sum(value > float(line) for value in every)
    return PreviewFacts(
        player_name=player_name,
        line_type=line_type,
        line=line,
        source=source,
        provider=provider,
        filters=filters,
        games=len(every),
        hits=hits,
        tiles=_tiles(response, line_type),
        values=tuple(reversed(every[:MAX_BARS])),
    )


# -- filter wording --------------------------------------------------------------

#: The defensive-filter labels of the frontend's ``OPPONENT_FILTERS``, mirrored
#: from crf04/statsplus ``docs/contracts/opponent-filter-labels.json``.
_OPPONENT_FILTER_LABELS: dict[str, str] = json.loads(
    (Path(__file__).resolve().parent.parent / "assets" / "opponent_filter_labels.json").read_text()
)


def _list_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _describe_defensive_filter(category: str, rank: int | tuple[int, int]) -> str:
    label = _OPPONENT_FILTER_LABELS.get(category, category)
    if isinstance(rank, tuple):
        return f"vs ranks {rank[0]}-{rank[1]} {label} D"
    return f"vs {'top' if rank > 0 else 'bottom'} {abs(rank)} {label} D"


def describe_filters(args: Any, query: GameLogQuery) -> str:
    """The link's filters in the words of the frontend's ``linkPreviewFor``.

    ``args`` says which filters the link names, since parsing fills in a
    default season and minutes range; ``query`` gives their parsed values.
    """

    parts = []
    if args.get("season_filter") is not None:
        parts.append(f"{query.season_filter} season")
    if query.game_filter is not None:
        parts.append(f"last {query.game_filter} games")
    if query.location_filter != "Both":
        parts.append(f"{query.location_filter.lower()} games")
    if query.opponent_tricode:
        parts.append(f"vs {query.opponent_tricode}")
    for category, rank in zip(query.teams_against, query.rank_filter, strict=True):
        parts.append(_describe_defensive_filter(category, rank))
    if args.get("minutes_filter") is not None:
        low, high = query.minutes_filter
        parts.append(f"{low}-{high} minutes")
    if query.date_filter:
        parts.append(f"since {query.date_filter.isoformat()}")
    if query.date_to:
        parts.append(f"through {query.date_to.isoformat()}")
    if query.players_on:
        parts.append(f"with {_list_names([name.strip() for name in query.players_on])}")
    if query.players_off:
        parts.append(f"without {_list_names([name.strip() for name in query.players_off])}")
    summary = ", ".join(parts) or "every logged game"
    return summary[0].upper() + summary[1:]


# -- drawing -------------------------------------------------------------------

_FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"
#: CourtAI tokens (frontend ``src/theme.css``).
_INK = (13, 11, 7)
_SURFACE = (22, 19, 13)
_TEXT = (239, 233, 220)
_DIM = (155, 147, 127)
_GOLD = (232, 163, 61)
_HIT = (76, 175, 125)
_MISS = (194, 78, 78)
_NEUTRAL = (122, 134, 153)
_LOGO = (249, 115, 22)
_MARGIN = 64


@cache
def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONTS / f"BarlowCondensed-{weight}.ttf"), size)


def _canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), _INK)
    draw = ImageDraw.Draw(image)
    # The app's warm glow at the top of the page.
    for step in range(48):
        mix = step / 48
        color = tuple(round(a + (b - a) * mix) for a, b in zip((26, 21, 12), _INK, strict=True))
        draw.rectangle((0, step * 6, WIDTH, step * 6 + 6), fill=color)
    return image, draw


def _logo(draw: ImageDraw.ImageDraw, left: int, top: int, size: int) -> None:
    """The CourtAI mark (``public/logo.svg``) scaled into a ``size`` square."""

    draw.rounded_rectangle((left, top, left + size, top + size), radius=size // 6, fill=_LOGO)

    def point(x: float, y: float) -> tuple[float, float]:
        # logo.svg: translate(256 256) scale(0.86) translate(-250 -298) in 512 units.
        return left + ((x - 250) * 0.86 + 256) * size / 512, top + ((y - 298) * 0.86 + 256) * size / 512

    stroke = max(2, round(38 * 0.86 * size / 512))
    for start, end in (
        ((225, 185), (225, 352)),
        ((122, 234), (225, 243)),
        ((225, 252), (360, 290)),
        ((225, 345), (138, 462)),
        ((225, 345), (296, 462)),
    ):
        draw.line((point(*start), point(*end)), fill="white", width=stroke)
    for (x, y), r in (((225, 149), 44), ((388, 296), 51)):
        cx, cy = point(x, y)
        radius = r * 0.86 * size / 512
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill="white")


def _header(draw: ImageDraw.ImageDraw) -> None:
    _logo(draw, _MARGIN, 44, 52)
    draw.text((_MARGIN + 68, 70), "CourtAI", font=_font("SemiBold", 40), fill=_TEXT, anchor="lm")
    draw.text((WIDTH - _MARGIN, 70), "courtai.app", font=_font("SemiBold", 30), fill=_DIM, anchor="rm")


def _fitted(
    draw: ImageDraw.ImageDraw, text: str, weight: str, size: int, width: float, smallest: int
) -> tuple[str, ImageFont.FreeTypeFont]:
    """``text`` in the largest font down to ``smallest`` that fits ``width``, else cut short with an ellipsis."""

    while size > smallest and draw.textlength(text, font=_font(weight, size)) > width:
        size -= 2
    font = _font(weight, size)
    if draw.textlength(text, font=font) <= width:
        return text, font
    while text and draw.textlength(text.rstrip() + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…", font


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _has_line(facts: PreviewFacts) -> bool:
    return facts.line is not None and facts.line > 0


def _change(percent: int) -> tuple[str, tuple[int, int, int]]:
    if percent > 0:
        return f"+{percent}%", _HIT
    if percent < 0:
        return f"{percent}%", _MISS
    return "0%", _DIM


def _tiles_row(draw: ImageDraw.ImageDraw, tiles: tuple[Tile, ...], top: int) -> None:
    gap = 16
    width = (WIDTH - 2 * _MARGIN - gap * (len(tiles) - 1)) / len(tiles)
    value_font, change_font = _font("Bold", 58), _font("Bold", 30)
    for index, tile in enumerate(tiles):
        left = _MARGIN + index * (width + gap)
        draw.rounded_rectangle((left, top, left + width, top + 112), radius=14, fill=_SURFACE)
        value = str(tile.value)
        value_width = draw.textlength(value, font=value_font)
        change = None if tile.change_percent is None else _change(tile.change_percent)
        change_width = 0 if change is None else draw.textlength(change[0], font=change_font) + 14
        x = left + (width - value_width - change_width) / 2
        draw.text((x, top + 46), value, font=value_font, fill=_TEXT, anchor="lm")
        if change is not None:
            draw.text(
                (x + value_width + 14, top + 50), change[0], font=change_font, fill=change[1], anchor="lm"
            )
        draw.text(
            (left + width / 2, top + 92), tile.caption, font=_font("SemiBold", 24), fill=_DIM, anchor="mm"
        )


def _bars(draw: ImageDraw.ImageDraw, facts: PreviewFacts, left: int, top: int, bottom: int) -> None:
    right = WIDTH - _MARGIN
    line = float(facts.line) if _has_line(facts) else None
    ceiling = max([*facts.values, line or 0.0, 1.0]) * 1.1
    slot = (right - left) / MAX_BARS
    bar_width = slot * 0.64
    draw.line((left, bottom, right, bottom), fill=(52, 46, 34), width=2)
    if line is not None:
        y = bottom - (bottom - top) * line / ceiling
        for x in range(left, right, 28):
            draw.line((x, y, min(x + 14, right), y), fill=_GOLD, width=3)
    for index, value in enumerate(facts.values):
        x = left + index * slot + (slot - bar_width) / 2
        height = max(6, (bottom - top) * value / ceiling)
        color = _NEUTRAL if line is None else _HIT if value > line else _MISS
        draw.rounded_rectangle((x, bottom - height, x + bar_width, bottom), radius=6, fill=color)


def draw(facts: PreviewFacts) -> bytes:
    """The data card, or the "No games match these filters" card."""

    image, canvas = _canvas()
    _header(canvas)
    text_width = WIDTH - 2 * _MARGIN
    headline_room = 0.0
    if facts.games:
        has_line = facts.hits is not None
        headline = f"{facts.hits} of {facts.games}" if has_line else f"{facts.games} games"
        headline_font = _font("Bold", 92)
        canvas.text((WIDTH - _MARGIN, 112), headline, font=headline_font, fill=_TEXT, anchor="ra")
        headline_room = canvas.textlength(headline, font=headline_font) + 40
        if has_line:
            percent = _whole_percent(Decimal(facts.hits), Decimal(facts.games))
            canvas.text(
                (WIDTH - _MARGIN, 206),
                f"over · {percent}%",
                font=_font("SemiBold", 32),
                fill=_DIM,
                anchor="ra",
            )
    name, name_font = _fitted(canvas, facts.player_name, "Bold", 72, text_width - headline_room, 40)
    # A shrunken name keeps the full-size name's baseline.
    name_baseline = 112 + _font("Bold", 72).getmetrics()[0]
    canvas.text((_MARGIN, name_baseline), name, font=name_font, fill=_TEXT, anchor="ls")
    stat_line = f"{facts.line_type.value} - {facts.line}" if _has_line(facts) else facts.line_type.value
    canvas.text((_MARGIN, 194), stat_line, font=_font("SemiBold", 36), fill=_GOLD)
    filters, filters_font = _fitted(canvas, facts.filters, "SemiBold", 30, text_width, 24)
    canvas.text((_MARGIN, 240), filters, font=filters_font, fill=_TEXT)

    if not facts.games:
        canvas.rounded_rectangle((_MARGIN, 300, WIDTH - _MARGIN, 566), radius=16, fill=_SURFACE)
        canvas.text(
            (WIDTH / 2, 433), "No games match these filters", font=_font("Bold", 64), fill=_DIM, anchor="mm"
        )
        return _png(image)

    _tiles_row(canvas, facts.tiles, top=292)
    canvas.text((_MARGIN, 428), f"LAST {MAX_BARS}", font=_font("SemiBold", 22), fill=_DIM)
    _bars(canvas, facts, left=_MARGIN + 90, top=424, bottom=604)
    return _png(image)


@cache
def draw_fallback() -> bytes:
    """The brand image served when no card can be drawn right now."""

    image, canvas = _canvas()
    _logo(canvas, WIDTH // 2 - 90, 150, 180)
    canvas.text((WIDTH / 2, 420), "CourtAI", font=_font("Bold", 110), fill=_TEXT, anchor="mm")
    canvas.text(
        (WIDTH / 2, 505), "NBA game logs and matchups", font=_font("SemiBold", 44), fill=_DIM, anchor="mm"
    )
    return _png(image)


# -- facts cache, render budget and the one entry point --------------------------

#: A failed read the preview answers with the fallback rather than an error.
_OUTAGES = (ProviderUnavailableError, QueryTimeoutError, SeasonUnavailableError, SQLAlchemyError)
_REDIS_DOWN = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


class GameLogPreviewService:
    """Draw one game-log link's preview, reading its game logs through ``logs``.

    ``parse`` and ``logs`` are the game-log route's own parser
    (``_game_log_query_from``) and read (``_filtered_logs``), so filters, the
    default season and the 400/404 refusals are the page's.  Without Redis
    nothing is cached and each process keeps its own, smaller render budget.
    """

    def __init__(
        self,
        *,
        parse: Callable[[Any], tuple[str, GameLogQuery]],
        logs: Callable[[str, GameLogQuery], Mapping[str, Any]],
        redis_client: Any | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._parse = parse
        self._logs = logs
        self._redis = redis_client
        self._clock = clock
        self._breaker = RedisCircuitBreaker(
            message="Game-log preview cache unavailable (%s); bypassing for %ss"
        )
        self._local_lock = threading.Lock()
        self._local_minute: int | None = None
        self._local_renders = 0

    def render(self, args: Any, line_type: LineType) -> PreviewImage:
        """The preview of the game-log link whose query parameters are ``args``."""

        try:
            return self._render(args, line_type)
        except _OUTAGES as error:
            logger.warning("Game-log preview read failed; serving the fallback: %s", error)
            return PreviewImage(draw_fallback(), FALLBACK_CACHE_CONTROL)

    def _render(self, args: Any, line_type: LineType) -> PreviewImage:
        # Parsing can read the database: an omitted season resolves the default.
        player_name, query = self._parse(args)
        filters = describe_filters(args, query)
        line_date = query.line_date or slate_date_for_instant(datetime.fromtimestamp(self._clock(), UTC))
        key = self._facts_key(player_name, query, filters, line_type, line_date)
        facts = self._cached_facts(key)
        if facts is None:
            if not self._take_render_budget():
                return PreviewImage(draw_fallback(), FALLBACK_CACHE_CONTROL)
            facts = preview_facts(self._logs(player_name, query), line_type, player_name, filters)
            self._store_facts(key, facts)
        return PreviewImage(draw(facts), CARD_CACHE_CONTROL)

    @staticmethod
    def _facts_key(
        player_name: str, query: GameLogQuery, filters: str, line_type: LineType, line_date: date
    ) -> str:
        # The wording joins the key: an omitted season and the default season
        # it resolves to parse alike but read differently.
        body = {
            "schema": PREVIEW_SCHEMA,
            "player_name": player_name,
            "query": query.model_dump(mode="json"),
            "filters": filters,
            "line_type": line_type.value,
            "line_date": line_date.isoformat(),
        }
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
        return f"game_log_preview:v{PREVIEW_SCHEMA}:{digest}"

    def _redis_call(self, call: Callable[[], Any]) -> Any:
        """``call``'s result, or ``None`` when Redis is absent or failing."""

        if self._redis is None or self._breaker.is_open():
            return None
        try:
            return call()
        except _REDIS_DOWN as error:
            self._breaker.record_failure(error)
        except Exception:
            logger.warning("Game-log preview Redis call failed", exc_info=True)
        return None

    def _cached_facts(self, key: str) -> PreviewFacts | None:
        payload = self._redis_call(lambda: self._redis.get(key))
        if not payload:
            return None
        try:
            return PreviewFacts.from_json(payload)
        except (KeyError, TypeError, ValueError):
            return None

    def _store_facts(self, key: str, facts: PreviewFacts) -> None:
        self._redis_call(lambda: self._redis.setex(key, FACTS_TTL_SECONDS, facts.to_json()))

    def _take_render_budget(self) -> bool:
        """Count one uncached render; ``False`` once this minute's budget is spent."""

        minute = int(self._clock() // 60)
        key = f"game_log_preview:renders:{minute}"

        def count() -> int:
            pipeline = self._redis.pipeline()
            pipeline.incr(key)
            pipeline.expire(key, 120)
            return int(pipeline.execute()[0])

        used = self._redis_call(count)
        if used is not None:
            return used <= RENDER_BUDGET_PER_MINUTE
        with self._local_lock:
            # A request whose Redis call stalled across a minute boundary carries
            # the earlier minute; it counts against the current window, never rewinds it.
            if self._local_minute is None or minute > self._local_minute:
                self._local_minute, self._local_renders = minute, 0
            self._local_renders += 1
            return self._local_renders <= LOCAL_RENDER_BUDGET_PER_MINUTE
