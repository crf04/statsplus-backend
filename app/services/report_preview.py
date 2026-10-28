"""The link preview image of a CourtAI report (crf04/statsplus#342).

Chat apps unfurl a shared ``/reports/<id>`` link by fetching its ``og:image``
signed out.  The card is drawn on the shared 1200x630 preview canvas: the
brand row with the report's date, its title, and up to three tagged items (the
first three in document order, spot blocks included), each a grade chip and the
item's title.  A block document with none draws its description (the opening
or lede paragraph) under the title instead.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from PIL import ImageDraw

from app.domain.report_document import tagged
from app.services.preview_card import (
    CARD_CACHE_CONTROL,
    DIM,
    GOLD,
    HIT,
    LINE,
    MARGIN,
    MISS,
    NEUTRAL,
    SURFACE,
    TEXT,
    WIDTH,
    PreviewImage,
    canvas,
    fitted,
    font,
    header,
    png,
)
from app.services.reports import describe

MAX_PREVIEW_ITEMS = 3
FOOTER = "A spot is a place to look, not a pick."

_CHIP_COLORS = {"strong": HIT, "lean": GOLD, "thin": NEUTRAL, "pass": DIM, "tipped": MISS}
_TITLE_TOP = 150
_ROWS_TOP = 300
_ROW_HEIGHT = 74
_ROW_GAP = 14
_FOOTER_Y = 590
_LEDE_SIZE = 34
_LEDE_LINES = 5


def _date_label(value: str | None) -> str:
    if value is None:
        return "Report"
    day = date.fromisoformat(value)
    return f"{day:%A}, {day:%b} {day.day}"


def tagged_items(document: dict[str, Any]) -> list[tuple[str, str]]:
    """The ``(tag, title)`` of the first three tagged items or spot blocks, in document order."""

    return tagged(document)[:MAX_PREVIEW_ITEMS]


def _wrapped(draw: ImageDraw.ImageDraw, text: str, size: Any, width: float, lines: int) -> list[str]:
    """``text`` wrapped to ``width`` on at most ``lines`` lines, the last cut with ``…`` if it runs over."""

    out: list[str] = []
    words = text.split()
    while words and len(out) < lines:
        line = words.pop(0)
        while words and draw.textlength(f"{line} {words[0]}", font=size) <= width:
            line = f"{line} {words.pop(0)}"
        out.append(line)
    if words and out:
        last = out[-1]
        while last and draw.textlength(f"{last}…", font=size) > width:
            last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
        out[-1] = f"{last}…"
    return out


def _title_lines(draw: ImageDraw.ImageDraw, title: str) -> tuple[list[str], Any]:
    """The title on one line at up to 64 px, else on two at 48 px, the second cut short."""

    width = WIDTH - 2 * MARGIN
    text, chosen = fitted(draw, title, "Bold", 64, width, 52)
    if not text.endswith("…") or text == title:
        return [text], chosen
    size = font("Bold", 48)
    words, first = title.split(), ""
    while words and draw.textlength(f"{first} {words[0]}".strip(), font=size) <= width:
        first = f"{first} {words.pop(0)}".strip()
    if not first:
        return [fitted(draw, title, "Bold", 48, width, 48)[0]], size
    rest, _ = fitted(draw, " ".join(words), "Bold", 48, width, 48)
    return [first, rest] if rest else [first], size


def _chip(draw: ImageDraw.ImageDraw, tag: str, left: float, middle: float) -> float:
    label_font = font("Bold", 28)
    label = tag.upper()
    color = _CHIP_COLORS[tag]
    # One width for every chip, so the item titles line up.
    width = max(draw.textlength(name.upper(), font=label_font) for name in _CHIP_COLORS) + 32
    draw.rounded_rectangle((left, middle - 22, left + width, middle + 22), radius=22, outline=color, width=3)
    draw.text((left + width / 2, middle), label, font=label_font, fill=color, anchor="mm")
    return left + width


def draw_report(document: dict[str, Any]) -> bytes:
    image, draw = canvas()
    header(draw, _date_label(document.get("date")))
    lines, title_font = _title_lines(draw, document["title"])
    for index, line in enumerate(lines):
        draw.text(
            (MARGIN, _TITLE_TOP + index * (title_font.size + 8)),
            line,
            font=title_font,
            fill=TEXT,
            anchor="lt",
        )
    items = tagged_items(document)
    if not items and "blocks" in document:
        description = describe(document)
        lede_font = font("SemiBold", _LEDE_SIZE)
        top = _TITLE_TOP + len(lines) * (title_font.size + 8) + 40
        for index, line in enumerate(
            _wrapped(draw, description or "", lede_font, WIDTH - 2 * MARGIN, _LEDE_LINES)
        ):
            draw.text((MARGIN, top + index * (_LEDE_SIZE + 12)), line, font=lede_font, fill=DIM, anchor="lt")
    for index, (tag, title) in enumerate(items):
        top = _ROWS_TOP + index * (_ROW_HEIGHT + _ROW_GAP)
        draw.rounded_rectangle(
            (MARGIN, top, WIDTH - MARGIN, top + _ROW_HEIGHT), radius=14, fill=SURFACE, outline=LINE
        )
        middle = top + _ROW_HEIGHT / 2
        chip_end = _chip(draw, tag, MARGIN + 20, middle)
        text, text_font = fitted(draw, title, "SemiBold", 36, WIDTH - MARGIN - 24 - chip_end - 20, 28)
        draw.text((chip_end + 20, middle), text, font=text_font, fill=TEXT, anchor="lm")
    draw.text((MARGIN, _FOOTER_Y), FOOTER, font=font("SemiBold", 26), fill=DIM, anchor="ls")
    draw.text((WIDTH - MARGIN, _FOOTER_Y), "CourtAI report", font=font("SemiBold", 26), fill=DIM, anchor="rs")
    return png(image)


def render_report_preview(document: dict[str, Any]) -> PreviewImage:
    return PreviewImage(draw_report(document), CARD_CACHE_CONTROL)
