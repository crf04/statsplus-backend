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
import json
import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from PIL import ImageDraw

from app.domain.slate_time import slate_date_for_instant
from app.errors import InvalidInputError, sanitize_public_value
from app.models.game_logs import GameLogQuery
from app.services.preview_card import (
    CARD_CACHE_CONTROL,
    FALLBACK_CACHE_CONTROL,
    WIDTH,
    GuardedRedis,
    PreviewImage,
    RenderBudget,
    draw_fallback,
)
from app.services.preview_card import (
    DIM as _DIM,
)
from app.services.preview_card import (
    GOLD as _GOLD,
)
from app.services.preview_card import (
    HIT as _HIT,
)
from app.services.preview_card import (
    MARGIN as _MARGIN,
)
from app.services.preview_card import (
    MISS as _MISS,
)
from app.services.preview_card import (
    NEUTRAL as _NEUTRAL,
)
from app.services.preview_card import (
    OUTAGES as _OUTAGES,
)
from app.services.preview_card import (
    SURFACE as _SURFACE,
)
from app.services.preview_card import (
    TEXT as _TEXT,
)
from app.services.preview_card import (
    canvas as _canvas,
)
from app.services.preview_card import (
    fitted as _fitted,
)
from app.services.preview_card import (
    font as _font,
)
from app.services.preview_card import (
    header as _header,
)
from app.services.preview_card import (
    png as _png,
)

logger = logging.getLogger(__name__)

#: Bumped whenever the facts or the rules that derive them change.
PREVIEW_SCHEMA = 2
FACTS_TTL_SECONDS = 3600
#: Uncached renders per minute, across every caller and worker.
RENDER_BUDGET_PER_MINUTE = 120
#: Uncached renders per minute in one process while Redis is unavailable.
LOCAL_RENDER_BUDGET_PER_MINUTE = 30
MAX_BARS = 15


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
    LINK = "link"
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


#: Up to three digits and two decimals; nothing else, not even surrounding spaces.
_LINE = re.compile(r"[0-9]{1,3}(\.[0-9]{1,2})?")


def parse_line(value: str | None) -> Decimal | None:
    """The ``line`` parameter without trailing zeros (``24.50`` is ``24.5``); omitted is ``None``."""

    if value is None:
        return None
    line = Decimal(value) if _LINE.fullmatch(value) else Decimal(0)
    if line <= 0:
        raise InvalidInputError(
            "line must be a number greater than 0 with at most two decimals.",
            public_details={"filters": [{"parameter": "line", "values": [sanitize_public_value(value)]}]},
        )
    # normalize() alone writes 100 as 1E+2.
    return line.quantize(Decimal(1)) if line == line.to_integral_value() else line.normalize()


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


def _half_up(value: Fraction, places: int = 0) -> Decimal:
    """``value`` to ``places`` decimals, an exact half away from zero as ``ROUND_HALF_UP`` does."""

    magnitude = math.floor(abs(value) * 10**places + Fraction(1, 2))
    return Decimal(-magnitude if value < 0 else magnitude).scaleb(-places)


def _whole_percent(numerator: Fraction | int, denominator: Fraction | int) -> int:
    return int(_half_up(Fraction(numerator) / Fraction(denominator) * 100))


def _tiles(response: Mapping[str, Any], line_type: LineType) -> tuple[Tile, ...]:
    """Per game, per 36 and minutes over the sample, each against the whole season.

    The change is shown only when the filters narrow the sample below the
    season's games, and never against a season value of 0 or none.
    """

    rows = response.get("game_logs") or []
    if not rows:
        return ()
    # Exact fractions, so a change that is exactly a half rounds as one.
    games = len(rows)
    stat_total = sum(Fraction(_finite(row.get(line_type.value)) or 0) for row in rows)
    minutes_total = sum(Fraction(_finite(row.get("MIN")) or 0) for row in rows)
    season = (response.get("season_averages") or [{}])[0]
    season_stat = _finite(season.get(line_type.value))
    season_minutes = _finite(season.get("MIN"))
    season_per_36 = None
    if season_stat is not None and season_minutes:
        season_per_36 = Fraction(season_stat) / Fraction(season_minutes) * 36
    season_games = response.get("season_game_count")
    narrowed = isinstance(season_games, int) and len(rows) < season_games

    def tile(caption: Literal["per game", "per 36", "min"], value: Fraction, season_value: Any) -> Tile:
        change = None
        if narrowed and season_value:
            change = _whole_percent(value - Fraction(season_value), Fraction(season_value))
        return Tile(caption, _half_up(value, 1), change)

    tiles = [tile("per game", stat_total / games, season_stat)]
    if minutes_total:
        tiles.append(tile("per 36", stat_total / minutes_total * 36, season_per_36))
    tiles.append(tile("min", minutes_total / games, season_minutes))
    return tuple(tiles)


def preview_facts(
    response: Mapping[str, Any],
    line_type: LineType,
    player_name: str = "",
    filters: str = "",
    link_line: Decimal | None = None,
) -> PreviewFacts:
    """The card's facts from one ``get_filtered_logs`` response (rows newest first).

    ``link_line``, the sender's line from the link, is the line as given.
    """

    rows = response.get("game_logs") or []
    every = [_finite(row.get(line_type.value)) or 0.0 for row in rows]
    if link_line is not None:
        line, source, provider = link_line, LineSource.LINK, None
    else:
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


#: Rank positions run 1-30; people read the mirrored product scale (frontend ``RANK_TEAM_LIMIT``).
_RANK_TEAM_LIMIT = 30


def _describe_rank_range(low: int, high: int) -> str:
    """The frontend's ``describeRank`` of a ``low,high`` range of positions."""

    if low > _RANK_TEAM_LIMIT:
        return "no ranked team"
    low, high = (_RANK_TEAM_LIMIT + 1 - min(end, _RANK_TEAM_LIMIT) for end in (high, low))
    return f"rank {low}" if low == high else f"ranks {low}–{high}"


def _describe_defensive_filter(token: str, rank: int | tuple[int, int]) -> str:
    label = _OPPONENT_FILTER_LABELS.get(token, token)
    if isinstance(rank, tuple):
        return f"vs {_describe_rank_range(*rank)} {label} D"
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
    # The tokens as the link spells them: parsing renames legacy aliases.
    for token, rank in zip(args.getlist("teams_against[]"), query.rank_filter, strict=True):
        parts.append(_describe_defensive_filter(token, rank))
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
            percent = _whole_percent(facts.hits, facts.games)
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


# -- facts cache, render budget and the one entry point --------------------------


class GameLogPreviewService:
    """Draw one game-log link's preview, reading its game logs through ``logs``.

    ``parse`` and ``logs`` are the game-log route's own parser
    (``_preview_query_from``) and read (``_filtered_logs``), so filters, the
    default season and the 400/404/503 refusals are the page's.  Without Redis
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
        self._redis = GuardedRedis(redis_client, name="Game-log preview cache")
        self._clock = clock
        self._budget = RenderBudget(
            self._redis,
            key_prefix="game_log_preview:renders",
            per_minute=RENDER_BUDGET_PER_MINUTE,
            local_per_minute=LOCAL_RENDER_BUDGET_PER_MINUTE,
            clock=clock,
        )

    def render(self, args: Any, line_type: LineType, link_line: Decimal | None = None) -> PreviewImage:
        """The preview of the game-log link whose query parameters are ``args``."""

        try:
            return self._render(args, line_type, link_line)
        except _OUTAGES as error:
            logger.warning("Game-log preview read failed; serving the fallback: %s", error)
            return PreviewImage(draw_fallback(), FALLBACK_CACHE_CONTROL)

    def _render(self, args: Any, line_type: LineType, link_line: Decimal | None) -> PreviewImage:
        # Parsing can read the database: an omitted season resolves the default.
        player_name, query = self._parse(args)
        if len(query.rank_filter) != len(query.teams_against):
            # The shared parser ignores ranks without filters; the link names a filter it cannot draw.
            raise InvalidInputError(
                "One or more game log filters are invalid.",
                public_details={
                    "filters": [
                        {
                            "parameter": "rank_filter",
                            "values": [sanitize_public_value(rank) for rank in args.getlist("rank_filter[]")],
                        }
                    ]
                },
            )
        filters = describe_filters(args, query)
        line_date = query.line_date or slate_date_for_instant(datetime.fromtimestamp(self._clock(), UTC))
        key = self._facts_key(player_name, query, filters, line_type, link_line, line_date)
        facts = self._cached_facts(key)
        if facts is None:
            if not self._budget.take():
                return PreviewImage(draw_fallback(), FALLBACK_CACHE_CONTROL)
            facts = preview_facts(self._logs(player_name, query), line_type, player_name, filters, link_line)
            self._store_facts(key, facts)
        return PreviewImage(draw(facts), CARD_CACHE_CONTROL)

    @staticmethod
    def _facts_key(
        player_name: str,
        query: GameLogQuery,
        filters: str,
        line_type: LineType,
        link_line: Decimal | None,
        line_date: date,
    ) -> str:
        # The wording joins the key: an omitted season and the default season
        # it resolves to parse alike but read differently.
        body = {
            "schema": PREVIEW_SCHEMA,
            "player_name": player_name,
            "query": query.model_dump(mode="json"),
            "filters": filters,
            "line_type": line_type.value,
            "line": None if link_line is None else str(link_line),
            "line_date": line_date.isoformat(),
        }
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
        return f"game_log_preview:v{PREVIEW_SCHEMA}:{digest}"

    def _cached_facts(self, key: str) -> PreviewFacts | None:
        payload = self._redis.call(lambda: self._redis.client.get(key))
        if not payload:
            return None
        try:
            return PreviewFacts.from_json(payload)
        except (KeyError, TypeError, ValueError):
            return None

    def _store_facts(self, key: str, facts: PreviewFacts) -> None:
        self._redis.call(lambda: self._redis.client.setex(key, FACTS_TTL_SECONDS, facts.to_json()))
