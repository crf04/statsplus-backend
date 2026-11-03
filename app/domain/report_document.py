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

A report may instead be a block document (crf04/statsplus#380): exactly one of
``tabs`` or ``blocks``, with no version key.

```text
Report = {title ≤120, date?, skill?, footnote ≤300?, opening? [≤600] ≤3, blocks: [Block] 1..120}
Block  = {id [a-z0-9-]{1,40} unique across the whole document, type, ...}
  heading   {text ≤120}
  paragraph {text ≤1500, lede?: bool}
  list      {items [≤300] 1..12, ordered?: bool}
  callout   {title? ≤40, text ≤1000, tone?: "warn"|"plain"}
  card      {card: call id}
  cards     {cards: [call id] 2..3}
  spot      {tag?, title ≤120, subtitle? ≤240, key? ≤240, cards? [call id] ≤8}
  table     {columns [≤40] 1..6, rows [[cell ≤200]] 1..20}   each row has len(columns) cells
  items     {layout tiles|rows|list, items [Item] ≤60}
  sources   {sources [[fact ≤200, http(s) url ≤500]] 1..12}
  tabs      {tabs [{title ≤30, blocks [Block] 1..60}] 2..5}  top level only
```

The 120-block limit counts every block, those inside a ``tabs`` group included.

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

The author's ``notes`` and edits (crf04/statsplus#377) are checked here too.
Notes are plain text of at most :data:`NOTES_MAX` characters with no link
(``http://``, ``https://`` or ``www.``); those two refusals are the flat
``notes_too_long`` and ``notes_links``.  An edit body names items by tab and
item index and lists each one's cards as a subset of the cards it was
published with; anything else about its items is the flat ``invalid_edits``.
A block document's edit body names blocks by ``id`` instead, each hidden or
with its ``cards`` (only on a ``cards``, ``spot`` or ``items`` block) a subset
of the block's published cards.  :func:`apply_edits` lays a stored edit body over the published document and
calls, which are never changed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from app.errors import FlatBodyError, InvalidInputError

TOOLS = (
    "query_game_logs",
    "get_on_off",
    "get_matchup",
    "get_rotation",
    "get_injuries",
)
TAGS = ("strong", "lean", "thin", "pass", "tipped")
LAYOUTS = ("tiles", "rows", "list")
BLOCK_TYPES = (
    "heading",
    "paragraph",
    "list",
    "callout",
    "card",
    "cards",
    "spot",
    "table",
    "items",
    "sources",
    "tabs",
)
TONES = ("warn", "plain")
#: The block types whose cards an edit may reorder or remove.
CARD_BLOCKS = ("cards", "spot", "items")

MAX_CALLS = 48
MAX_TABS = 5
MAX_ITEMS = 60
MAX_OPENING = 3
MAX_BODY = 6
MAX_BULLETS = 10
MAX_ITEM_CARDS = 8
MAX_SOURCES = 12
MAX_BLOCKS = 120
MAX_TAB_BLOCKS = 60
MIN_GROUP_TABS = 2
MAX_LIST_ITEMS = 12
MIN_ROW_CARDS = 2
MAX_ROW_CARDS = 3
MAX_TABLE_COLUMNS = 6
MAX_TABLE_ROWS = 20

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
NOTES_MAX = 2000
LIST_ITEM_MAX = 300
CALLOUT_TITLE_MAX = 40
CALLOUT_TEXT_MAX = 1000
TABLE_COLUMN_MAX = 40
TABLE_CELL_MAX = 200

SKILL_URL_PREFIX = "https://courtai.app/"

_CALL_ID = re.compile(r"[a-z0-9-]{1,40}")
#: The ``<card>`` of ``ui://statsplus/<card>.html``.
_CARD_NAME = re.compile(r"[a-z0-9_-]{1,60}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HTTP_URL = re.compile(r"https?://[^\s/?#]+[^\s]*", re.IGNORECASE)
#: C0 controls except tab and newline (carriage return included), DEL and C1.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_NOTES_LINK = re.compile(r"https?://|www\.", re.IGNORECASE)
#: Light formatting (crf04/statsplus#380): ``**bold**``, ``*italic*`` and ``- `` bullet lines.
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"\*(.+?)\*")
_BULLET = re.compile(r"^- ", re.MULTILINE)


class ReportOrigin(StrEnum):
    """Who wrote a report (crf04/statsplus#382): an AI assistant, or its author in the Report Builder."""

    ASSISTANT = "assistant"
    BUILDER = "builder"


@dataclass(frozen=True)
class PublishedReport:
    """A validated publish body, ready to store."""

    document: dict[str, Any]
    calls: list[dict[str, Any]]
    cards: dict[str, str]
    show_author: bool
    listed: bool = False
    notes: str | None = None
    origin: ReportOrigin = ReportOrigin.ASSISTANT


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


def _flag(source: Mapping[str, Any], key: str, field: str, out: dict) -> None:
    if source.get(key) is not None:
        if not isinstance(source[key], bool):
            raise _refuse(field, "must be true or false")
        out[key] = source[key]


def _tag(source: Mapping[str, Any], field: str, out: dict) -> None:
    if source.get("tag") is not None:
        if source["tag"] not in TAGS:
            raise _refuse(field, f"must be one of {', '.join(TAGS)}")
        out["tag"] = source["tag"]


def _card_list(value: Any, field: str, call_ids: set[str], maximum: int, *, minimum: int = 0) -> list[str]:
    cards = []
    for index, card in enumerate(_list(value, field, maximum, minimum=minimum)):
        if not isinstance(card, str) or card not in call_ids:
            raise _refuse(f"{field}[{index}]", "must name a call in calls")
        cards.append(card)
    return cards


def _sources(value: Any, field: str, *, minimum: int = 0) -> list[list[str]]:
    sources = []
    for index, entry in enumerate(_list(value, field, MAX_SOURCES, minimum=minimum)):
        fact, url = _pair(entry, f"{field}[{index}]")
        sources.append(
            [_text(fact, f"{field}[{index}][0]", SOURCE_FACT_MAX), _url(url, f"{field}[{index}][1]")]
        )
    return sources


def _item(value: Any, field: str, call_ids: set[str]) -> dict[str, Any]:
    source = _object(
        value,
        field,
        ("title", "tag", "subtitle", "key", "body", "bullets", "watch", "cards", "sources"),
    )
    item: dict[str, Any] = {"title": _text(source.get("title"), f"{field}.title", TITLE_MAX)}
    _tag(source, f"{field}.tag", item)
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
    item["cards"] = _card_list(source.get("cards"), f"{field}.cards", call_ids, MAX_ITEM_CARDS)
    item["sources"] = _sources(source.get("sources"), f"{field}.sources")
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


_BLOCK_KEYS: dict[str, tuple[str, ...]] = {
    "heading": ("text",),
    "paragraph": ("text", "lede"),
    "list": ("items", "ordered"),
    "callout": ("title", "text", "tone"),
    "card": ("card",),
    "cards": ("cards",),
    "spot": ("tag", "title", "subtitle", "key", "cards"),
    "table": ("columns", "rows"),
    "items": ("layout", "items"),
    "sources": ("sources",),
    "tabs": ("tabs",),
}


def _block(value: Any, field: str, call_ids: set[str], ids: set[str], *, nested: bool) -> dict[str, Any]:
    """One validated block; ``ids`` collects every block id so far, across the whole document."""

    if not isinstance(value, Mapping):
        raise _refuse(field, "must be an object")
    kind = value.get("type")
    if kind not in BLOCK_TYPES:
        raise _refuse(f"{field}.type", f"must be one of {', '.join(BLOCK_TYPES)}")
    if kind == "tabs" and nested:
        raise _refuse(f"{field}.type", "must not be tabs inside tabs")
    source = _object(value, field, ("id", "type", *_BLOCK_KEYS[kind]))
    block_id = source.get("id")
    if not isinstance(block_id, str) or _CALL_ID.fullmatch(block_id) is None:
        raise _refuse(f"{field}.id", "must be 1-40 characters of a-z, 0-9 and -")
    if block_id in ids:
        raise _refuse(f"{field}.id", "must be unique across the report")
    ids.add(block_id)
    if len(ids) > MAX_BLOCKS:
        raise _refuse("report.blocks", f"must hold at most {MAX_BLOCKS} blocks, counting those inside tabs")
    block: dict[str, Any] = {"id": block_id, "type": kind}
    if kind == "heading":
        block["text"] = _text(source.get("text"), f"{field}.text", TITLE_MAX)
    elif kind == "paragraph":
        block["text"] = _text(source.get("text"), f"{field}.text", BODY_MAX)
        _flag(source, "lede", f"{field}.lede", block)
    elif kind == "list":
        block["items"] = [
            _text(entry, f"{field}.items[{index}]", LIST_ITEM_MAX)
            for index, entry in enumerate(
                _list(source.get("items"), f"{field}.items", MAX_LIST_ITEMS, minimum=1)
            )
        ]
        _flag(source, "ordered", f"{field}.ordered", block)
    elif kind == "callout":
        _optional_text(source, "title", f"{field}.title", CALLOUT_TITLE_MAX, block)
        block["text"] = _text(source.get("text"), f"{field}.text", CALLOUT_TEXT_MAX)
        if source.get("tone") is not None:
            if source["tone"] not in TONES:
                raise _refuse(f"{field}.tone", f"must be one of {', '.join(TONES)}")
            block["tone"] = source["tone"]
    elif kind == "card":
        card = source.get("card")
        if not isinstance(card, str) or card not in call_ids:
            raise _refuse(f"{field}.card", "must name a call in calls")
        block["card"] = card
    elif kind == "cards":
        block["cards"] = _card_list(
            source.get("cards"), f"{field}.cards", call_ids, MAX_ROW_CARDS, minimum=MIN_ROW_CARDS
        )
    elif kind == "spot":
        _tag(source, f"{field}.tag", block)
        block["title"] = _text(source.get("title"), f"{field}.title", TITLE_MAX)
        _optional_text(source, "subtitle", f"{field}.subtitle", SUBTITLE_MAX, block)
        _optional_text(source, "key", f"{field}.key", KEY_MAX, block)
        block["cards"] = _card_list(source.get("cards"), f"{field}.cards", call_ids, MAX_ITEM_CARDS)
    elif kind == "table":
        columns = [
            _text(column, f"{field}.columns[{index}]", TABLE_COLUMN_MAX)
            for index, column in enumerate(
                _list(source.get("columns"), f"{field}.columns", MAX_TABLE_COLUMNS, minimum=1)
            )
        ]
        rows = []
        for row_index, row in enumerate(
            _list(source.get("rows"), f"{field}.rows", MAX_TABLE_ROWS, minimum=1)
        ):
            row_field = f"{field}.rows[{row_index}]"
            if not isinstance(row, list):
                raise _refuse(row_field, "must be a list")
            if len(row) != len(columns):
                raise _refuse(row_field, f"must have {len(columns)} cells, one per column")
            rows.append(
                [_text(cell, f"{row_field}[{index}]", TABLE_CELL_MAX) for index, cell in enumerate(row)]
            )
        block["columns"], block["rows"] = columns, rows
    elif kind == "items":
        if source.get("layout") not in LAYOUTS:
            raise _refuse(f"{field}.layout", f"must be one of {', '.join(LAYOUTS)}")
        block["layout"] = source["layout"]
        block["items"] = [
            _item(item, f"{field}.items[{index}]", call_ids)
            for index, item in enumerate(_list(source.get("items"), f"{field}.items", MAX_ITEMS))
        ]
    elif kind == "sources":
        block["sources"] = _sources(source.get("sources"), f"{field}.sources", minimum=1)
    else:  # tabs
        tabs = []
        for index, tab in enumerate(
            _list(source.get("tabs"), f"{field}.tabs", MAX_TABS, minimum=MIN_GROUP_TABS)
        ):
            tab_field = f"{field}.tabs[{index}]"
            tab_source = _object(tab, tab_field, ("title", "blocks"))
            title = _text(tab_source.get("title"), f"{tab_field}.title", TAB_TITLE_MAX)
            blocks = [
                _block(child, f"{tab_field}.blocks[{child_index}]", call_ids, ids, nested=True)
                for child_index, child in enumerate(
                    _list(tab_source.get("blocks"), f"{tab_field}.blocks", MAX_TAB_BLOCKS, minimum=1)
                )
            ]
            tabs.append({"title": title, "blocks": blocks})
        block["tabs"] = tabs
    return block


def _document(value: Any, call_ids: set[str]) -> dict[str, Any]:
    source = _object(value, "report", ("title", "date", "opening", "skill", "footnote", "tabs", "blocks"))
    if (source.get("tabs") is None) == (source.get("blocks") is None):
        raise _refuse("report", "must have exactly one of tabs or blocks")
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
    if source.get("blocks") is not None:
        ids: set[str] = set()
        document["blocks"] = [
            _block(block, f"report.blocks[{index}]", call_ids, ids, nested=False)
            for index, block in enumerate(_list(source["blocks"], "report.blocks", MAX_BLOCKS, minimum=1))
        ]
        return document
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

    source = _object(body, "body", ("report", "calls", "cards", "show_author", "listed", "notes", "origin"))
    notes = validate_notes(source.get("notes"))
    calls = _calls(source.get("calls"))
    document = _document(source.get("report"), {call["id"] for call in calls})
    cards = _cards(source.get("cards"), calls)
    flags = {}
    for name in ("show_author", "listed"):
        flags[name] = source.get(name, False)
        if not isinstance(flags[name], bool):
            raise _refuse(name, "must be true or false")
    return PublishedReport(
        document=document,
        calls=calls,
        cards=cards,
        notes=notes,
        origin=_origin(source.get("origin", ReportOrigin.ASSISTANT)),
        **flags,
    )


def _origin(value: Any) -> ReportOrigin:
    try:
        return ReportOrigin(value)
    except ValueError:
        raise _refuse("origin", "must be assistant or builder") from None


# -- the author's notes and edits (crf04/statsplus#377) -------------------------------


def notes_too_long() -> FlatBodyError:
    return FlatBodyError(400, "notes_too_long", f"Notes are at most {NOTES_MAX:,} characters.")


def notes_links() -> FlatBodyError:
    return FlatBodyError(400, "notes_links", "Notes can't contain links.")


def invalid_edits() -> FlatBodyError:
    return FlatBodyError(400, "invalid_edits", "The edits don't match this report's items and cards.")


def validate_notes(value: Any) -> str | None:
    """The author's notes as given, ``None`` for none or blank, or a refusal."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise _refuse("notes", "must be a string")
    if len(value) > NOTES_MAX:
        raise notes_too_long()
    if not value.strip():
        return None
    if _NOTES_LINK.search(value):
        raise notes_links()
    if _CONTROL.search(value):
        raise _refuse("notes", "must not contain control characters")
    return value


def _index(value: Any, size: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < size:
        raise invalid_edits()
    return value


def validate_edits(body: Any, document: dict[str, Any]) -> dict[str, Any]:
    """The normalised ``PUT /api/user/reports/<id>/edits`` body for ``document``.

    The title is checked as on publish (``invalid_input``), the notes by
    :func:`validate_notes`; ``items`` (omitted is ``[]``) names each item at
    most once, and each item's ``cards`` are distinct cards it was published
    with, in display order, or the body is ``invalid_edits``.  A block
    document's body has ``blocks`` instead of ``items``; see :func:`_block_edits`.
    """

    if "blocks" in document:
        source = _object(body, "body", ("title", "notes", "blocks"))
        title = _text(source.get("title"), "title", TITLE_MAX)
        notes = validate_notes(source.get("notes"))
        return {"title": title, "notes": notes, "blocks": _block_edits(source.get("blocks"), document)}
    source = _object(body, "body", ("title", "notes", "items"))
    title = _text(source.get("title"), "title", TITLE_MAX)
    notes = validate_notes(source.get("notes"))
    entries = source.get("items")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise invalid_edits()
    tabs = document["tabs"]
    items: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"tab", "item", "hidden", "cards"}:
            raise invalid_edits()
        tab = _index(entry["tab"], len(tabs))
        item = _index(entry["item"], len(tabs[tab]["items"]))
        if (tab, item) in seen or not isinstance(entry["hidden"], bool):
            raise invalid_edits()
        seen.add((tab, item))
        cards = entry["cards"]
        published = tabs[tab]["items"][item]["cards"]
        if (
            not isinstance(cards, list)
            or not all(isinstance(card, str) and card in published for card in cards)
            or len(set(cards)) != len(cards)
        ):
            raise invalid_edits()
        items.append({"tab": tab, "item": item, "hidden": entry["hidden"], "cards": list(cards)})
    return {"title": title, "notes": notes, "items": items}


def apply_edits(
    document: dict[str, Any], calls: list[dict[str, Any]], edits: dict[str, Any] | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The document and calls a reader sees: ``edits`` (or none) over the published ones.

    Hidden items are dropped, each overridden item shows only its listed cards
    in their order, and only the calls some shown card names are kept, in
    published order.  In a block document a hidden block is dropped (a hidden
    ``tabs`` block with its whole group) and an overridden block shows only its
    listed cards.  The inputs are not changed.
    """

    if "blocks" in document:
        if edits is not None:
            overrides = {entry["id"]: entry for entry in edits["blocks"]}
            document = {**document, "title": edits["title"], "blocks": _edited(document["blocks"], overrides)}
        shown = set(shown_cards(document))
        return document, [call for call in calls if call["id"] in shown]
    if edits is not None:
        overrides = {(entry["tab"], entry["item"]): entry for entry in edits["items"]}
        tabs = []
        for tab_index, tab in enumerate(document["tabs"]):
            items = []
            for item_index, item in enumerate(tab["items"]):
                override = overrides.get((tab_index, item_index))
                if override is None:
                    items.append(item)
                elif not override["hidden"]:
                    items.append({**item, "cards": list(override["cards"])})
            tabs.append({**tab, "items": items})
        document = {**document, "title": edits["title"], "tabs": tabs}
    shown = {card for tab in document["tabs"] for item in tab["items"] for card in item["cards"]}
    return document, [call for call in calls if call["id"] in shown]


def _block_edits(value: Any, document: dict[str, Any]) -> list[dict[str, Any]]:
    """A block document's edit entries: ``[{id, hidden, cards?}]``, omitted is ``[]``.

    ``id`` names a published block at most once; ``cards``, only on a
    ``cards``, ``spot`` or ``items`` block, lists distinct cards that block
    was published with, in display order.  Anything else is ``invalid_edits``.
    """

    if value is None:
        return []
    if not isinstance(value, list):
        raise invalid_edits()
    published = {block["id"]: block for block in walk_blocks(document)}
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, Mapping) or not {"id", "hidden"} <= set(entry) <= {"id", "hidden", "cards"}:
            raise invalid_edits()
        block_id = entry["id"]
        if not isinstance(block_id, str) or block_id not in published or block_id in seen:
            raise invalid_edits()
        if not isinstance(entry["hidden"], bool):
            raise invalid_edits()
        seen.add(block_id)
        edit: dict[str, Any] = {"id": block_id, "hidden": entry["hidden"]}
        cards = entry.get("cards")
        if cards is not None:
            block = published[block_id]
            own = block_cards(block)
            if (
                block["type"] not in CARD_BLOCKS
                or not isinstance(cards, list)
                or not all(isinstance(card, str) and card in own for card in cards)
                or len(set(cards)) != len(cards)
            ):
                raise invalid_edits()
            edit["cards"] = list(cards)
        entries.append(edit)
    return entries


def _edited(blocks: list[dict[str, Any]], overrides: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    shown = []
    for block in blocks:
        override = overrides.get(block["id"])
        if override is not None and override["hidden"]:
            continue
        if block["type"] == "tabs":
            block = {
                **block,
                "tabs": [{**tab, "blocks": _edited(tab["blocks"], overrides)} for tab in block["tabs"]],
            }
        if override is not None and "cards" in override:
            kept = override["cards"]
            if block["type"] == "items":
                block = {
                    **block,
                    "items": [
                        {**item, "cards": [card for card in kept if card in item["cards"]]}
                        for item in block["items"]
                    ],
                }
            else:
                block = {**block, "cards": list(kept)}
        shown.append(block)
    return shown


# -- reading either document shape ----------------------------------------------------


def walk_blocks(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Every block in document order, a ``tabs`` group before the blocks it holds; ``[]`` for tabs."""

    def walk(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for block in blocks:
            out.append(block)
            if block["type"] == "tabs":
                for tab in block["tabs"]:
                    out.extend(walk(tab["blocks"]))
        return out

    return walk(document.get("blocks") or [])


def block_cards(block: dict[str, Any]) -> list[str]:
    """The call ids one block shows, in display order, once each."""

    if block["type"] == "card":
        return [block["card"]]
    if block["type"] in ("cards", "spot"):
        return list(dict.fromkeys(block["cards"]))
    if block["type"] == "items":
        return list(dict.fromkeys(card for item in block["items"] for card in item["cards"]))
    return []


def shown_cards(document: dict[str, Any]) -> list[str]:
    """Every call id a card in the document names, in document order, once each."""

    if "blocks" in document:
        cards = (card for block in walk_blocks(document) for card in block_cards(block))
    else:
        cards = (card for tab in document["tabs"] for item in tab["items"] for card in item["cards"])
    return list(dict.fromkeys(cards))


def tagged(document: dict[str, Any]) -> list[tuple[str, str]]:
    """The ``(tag, title)`` of every tagged item and spot block, in document order."""

    if "blocks" not in document:
        return [
            (item["tag"], item["title"]) for tab in document["tabs"] for item in tab["items"] if "tag" in item
        ]
    entries = []
    for block in walk_blocks(document):
        if block["type"] == "spot" and "tag" in block:
            entries.append((block["tag"], block["title"]))
        elif block["type"] == "items":
            entries.extend((item["tag"], item["title"]) for item in block["items"] if "tag" in item)
    return entries


def lede(document: dict[str, Any]) -> str | None:
    """A block document's first ``lede`` paragraph, else its first paragraph, as plain text.

    The light formatting is dropped: ``**`` and ``*`` around text and a
    leading ``- `` on a line.  ``None`` for a tab document or one with no paragraph.
    """

    paragraphs = [block for block in walk_blocks(document) if block["type"] == "paragraph"]
    chosen = next((block for block in paragraphs if block.get("lede")), paragraphs[0] if paragraphs else None)
    if chosen is None:
        return None
    return _ITALIC.sub(r"\1", _BOLD.sub(r"\1", _BULLET.sub("", chosen["text"])))
