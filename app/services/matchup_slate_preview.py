"""The link preview images of a Matchup and a Slate (crf04/statsplus#320).

Chat apps unfurl a shared ``/matchups`` or ``/matchups/<game_id>`` link by
fetching its ``og:image`` signed out, so these draw that day's points Matchup
Scores as 1200x630 PNGs.  The contract is crf04/statsplus
``docs/contracts/matchup-slate-preview.md``.

The scores are the ones ``GET /api/games/slate/edges`` lists, read through
:meth:`SlateEdgesService.preview_read`, so its Redis response cache serves
them; the PNG is drawn per request.  :func:`matchup_card` and
:func:`slate_card` reduce them to the facts a card shows, and
:func:`draw_matchup` and :func:`draw_slate` draw those facts.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from nba_api.stats.static import teams as nba_teams
from PIL import ImageDraw

from app.domain.nba_events import is_final_event, is_started_event, season_for_game_id
from app.domain.slate_time import slate_date_for_instant
from app.domain.utc import parse_utc_iso
from app.errors import ProviderUnavailableError, ResourceNotFoundError
from app.services.preview_card import (
    CARD_CACHE_CONTROL,
    DIM,
    FALLBACK_CACHE_CONTROL,
    HIT,
    LINE,
    MARGIN,
    MISS,
    OUTAGES,
    SURFACE,
    TEXT,
    WIDTH,
    GuardedRedis,
    PreviewImage,
    RenderBudget,
    canvas,
    draw_fallback,
    fitted,
    font,
    header,
    png,
)
from app.services.slate_fallback import NEXT_GAME_DAY

logger = logging.getLogger(__name__)

#: Uncached renders per minute of both cards together, across every worker.
RENDER_BUDGET_PER_MINUTE = 60
#: The same, in one process while Redis is unavailable.
LOCAL_RENDER_BUDGET_PER_MINUTE = 15
#: A team with more drawn scores shows its 5 highest and 5 lowest.
MAX_ROWS_PER_TEAM = 10
#: A Slate tile names a softest (toughest) score only at +2% (-2%) or beyond, after rounding.
SLATE_EDGE_PERCENT = 2
#: Out lines are left off nights with more games than this.
MAX_GAMES_WITH_OUT_LINE = 12
GRID_COLUMNS = 4
_EASTERN = ZoneInfo("America/New_York")


# -- facts ------------------------------------------------------------------------


def whole_percent(value: float) -> int:
    """A score's fraction as a whole percent, an exact half rounded away from zero (``0.025`` is 3)."""

    return int((Decimal(str(value)) * 100).quantize(Decimal(1), ROUND_HALF_UP))


def date_label(day: date) -> str:
    """``Saturday, Oct 24``."""

    return f"{day:%A}, {day:%b} {day.day}"


@dataclass(frozen=True)
class ScoreRow:
    name: str
    tricode: str
    percent: int


@dataclass(frozen=True)
class TeamColumn:
    """One team's heading and drawn rows; ``more`` scores are not drawn."""

    tricode: str
    caption: str
    rows: tuple[ScoreRow, ...]
    more: int


@dataclass(frozen=True)
class MatchupCard:
    date: str
    away: TeamColumn
    home: TeamColumn
    #: Every drawable score of the game, drawn or counted in a ``+N more``.
    scored: int


@dataclass(frozen=True)
class GameTile:
    away: str
    home: str
    #: The ET tip time without a suffix, or ``Live``, ``Final`` or the status label.
    tip: str
    #: ``False`` when the game has no drawable score.
    scored: bool
    softest: ScoreRow | None
    toughest: ScoreRow | None
    #: Players listed Out; ``None`` when no Out line is drawn.
    out: int | None


@dataclass(frozen=True)
class SlateCard:
    date: str
    tiles: tuple[GameTile, ...]


def _drawable(players: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The scores a card draws, in order: thin ones left out; score, then
    Season scoring, descending, then canonical id ascending."""

    drawn = [player for player in players if not player["thin"] and player["value"] is not None]
    return sorted(
        drawn,
        key=lambda player: (
            -player["value"],
            -(player["season_scoring"] or 0),
            int(player["canonical_id"]),
        ),
    )


def _row(player: Mapping[str, Any]) -> ScoreRow:
    return ScoreRow(str(player["name"]), str(player["tricode"]), whole_percent(player["value"]))


def _nickname(team: Mapping[str, Any]) -> str:
    known = nba_teams.find_team_name_by_id(int(team["team_id"]))
    if known:
        return known["nickname"]
    name = team.get("name")
    return str(name).split()[-1] if name else str(team["tricode"])


def _column(team: Mapping[str, Any], side: str, players: Sequence[Mapping[str, Any]]) -> TeamColumn:
    own = [player for player in players if int(player["team_id"]) == int(team["team_id"])]
    if len(own) > MAX_ROWS_PER_TEAM:
        half = MAX_ROWS_PER_TEAM // 2
        shown, more = own[:half] + own[-half:], len(own) - MAX_ROWS_PER_TEAM
    else:
        shown, more = own, 0
    return TeamColumn(
        tricode=str(team["tricode"]),
        caption=f"{_nickname(team)} · {side}",
        rows=tuple(_row(player) for player in shown),
        more=more,
    )


def matchup_card(
    slate_date: str, game: Mapping[str, Any], players: Sequence[Mapping[str, Any]]
) -> MatchupCard:
    """The Matchup card of one Slate game from its edges ``players``."""

    drawn = _drawable(players)
    away = _column(game["away_team"], "away", drawn)
    home = _column(game["home_team"], "home", drawn)
    return MatchupCard(
        date=date_label(date.fromisoformat(slate_date)),
        away=away,
        home=home,
        scored=sum(len(column.rows) + column.more for column in (away, home)),
    )


def _tip(game: Mapping[str, Any]) -> str:
    status = game.get("status") or {}
    state = status.get("state")
    label = str(status.get("label") or "").strip()
    if state == "final" or is_final_event({"status_text": label}):
        return "Final"
    if state == "scheduled":
        # A game clock (``Q3 5:21``, ``Halftime``) is live; any other label
        # (``Scheduled``, ``7:30 pm ET``) is a game yet to tip.
        if is_started_event({"status_text": label}):
            return "Live"
        tip = parse_utc_iso(str(game["scheduled_at"])).astimezone(_EASTERN)
        return f"{tip.hour % 12 or 12}:{tip.minute:02d}"
    return label or str(state).capitalize()


def slate_card(slate_date: str, games: Sequence[Mapping[str, Any]], edges: Mapping[str, Any]) -> SlateCard:
    """The Slate card from the Slate's games, in its order, and their edges."""

    players_by_game = {entry["game_id"]: entry.get("players") or [] for entry in edges.get("games") or []}
    with_out_line = len(games) <= MAX_GAMES_WITH_OUT_LINE
    tiles = []
    for game in games:
        drawn = _drawable(players_by_game.get(game["game_id"], []))
        softest = toughest = None
        if drawn:
            high, low = _row(drawn[0]), _row(drawn[-1])
            softest = high if high.percent >= SLATE_EDGE_PERCENT else None
            toughest = low if low.percent <= -SLATE_EDGE_PERCENT else None
        counts = game.get("injury_counts")
        tiles.append(
            GameTile(
                away=str(game["away_team"]["tricode"]),
                home=str(game["home_team"]["tricode"]),
                tip=_tip(game),
                scored=bool(drawn),
                softest=softest,
                toughest=toughest,
                out=int(counts.get("Out") or 0) if with_out_line and counts is not None else None,
            )
        )
    return SlateCard(date=date_label(date.fromisoformat(slate_date)), tiles=tuple(tiles))


# -- drawing -----------------------------------------------------------------------

_TOP = 124
_FOOTER_Y = 578
_ROWS_BOTTOM = 548


def _percent_text(percent: int) -> tuple[str, tuple[int, int, int]]:
    if percent > 0:
        return f"+{percent}%", HIT
    if percent < 0:
        return f"{percent}%", MISS
    return "0%", DIM


def _legend(draw: ImageDraw.ImageDraw) -> None:
    """``◀ tougher · softer ▶``; Barlow has no ◀ or ▶, so they are drawn as triangles."""

    legend_font = font("SemiBold", 22)
    arrow, space = 12, 8
    parts = (("tougher", MISS), (" · ", DIM), ("softer", HIT))
    width = 2 * (arrow + space) + sum(draw.textlength(text, font=legend_font) for text, _ in parts)
    x = WIDTH - MARGIN - width
    middle = _FOOTER_Y - 8
    draw.polygon(((x, middle), (x + arrow, middle - 7), (x + arrow, middle + 7)), fill=MISS)
    x += arrow + space
    for text, color in parts:
        draw.text((x, _FOOTER_Y), text, font=legend_font, fill=color, anchor="ls")
        x += draw.textlength(text, font=legend_font)
    x += space
    draw.polygon(((x, middle - 7), (x + arrow, middle), (x, middle + 7)), fill=HIT)


def _bar(
    draw: ImageDraw.ImageDraw,
    left: float,
    right: float,
    middle: float,
    height: float,
    percent: int,
    scale: int,
) -> None:
    axis = (left + right) / 2
    half = (right - left) / 2 - 2
    draw.rectangle((axis - 1, middle - height / 2 - 2, axis + 1, middle + height / 2 + 2), fill=LINE)
    if percent == 0 or scale == 0:
        return
    length = max(4.0, half * abs(percent) / scale)
    top, bottom = middle - height / 2, middle + height / 2
    radius = min(height, length) / 2
    start, end = (axis + 2, axis + 2 + length) if percent > 0 else (axis - 2 - length, axis - 2)
    color = HIT if percent > 0 else MISS
    # Rounded at the outer end only (Pillow's ``corners`` fails on a pill this short).
    draw.rounded_rectangle((start, top, end, bottom), radius=radius, fill=color)
    inner = (start, top, start + radius, bottom) if percent > 0 else (end - radius, top, end, bottom)
    draw.rectangle(inner, fill=color)


def _matchup_column(
    draw: ImageDraw.ImageDraw, column: TeamColumn, left: float, right: float, row_height: float, scale: int
) -> None:
    tricode_font = font("Bold", 72)
    draw.text((left, 186), column.tricode, font=tricode_font, fill=TEXT, anchor="ls")
    caption_left = left + draw.textlength(column.tricode, font=tricode_font) + 12
    caption, caption_font = fitted(draw, column.caption, "SemiBold", 26, right - caption_left, 20)
    draw.text((caption_left, 186), caption, font=caption_font, fill=DIM, anchor="ls")

    size = int(min(32, max(20, row_height * 0.7)))
    row_font = font("Bold", size)
    name_width = (right - left) * 0.44
    percent_width = draw.textlength("-10%", font=row_font) + 4
    bar_left = left + name_width + 12
    bar_right = right - percent_width - 12
    top = 204
    for index, row in enumerate(column.rows):
        middle = top + row_height * (index + 0.5)
        name, name_font = fitted(draw, row.name, "Bold", size, name_width, max(16, size - 6))
        draw.text((left, middle), name, font=name_font, fill=TEXT, anchor="lm")
        _bar(draw, bar_left, bar_right, middle, max(8, row_height * 0.5), row.percent, scale)
        text, color = _percent_text(row.percent)
        draw.text((right, middle), text, font=row_font, fill=color, anchor="rm")
    if column.more:
        middle = top + row_height * (len(column.rows) + 0.5)
        draw.text(
            (left, middle),
            f"+{column.more} more players",
            font=font("SemiBold", max(18, size - 4)),
            fill=DIM,
            anchor="lm",
        )


def draw_matchup(card: MatchupCard) -> bytes:
    """The Matchup card, or its "No Matchup Scores yet" card."""

    image, draw = canvas()
    header(draw, f"{card.date} · Matchup")
    gutter = 40
    middle = WIDTH / 2
    columns = ((card.away, MARGIN, middle - gutter / 2), (card.home, middle + gutter / 2, WIDTH - MARGIN))
    if not card.scored:
        for column, left, right in columns:
            _matchup_column(draw, TeamColumn(column.tricode, column.caption, (), 0), left, right, 40, 0)
        draw.rounded_rectangle((MARGIN, 224, WIDTH - MARGIN, 530), radius=16, fill=SURFACE)
        draw.text((WIDTH / 2, 377), "No Matchup Scores yet", font=font("Bold", 64), fill=DIM, anchor="mm")
        draw.text(
            (MARGIN, _FOOTER_Y),
            "Points Matchup Score vs league",
            font=font("SemiBold", 22),
            fill=DIM,
            anchor="ls",
        )
        return png(image)

    draw.rectangle((middle - 1, _TOP, middle + 1, _ROWS_BOTTOM), fill=LINE)
    slots = max(len(column.rows) + (1 if column.more else 0) for column, _, _ in columns)
    row_height = min(44.0, (_ROWS_BOTTOM - 204) / max(slots, 1))
    scale = max((abs(row.percent) for column, _, _ in columns for row in column.rows), default=0)
    for column, left, right in columns:
        _matchup_column(draw, column, left, right, row_height, scale)
    footer = f"Points Matchup Score vs league · all {card.scored} scored players"
    draw.text((MARGIN, _FOOTER_Y), footer, font=font("SemiBold", 22), fill=DIM, anchor="ls")
    _legend(draw)
    return png(image)


@dataclass(frozen=True)
class _TileMetrics:
    padding: int
    heading: int
    tip: int
    name: int
    tricode: int
    percent: int
    note: int
    line: int


#: Type sizes by grid rows: two rows (up to 8 games) get the prototype's sizes.
_TILE_METRICS = {
    1: _TileMetrics(16, 40, 22, 26, 19, 30, 20, 36),
    2: _TileMetrics(16, 40, 22, 26, 19, 30, 20, 36),
    3: _TileMetrics(12, 30, 20, 22, 16, 26, 17, 27),
    4: _TileMetrics(9, 27, 18, 20, 15, 23, 16, 24),
}


def _edge_line(
    draw: ImageDraw.ImageDraw, row: ScoreRow, left: float, right: float, middle: float, metrics: _TileMetrics
) -> None:
    text, color = _percent_text(row.percent)
    percent_font = font("Bold", metrics.percent)
    draw.text((right, middle), text, font=percent_font, fill=color, anchor="rm")
    tricode_font = font("SemiBold", metrics.tricode)
    gap = 6
    room = (
        right
        - left
        - draw.textlength(text, font=percent_font)
        - 8
        - gap
        - draw.textlength(row.tricode, font=tricode_font)
    )
    name, name_font = fitted(draw, row.name, "Bold", metrics.name, room, metrics.name - 4)
    draw.text((left, middle), name, font=name_font, fill=TEXT, anchor="lm")
    tricode_left = left + draw.textlength(name, font=name_font) + gap
    # Sit the smaller tricode on the name's baseline.
    baseline = middle + font("Bold", metrics.name).getmetrics()[0] / 2 - 2
    draw.text((tricode_left, baseline), row.tricode, font=tricode_font, fill=DIM, anchor="ls")


def _tile(
    draw: ImageDraw.ImageDraw, tile: GameTile, box: tuple[float, float, float, float], metrics: _TileMetrics
) -> None:
    left, top, right, bottom = box
    draw.rounded_rectangle(box, radius=14, fill=SURFACE)
    inner_left, inner_right = left + metrics.padding, right - metrics.padding
    heading_font, at_font = font("Bold", metrics.heading), font("SemiBold", int(metrics.heading * 0.7))
    baseline = top + metrics.padding + metrics.heading * 0.82
    x = inner_left
    for text, chosen, color in (
        (tile.away, heading_font, TEXT),
        (" @ ", at_font, DIM),
        (tile.home, heading_font, TEXT),
    ):
        draw.text((x, baseline), text, font=chosen, fill=color, anchor="ls")
        x += draw.textlength(text, font=chosen)
    tip, tip_font = fitted(draw, tile.tip, "SemiBold", metrics.tip, inner_right - x - 6, metrics.tip - 4)
    draw.text((inner_right, baseline), tip, font=tip_font, fill=DIM, anchor="rs")

    line_top = baseline + metrics.line * 0.25
    note_font = font("SemiBold", metrics.note + 2)
    if not tile.scored:
        draw.text(
            (inner_left, line_top + metrics.line / 2), "No scores yet", font=note_font, fill=DIM, anchor="lm"
        )
    else:
        for index, (row, empty) in enumerate(
            (
                (tile.softest, f"nothing above +{SLATE_EDGE_PERCENT}%"),
                (tile.toughest, f"nothing below -{SLATE_EDGE_PERCENT}%"),
            )
        ):
            middle = line_top + metrics.line * (index + 0.5)
            if row is None:
                draw.text((inner_left, middle), empty, font=note_font, fill=DIM, anchor="lm")
            else:
                _edge_line(draw, row, inner_left, inner_right, middle, metrics)
    if tile.out is not None:
        text, color = (f"{tile.out} listed Out", MISS) if tile.out else ("No one Out", DIM)
        draw.text(
            (inner_left, bottom - metrics.padding),
            text,
            font=font("SemiBold", metrics.note),
            fill=color,
            anchor="ls",
        )


def draw_slate(card: SlateCard) -> bytes:
    """The Slate card, or its "No games on ..." card."""

    image, draw = canvas()
    count = len(card.tiles)
    if not count:
        header(draw, card.date)
        draw.rounded_rectangle((MARGIN, _TOP + 16, WIDTH - MARGIN, _ROWS_BOTTOM), radius=16, fill=SURFACE)
        draw.text(
            (WIDTH / 2, (_TOP + 16 + _ROWS_BOTTOM) / 2),
            f"No games on {card.date}",
            font=font("Bold", 64),
            fill=DIM,
            anchor="mm",
        )
        return png(image)

    header(draw, f"{card.date} · {count} game{'' if count == 1 else 's'} · ET")
    rows = math.ceil(count / GRID_COLUMNS)
    metrics = _TILE_METRICS[min(rows, 4)]
    gap = 14
    width = (WIDTH - 2 * MARGIN - gap * (GRID_COLUMNS - 1)) / GRID_COLUMNS
    grid_bottom = _ROWS_BOTTOM - 6
    height = (grid_bottom - _TOP - gap * (rows - 1)) / rows
    for index, tile in enumerate(card.tiles):
        row, column = divmod(index, GRID_COLUMNS)
        left = MARGIN + column * (width + gap)
        top = _TOP + row * (height + gap)
        _tile(draw, tile, (left, top, left + width, top + height), metrics)
    draw.text(
        (MARGIN, _FOOTER_Y),
        "Softest and toughest points Matchup Score per game, vs league",
        font=font("SemiBold", 22),
        fill=DIM,
        anchor="ls",
    )
    return png(image)


# -- the service the routes call ----------------------------------------------------


class MatchupSlatePreviewService:
    """Draw the Slate and Matchup preview cards from the slate edges service.

    ``edges`` is the :class:`SlateEdgesService` and ``slates`` the Slate
    service.  An edges read its Redis cache cannot answer is an uncached
    render, counted against one budget both cards share.
    """

    def __init__(
        self,
        *,
        edges: Any,
        slates: Any,
        redis_client: Any | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._edges = edges
        self._slates = slates
        self._clock = clock
        self._budget = RenderBudget(
            GuardedRedis(redis_client, name="Matchup and Slate preview budget"),
            key_prefix="matchup_slate_preview:renders",
            per_minute=RENDER_BUDGET_PER_MINUTE,
            local_per_minute=LOCAL_RENDER_BUDGET_PER_MINUTE,
            clock=clock,
        )

    def slate(self, requested_date: str | None) -> PreviewImage:
        """The Slate card of ``requested_date``, or with none the day the Slate page shows."""

        try:
            try:
                read = self._edges.preview_read(
                    requested_date,
                    fallback=None if requested_date is not None else NEXT_GAME_DAY,
                    may_compose=self._budget.take,
                )
            except ResourceNotFoundError:
                # No schedule is stored for the date's season: no games that day.
                day = (
                    requested_date
                    or slate_date_for_instant(datetime.fromtimestamp(self._clock(), UTC)).isoformat()
                )
                return PreviewImage(
                    draw_slate(SlateCard(date_label(date.fromisoformat(day)), ())), CARD_CACHE_CONTROL
                )
            if read is None:
                return PreviewImage(draw_fallback(), FALLBACK_CACHE_CONTROL)
            slate, edges = read
            if any(entry.get("outage") for entry in edges["games"]):
                raise ProviderUnavailableError("A game's Matchup could not be read.")
            card = slate_card(slate["slate_date"], slate["games"], edges)
            return PreviewImage(draw_slate(card), CARD_CACHE_CONTROL)
        except OUTAGES as error:
            logger.warning("Slate preview read failed; serving the fallback: %s", error)
            return PreviewImage(draw_fallback(), FALLBACK_CACHE_CONTROL)

    def matchup(self, game_id: str) -> PreviewImage:
        """The Matchup card of one game; an unknown game is ``resource_not_found``."""

        try:
            slate_date = self._slates.game_slate_date(game_id)
            # The ID's own season, and a game missing from its day's Slate is
            # refused before the budget can turn it into the fallback.
            read = self._edges.preview_read(
                slate_date,
                season=season_for_game_id(game_id),
                game_id=game_id,
                may_compose=self._budget.take,
            )
            if read is None:
                return PreviewImage(draw_fallback(), FALLBACK_CACHE_CONTROL)
            slate, edges = read
            (game,) = (game for game in slate["games"] if game["game_id"] == game_id)
            entry = next((entry for entry in edges["games"] if entry["game_id"] == game_id), {})
            if entry.get("outage"):
                raise ProviderUnavailableError("The game's Matchup could not be read.")
            players = entry.get("players") or []
            return PreviewImage(
                draw_matchup(matchup_card(slate["slate_date"], game, players)), CARD_CACHE_CONTROL
            )
        except OUTAGES as error:
            logger.warning("Matchup preview read failed; serving the fallback: %s", error)
            return PreviewImage(draw_fallback(), FALLBACK_CACHE_CONTROL)
