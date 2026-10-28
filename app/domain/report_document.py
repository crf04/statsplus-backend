"""The CourtAI report shape and its limits (crf04/statsplus#342).

``publish_report`` in the MCP server validates a report against this schema
before it posts it; the backend checks it again here, so a caller other than
the MCP cannot store something bigger or different.  The contract is the
"Contract" section of crf04/statsplus#342:

```text
Call   = {id: str ≤40 [a-z0-9-], tool: one of TOOLS, args: object}  ≤ 48 calls, ids unique
Report = {title ≤120, date YYYY-MM-DD?, opening: [str ≤600] ≤3,
          skill: {name ≤60, url?: https://courtai.app/…}?, footnote ≤300?,
          tabs: [Tab] 1..5}
Tab    = {title ≤30, layout: "tiles" | "rows" | "list", items: [Item] ≤60}
Item   = {title ≤120, tag?: strong|lean|thin|pass|tipped, subtitle ≤240?, key ≤240?,
          body: [str ≤1500] ≤6, bullets: [[label ≤80, text ≤300]] ≤10, watch ≤500?,
          cards: [call id] ≤8, sources: [[fact ≤200, http(s) url ≤500]] ≤12}
```

The stored call also carries the rerun's ``structured`` content, its optional
``meta`` and the ``card`` it draws; ``cards`` maps each card name to its HTML.

Text is stored as given and rendered escaped by the web app, so HTML in a
field is allowed and harmless; control characters other than tab and newline
are refused.  Every refusal is an ``invalid_input`` whose ``details.field``
names the offending field (``report.tabs[0].items[2].title``), built only from
schema keys and indexes.  Unknown keys are refused rather than dropped, so a
skill that misspells a field hears about it.

A valid body is returned normalised: an omitted list is ``[]`` and an omitted
or ``null`` optional value is left out.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.errors import InvalidInputError

TOOLS = (
    "query_game_logs",
    "get_on_off",
    "get_matchup",
    "get_rotation",
    "get_injuries",
)
TAGS = ("strong", "lean", "thin", "pass", "tipped")
LAYOUTS = ("tiles", "rows", "list")

MAX_CALLS = 48
MAX_TABS = 5
MAX_ITEMS = 60
MAX_OPENING = 3
MAX_BODY = 6
MAX_BULLETS = 10
MAX_ITEM_CARDS = 8
MAX_SOURCES = 12

TITLE_MAX = 120
OPENING_MAX = 600
SKILL_NAME_MAX = 60
URL_MAX = 500
FOOTNOTE_MAX = 300
TAB_TITLE_MAX = 30
SUBTITLE_MAX = 240
KEY_MAX = 240
BODY_MAX = 1500
BULLET_LABEL_MAX = 80
BULLET_TEXT_MAX = 300
WATCH_MAX = 500
SOURCE_FACT_MAX = 200

SKILL_URL_PREFIX = "https://courtai.app/"

_CALL_ID = re.compile(r"[a-z0-9-]{1,40}")
#: The ``<card>`` of ``ui://statsplus/<card>.html``.
_CARD_NAME = re.compile(r"[a-z0-9_-]{1,60}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HTTP_URL = re.compile(r"https?://[^\s/?#]+[^\s]*", re.IGNORECASE)
#: C0 controls except tab and newline (carriage return included), DEL and C1.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class PublishedReport:
    """A validated publish body, ready to store."""

    document: dict[str, Any]
    calls: list[dict[str, Any]]
    cards: dict[str, str]
    show_author: bool
    listed: bool = False


def _refuse(field: str, problem: str) -> InvalidInputError:
    return InvalidInputError(f"{field} {problem}.", public_details={"field": field})


def _object(value: Any, field: str, allowed: tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _refuse(field, "must be an object")
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        # The field path names the object; the unknown key itself is caller text.
        raise _refuse(field, f"has unknown keys ({len(unknown)})")
    return value


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise _refuse(field, "must be a string")
    if not value.strip():
        raise _refuse(field, "must not be empty")
    if len(value) > maximum:
        raise _refuse(field, f"must be at most {maximum} characters")
    if _CONTROL.search(value):
        raise _refuse(field, "must not contain control characters")
    return value


def _optional_text(source: Mapping[str, Any], key: str, field: str, maximum: int, out: dict) -> None:
    if source.get(key) is not None:
        out[key] = _text(source[key], field, maximum)


def _list(value: Any, field: str, maximum: int, *, minimum: int = 0) -> list[Any]:
    if value is None and minimum == 0:
        return []
    if not isinstance(value, list):
        raise _refuse(field, "must be a list")
    if len(value) < minimum:
        raise _refuse(field, f"must have at least {minimum} entr{'y' if minimum == 1 else 'ies'}")
    if len(value) > maximum:
        raise _refuse(field, f"must have at most {maximum} entries")
    return value


def _pair(value: Any, field: str) -> tuple[Any, Any]:
    if not isinstance(value, list) or len(value) != 2:
        raise _refuse(field, "must be a two-entry list")
    return value[0], value[1]


def _url(value: Any, field: str) -> str:
    url = _text(value, field, URL_MAX)
    if _HTTP_URL.fullmatch(url) is None:
        raise _refuse(field, "must be an http or https URL")
    return url


def _date(value: Any, field: str) -> str:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise _refuse(field, "must be a YYYY-MM-DD date")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise _refuse(field, "must be a valid date") from error
    return value


def _item(value: Any, field: str, call_ids: set[str]) -> dict[str, Any]:
    source = _object(
        value,
        field,
        ("title", "tag", "subtitle", "key", "body", "bullets", "watch", "cards", "sources"),
    )
    item: dict[str, Any] = {"title": _text(source.get("title"), f"{field}.title", TITLE_MAX)}
    if source.get("tag") is not None:
        if source["tag"] not in TAGS:
            raise _refuse(f"{field}.tag", f"must be one of {', '.join(TAGS)}")
        item["tag"] = source["tag"]
    _optional_text(source, "subtitle", f"{field}.subtitle", SUBTITLE_MAX, item)
    _optional_text(source, "key", f"{field}.key", KEY_MAX, item)
    item["body"] = [
        _text(paragraph, f"{field}.body[{index}]", BODY_MAX)
        for index, paragraph in enumerate(_list(source.get("body"), f"{field}.body", MAX_BODY))
    ]
    bullets = []
    for index, bullet in enumerate(_list(source.get("bullets"), f"{field}.bullets", MAX_BULLETS)):
        label, text = _pair(bullet, f"{field}.bullets[{index}]")
        bullets.append(
            [
                _text(label, f"{field}.bullets[{index}][0]", BULLET_LABEL_MAX),
                _text(text, f"{field}.bullets[{index}][1]", BULLET_TEXT_MAX),
            ]
        )
    item["bullets"] = bullets
    _optional_text(source, "watch", f"{field}.watch", WATCH_MAX, item)
    cards = []
    for index, card in enumerate(_list(source.get("cards"), f"{field}.cards", MAX_ITEM_CARDS)):
        if not isinstance(card, str) or card not in call_ids:
            raise _refuse(f"{field}.cards[{index}]", "must name a call in calls")
        cards.append(card)
    item["cards"] = cards
    sources = []
    for index, entry in enumerate(_list(source.get("sources"), f"{field}.sources", MAX_SOURCES)):
        fact, url = _pair(entry, f"{field}.sources[{index}]")
        sources.append(
            [
                _text(fact, f"{field}.sources[{index}][0]", SOURCE_FACT_MAX),
                _url(url, f"{field}.sources[{index}][1]"),
            ]
        )
    item["sources"] = sources
    return item


def _tab(value: Any, field: str, call_ids: set[str]) -> dict[str, Any]:
    source = _object(value, field, ("title", "layout", "items"))
    title = _text(source.get("title"), f"{field}.title", TAB_TITLE_MAX)
    if source.get("layout") not in LAYOUTS:
        raise _refuse(f"{field}.layout", f"must be one of {', '.join(LAYOUTS)}")
    items = [
        _item(item, f"{field}.items[{index}]", call_ids)
        for index, item in enumerate(_list(source.get("items"), f"{field}.items", MAX_ITEMS))
    ]
    return {"title": title, "layout": source["layout"], "items": items}


def _document(value: Any, call_ids: set[str]) -> dict[str, Any]:
    source = _object(value, "report", ("title", "date", "opening", "skill", "footnote", "tabs"))
    document: dict[str, Any] = {"title": _text(source.get("title"), "report.title", TITLE_MAX)}
    if source.get("date") is not None:
        document["date"] = _date(source["date"], "report.date")
    document["opening"] = [
        _text(paragraph, f"report.opening[{index}]", OPENING_MAX)
        for index, paragraph in enumerate(_list(source.get("opening"), "report.opening", MAX_OPENING))
    ]
    if source.get("skill") is not None:
        skill_source = _object(source["skill"], "report.skill", ("name", "url"))
        skill = {"name": _text(skill_source.get("name"), "report.skill.name", SKILL_NAME_MAX)}
        if skill_source.get("url") is not None:
            url = _url(skill_source["url"], "report.skill.url")
            if not url.startswith(SKILL_URL_PREFIX):
                raise _refuse("report.skill.url", f"must start with {SKILL_URL_PREFIX}")
            skill["url"] = url
        document["skill"] = skill
    _optional_text(source, "footnote", "report.footnote", FOOTNOTE_MAX, document)
    document["tabs"] = [
        _tab(tab, f"report.tabs[{index}]", call_ids)
        for index, tab in enumerate(_list(source.get("tabs"), "report.tabs", MAX_TABS, minimum=1))
    ]
    return document


def _calls(value: Any) -> list[dict[str, Any]]:
    calls = []
    seen: set[str] = set()
    for index, entry in enumerate(_list(value, "calls", MAX_CALLS)):
        field = f"calls[{index}]"
        source = _object(entry, field, ("id", "tool", "args", "structured", "meta", "card"))
        call_id = source.get("id")
        if not isinstance(call_id, str) or _CALL_ID.fullmatch(call_id) is None:
            raise _refuse(f"{field}.id", "must be 1-40 characters of a-z, 0-9 and -")
        if call_id in seen:
            raise _refuse(f"{field}.id", "must be unique")
        seen.add(call_id)
        if source.get("tool") not in TOOLS:
            raise _refuse(f"{field}.tool", f"must be one of {', '.join(TOOLS)}")
        if not isinstance(source.get("args"), Mapping):
            raise _refuse(f"{field}.args", "must be an object")
        if not isinstance(source.get("structured"), Mapping):
            raise _refuse(f"{field}.structured", "must be an object")
        meta = source.get("meta")
        if meta is not None and not isinstance(meta, Mapping):
            raise _refuse(f"{field}.meta", "must be an object")
        card = source.get("card")
        if not isinstance(card, str) or _CARD_NAME.fullmatch(card) is None:
            raise _refuse(f"{field}.card", "must be a card name of 1-60 characters of a-z, 0-9, _ and -")
        calls.append(
            {
                "id": call_id,
                "tool": source["tool"],
                "args": dict(source["args"]),
                "structured": dict(source["structured"]),
                "meta": dict(meta) if meta is not None else None,
                "card": card,
            }
        )
    return calls


def _cards(value: Any, calls: list[dict[str, Any]]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise _refuse("cards", "must be an object")
    named = {call["card"] for call in calls}
    for index, call in enumerate(calls):
        if call["card"] not in value:
            raise _refuse(f"calls[{index}].card", "must name a card whose HTML is in cards")
    cards = {}
    for name, html in value.items():
        if name not in named:
            raise _refuse("cards", "must hold only cards a call names")
        if not isinstance(html, str) or not html.strip():
            raise _refuse("cards", "must map each card name to its HTML")
        cards[name] = html
    return cards


def validate_publish_body(body: Any) -> PublishedReport:
    """The validated ``POST /api/user/reports`` body, or ``invalid_input`` naming the field."""

    source = _object(body, "body", ("report", "calls", "cards", "show_author", "listed"))
    calls = _calls(source.get("calls"))
    document = _document(source.get("report"), {call["id"] for call in calls})
    cards = _cards(source.get("cards"), calls)
    flags = {}
    for name in ("show_author", "listed"):
        flags[name] = source.get(name, False)
        if not isinstance(flags[name], bool):
            raise _refuse(name, "must be true or false")
    return PublishedReport(document=document, calls=calls, cards=cards, **flags)
