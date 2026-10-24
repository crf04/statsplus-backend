"""What every link preview card shares: canvas, fonts, colours, brand row,
fallback image and the render budget.

The Game Logs card (crf04/statsplus#285) and the Matchup and Slate cards
(crf04/statsplus#320) are drawn on the same 1200x630 canvas in the app's
CourtAI colours, and answer with the same brand image when no card can be
drawn right now.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import redis
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy.exc import SQLAlchemyError

from app.errors import ProviderUnavailableError, QueryTimeoutError, SeasonUnavailableError
from app.utils.redis_breaker import RedisCircuitBreaker

logger = logging.getLogger(__name__)

WIDTH, HEIGHT = 1200, 630
CARD_CACHE_CONTROL = "public, max-age=3600"
FALLBACK_CACHE_CONTROL = "public, max-age=60"

_FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"
#: CourtAI tokens (frontend ``src/theme.css``).
INK = (13, 11, 7)
SURFACE = (22, 19, 13)
LINE = (52, 46, 34)
TEXT = (239, 233, 220)
DIM = (155, 147, 127)
GOLD = (232, 163, 61)
HIT = (76, 175, 125)
MISS = (194, 78, 78)
NEUTRAL = (122, 134, 153)
LOGO = (249, 115, 22)
MARGIN = 64

#: A failed read a preview answers with the fallback rather than an error.
OUTAGES = (ProviderUnavailableError, QueryTimeoutError, SeasonUnavailableError, SQLAlchemyError)
_REDIS_DOWN = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


@dataclass(frozen=True)
class PreviewImage:
    png: bytes
    cache_control: str


@cache
def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONTS / f"BarlowCondensed-{weight}.ttf"), size)


def canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), INK)
    draw = ImageDraw.Draw(image)
    # The app's warm glow at the top of the page.
    for step in range(48):
        mix = step / 48
        color = tuple(round(a + (b - a) * mix) for a, b in zip((26, 21, 12), INK, strict=True))
        draw.rectangle((0, step * 6, WIDTH, step * 6 + 6), fill=color)
    return image, draw


def logo(draw: ImageDraw.ImageDraw, left: int, top: int, size: int) -> None:
    """The CourtAI mark (``public/logo.svg``) scaled into a ``size`` square."""

    draw.rounded_rectangle((left, top, left + size, top + size), radius=size // 6, fill=LOGO)

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


def header(draw: ImageDraw.ImageDraw, right: str = "courtai.app") -> None:
    """The brand row: logo and ``CourtAI`` on the left, ``right`` dim on the right."""

    logo(draw, MARGIN, 44, 52)
    draw.text((MARGIN + 68, 70), "CourtAI", font=font("SemiBold", 40), fill=TEXT, anchor="lm")
    draw.text((WIDTH - MARGIN, 70), right, font=font("SemiBold", 30), fill=DIM, anchor="rm")


def fitted(
    draw: ImageDraw.ImageDraw, text: str, weight: str, size: int, width: float, smallest: int
) -> tuple[str, ImageFont.FreeTypeFont]:
    """``text`` on one line in the largest font down to ``smallest`` that fits ``width``, else cut short with an ellipsis."""

    text = re.sub(r"\s+", " ", text)
    while size > smallest and draw.textlength(text, font=font(weight, size)) > width:
        size -= 2
    chosen = font(weight, size)
    if draw.textlength(text, font=chosen) <= width:
        return text, chosen
    while text and draw.textlength(text.rstrip() + "…", font=chosen) > width:
        text = text[:-1]
    return text.rstrip() + "…", chosen


def png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@cache
def draw_fallback() -> bytes:
    """The brand image served when no card can be drawn right now."""

    image, draw = canvas()
    logo(draw, WIDTH // 2 - 90, 150, 180)
    draw.text((WIDTH / 2, 420), "CourtAI", font=font("Bold", 110), fill=TEXT, anchor="mm")
    draw.text(
        (WIDTH / 2, 505), "NBA game logs and matchups", font=font("SemiBold", 44), fill=DIM, anchor="mm"
    )
    return png(image)


class GuardedRedis:
    """Redis calls that answer ``None`` when Redis is absent or failing, never raise."""

    def __init__(self, client: Any | None, *, name: str) -> None:
        self.client = client
        self._name = name
        self._breaker = RedisCircuitBreaker(message=f"{name} unavailable (%s); bypassing for %ss")

    def call(self, call: Callable[[], Any]) -> Any:
        if self.client is None or self._breaker.is_open():
            return None
        try:
            return call()
        except _REDIS_DOWN as error:
            self._breaker.record_failure(error)
        except Exception:
            logger.warning("%s Redis call failed", self._name, exc_info=True)
        return None


class RenderBudget:
    """Uncached renders per minute: one global count in Redis, else a smaller one per process."""

    def __init__(
        self,
        redis_calls: GuardedRedis,
        *,
        key_prefix: str,
        per_minute: int,
        local_per_minute: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis_calls
        self._key_prefix = key_prefix
        self._per_minute = per_minute
        self._local_per_minute = local_per_minute
        self._clock = clock
        self._local_lock = threading.Lock()
        self._local_minute: int | None = None
        self._local_renders = 0

    def take(self) -> bool:
        """Count one uncached render; ``False`` once this minute's budget is spent."""

        minute = int(self._clock() // 60)
        key = f"{self._key_prefix}:{minute}"

        def count() -> int:
            pipeline = self._redis.client.pipeline()
            pipeline.incr(key)
            pipeline.expire(key, 120)
            return int(pipeline.execute()[0])

        used = self._redis.call(count)
        if used is not None:
            return used <= self._per_minute
        with self._local_lock:
            # A request whose Redis call stalled across a minute boundary carries
            # the earlier minute; it counts against the current window, never rewinds it.
            if self._local_minute is None or minute > self._local_minute:
                self._local_minute, self._local_renders = minute, 0
            self._local_renders += 1
            return self._local_renders <= self._local_per_minute
