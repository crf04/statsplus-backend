"""Store and serve CourtAI reports (crf04/statsplus#342) and the Research feed (#372).

The MCP server publishes a report for a signed-in caller; anyone with the link
reads it.  A missing report is ``resource_not_found``; one its author deleted
or an admin took down is ``410 removed`` on the report read and
``resource_not_found`` everywhere else, and someone else's report is
``resource_not_found`` to a caller who tries to change or delete it.

An author with a public handle may list a report in the public Research feed,
on publish or later, at most :data:`LISTING_LIMIT` moves to listed per rolling
:data:`LISTING_WINDOW`; an admin may pin listed reports as Featured.

The author edits a report by saving an edit body (crf04/statsplus#377) that
replaces any earlier one.  Every read applies it: readers never receive a
hidden item, a removed card or a call no shown card names, and the author also
receives the published original and the stored edits.  An edit never changes
``listed_at``; a listed report's feed title and ``covers`` follow it.
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets
import string
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.domain.nba_teams import (
    NBA_TEAM_ABBREVIATION_DIALECTS,
    NBA_TEAM_FULL_NAME_TO_TRICODE,
    NBA_TEAM_TRICODES,
)
from app.domain.report_document import (
    TAGS,
    PublishedReport,
    apply_edits,
    lede,
    tagged,
    validate_edits,
    validate_publish_body,
)
from app.errors import ConflictError, FlatBodyError, InvalidInputError, ResourceNotFoundError
from app.models import get_session
from app.models.report import REPORT_ID_LENGTH, REPORT_ID_MAX_LENGTH, Report, ReportListing
from app.models.user import User, normalize_public_handle

#: Reports one account may hold; deleted ones do not count.
REPORT_LIMIT = 1000
REPORT_URL_PREFIX = "https://courtai.app/reports/"
DESCRIPTION_MAX = 200

#: Moves to listed one account may make per rolling window, over both endpoints.
LISTING_LIMIT = 5
LISTING_WINDOW = timedelta(hours=24)
#: Entries per Research feed or author page.
FEED_PAGE_SIZE = 20

_ALPHABET = string.ascii_letters + string.digits
_NOT_FOUND = "This report isn't available."
_ID_ATTEMPTS = 3


def handle_required() -> FlatBodyError:
    return FlatBodyError(
        409, "handle_required", "Choose a public handle before listing a report in Research."
    )


def listed_limit() -> FlatBodyError:
    return FlatBodyError(
        429,
        "listed_limit",
        f"An account may list at most {LISTING_LIMIT} reports in 24 hours. Try again later.",
    )


def invalid_handle() -> FlatBodyError:
    return FlatBodyError(400, "invalid_handle", "A handle is 3-20 characters of a-z, 0-9 and _.")


def handle_taken() -> FlatBodyError:
    return FlatBodyError(409, "handle_taken", "That handle is taken.")


def handle_already_set() -> FlatBodyError:
    return FlatBodyError(
        409, "handle_already_set", "Your handle is already set. Ask CourtAI support to change it."
    )


# -- covers ------------------------------------------------------------------------

#: Every spelling of a team a ``game`` argument may use, lowercased: tricodes,
#: the reviewed dialects, full names and nicknames ("Pistons", "Trail Blazers").
_TEAM_NAMES = {
    **{tricode.lower(): tricode for tricode in NBA_TEAM_TRICODES},
    **{dialect.lower(): tricode for dialect, tricode in NBA_TEAM_ABBREVIATION_DIALECTS.items()},
    **{name.lower(): tricode for name, tricode in NBA_TEAM_FULL_NAME_TO_TRICODE.items()},
    **{name.rsplit(" ", 1)[1].lower(): tricode for name, tricode in NBA_TEAM_FULL_NAME_TO_TRICODE.items()},
    "trail blazers": "POR",
}
_GAME = re.compile(r"(.+?)\s*@\s*(.+)|(.+?)\s+at\s+(.+)", re.IGNORECASE)
#: The ``Name (TEAM)`` form that picks between namesakes.
_NAMESAKE_TEAM = re.compile(r"\s*\([A-Za-z]{2,3}\)$")


def _game_tricodes(value: Any) -> str | None:
    """``'DET @ CHA'`` for a game named by its teams in any spelling, else ``None``."""

    if not isinstance(value, str):
        return None
    match = _GAME.fullmatch(" ".join(value.split()))
    if match is None:
        return None
    away, home = (part for part in match.groups() if part is not None)
    teams = [_TEAM_NAMES.get(away.lower()), _TEAM_NAMES.get(home.lower())]
    return f"{teams[0]} @ {teams[1]}" if all(teams) and teams[0] != teams[1] else None


def _player_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = _NAMESAKE_TEAM.sub("", " ".join(value.split()))
    return name or None


def covers(calls: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """The games and players a report's card calls name, in call order, once each.

    A game is its ``game`` argument as tricodes (``DET @ CHA``); a player is
    the ``player`` argument without a ``(TEAM)`` suffix.  A call naming
    neither, or a game no team table resolves, adds nothing.
    """

    games: dict[str, None] = {}
    players: dict[str, None] = {}
    for call in calls:
        args = call.get("args") or {}
        game, player = _game_tricodes(args.get("game")), _player_name(args.get("player"))
        if game:
            games[game] = None
        if player:
            players[player] = None
    return {"games": list(games), "players": list(players)}


def _shown(row: Report) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The document and calls readers see: the stored edits over the published ones."""

    return apply_edits(row.document, row.calls, row.edits)


def _public_calls(calls: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The calls as a read returns them, without their ``args``."""

    return [{key: call.get(key) for key in ("id", "tool", "card", "structured", "meta")} for call in calls]


def _utc(value: datetime) -> datetime:
    """SQLite reads a timestamp back naive; every stored time is UTC."""

    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _encode_cursor(listed_at: datetime, report_id: str) -> str:
    raw = f"{_utc(listed_at).isoformat()}|{report_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        listed_at, report_id = raw.split("|")
        moment = datetime.fromisoformat(listed_at)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise InvalidInputError("cursor is not a Research page cursor.") from None
    if moment.tzinfo is None or not _is_report_id(report_id):
        raise InvalidInputError("cursor is not a Research page cursor.")
    return moment.astimezone(UTC), report_id


def new_report_id() -> str:
    """A random base62 id: 10 characters, about 59 bits."""

    return "".join(secrets.choice(_ALPHABET) for _ in range(REPORT_ID_LENGTH))


def _is_report_id(value: str) -> bool:
    return 0 < len(value) <= REPORT_ID_MAX_LENGTH and all(character in _ALPHABET for character in value)


def describe(document: dict[str, Any]) -> str | None:
    """The first opening paragraph, cut to 200 characters on a word boundary.

    A block document without an opening uses its lede paragraph, else its
    first paragraph, without the light formatting.
    """

    opening = document.get("opening") or []
    source = opening[0] if opening else lede(document)
    if source is None:
        return None
    text = " ".join(source.split())
    if len(text) <= DESCRIPTION_MAX:
        return text
    # One character is left for the ellipsis; a word the cut splits is dropped.
    cut = text[: DESCRIPTION_MAX - 1]
    if text[DESCRIPTION_MAX - 1] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.-") + "…"


def tag_counts(document: dict[str, Any]) -> dict[str, int]:
    """How many items (and spot blocks) carry each tag, over the whole document; absent tags are left out."""

    counts = Counter(tag for tag, _ in tagged(document))
    return {tag: counts[tag] for tag in TAGS if counts[tag]}


class ReportService:
    """The reports table's only reader and writer."""

    def __init__(
        self,
        engine: Any,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = new_report_id,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._new_id = new_id

    # -- writes ------------------------------------------------------------------

    def publish(self, firebase_uid: str, body: Any) -> dict[str, Any]:
        """Validate and store a report; return its id, URL, creation time and origin."""

        report = validate_publish_body(body)
        session = get_session(self._engine)
        try:
            for attempt in range(_ID_ATTEMPTS):
                row = self._insert_under_cap(session, firebase_uid, report)
                # Read before the commit expires them: SQLite reloads a naive time.
                report_id, created_at = row.id, row.created_at
                try:
                    session.commit()
                    break
                except IntegrityError:
                    # An id collision; anything else (no user row) is a real failure.
                    # The rollback released the account lock, so the retry takes it
                    # and counts again.
                    session.rollback()
                    if attempt == _ID_ATTEMPTS - 1 or session.get(Report, report_id) is None:
                        raise
            return {
                "id": report_id,
                "url": f"{REPORT_URL_PREFIX}{report_id}",
                "created_at": created_at.isoformat(),
                "origin": report.origin,
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _insert_under_cap(self, session: Any, firebase_uid: str, report: PublishedReport) -> Report:
        """Add the report in the open transaction, refusing an account at the cap.

        The account's ``users`` row is locked first (``FOR UPDATE`` on
        PostgreSQL; SQLite serializes writers itself), so concurrent publishes
        and listings by one account count and insert one at a time and cannot
        overshoot either cap.
        """

        handle = self._lock_account(session, firebase_uid)
        held = session.scalar(
            select(func.count())
            .select_from(Report)
            .where(Report.firebase_uid == firebase_uid, Report.deleted_at.is_(None))
        )
        if held >= REPORT_LIMIT:
            raise ConflictError(f"An account may hold at most {REPORT_LIMIT:,} reports.")
        row = self._row(firebase_uid, report, self._clock())
        if report.listed:
            self._spend_listing(session, firebase_uid, handle, row.id, row.created_at)
            row.listed_at = row.created_at
            row.covers = covers(_shown(row)[1])
        session.add(row)
        return row

    @staticmethod
    def _lock_account(session: Any, firebase_uid: str) -> str | None:
        """Lock the account's row for this transaction; return its handle."""

        return session.scalar(
            select(User.public_handle).where(User.firebase_uid == firebase_uid).with_for_update()
        )

    def _spend_listing(
        self, session: Any, firebase_uid: str, handle: str | None, report_id: str, now: datetime
    ) -> None:
        """Record one move to listed, or refuse it: no handle, or the cap is spent.

        The caller holds the account lock, so the count and the insert are one step.
        """

        if handle is None:
            raise handle_required()
        spent = session.scalar(
            select(func.count())
            .select_from(ReportListing)
            .where(ReportListing.firebase_uid == firebase_uid, ReportListing.listed_at > now - LISTING_WINDOW)
        )
        if spent >= LISTING_LIMIT:
            raise listed_limit()
        session.add(ReportListing(firebase_uid=firebase_uid, report_id=report_id, listed_at=now))

    def _row(self, firebase_uid: str, report: PublishedReport, created_at: datetime) -> Report:
        return Report(
            id=self._new_id(),
            firebase_uid=firebase_uid,
            created_at=created_at,
            title=report.document["title"],
            document=report.document,
            calls=report.calls,
            cards=report.cards,
            show_author=report.show_author,
            notes=report.notes,
            origin=report.origin,
        )

    def delete(self, firebase_uid: str, report_id: str) -> None:
        """Soft-delete the caller's own report; any other id is not found."""

        self._stamp(report_id, "deleted_at", owner=firebase_uid)

    def take_down(self, report_id: str) -> dict[str, Any]:
        """An admin hides a report for everyone; idempotent, keeping the first time."""

        taken_down_at = self._stamp(report_id, "taken_down_at")
        return {"id": report_id, "taken_down_at": taken_down_at.isoformat()}

    def _stamp(self, report_id: str, column: str, *, owner: str | None = None) -> datetime:
        if not _is_report_id(report_id):
            raise ResourceNotFoundError(_NOT_FOUND)
        session = get_session(self._engine)
        try:
            row = session.get(Report, report_id)
            if row is None or (owner is not None and (row.firebase_uid != owner or row.deleted_at)):
                raise ResourceNotFoundError(_NOT_FOUND)
            if getattr(row, column) is None:
                setattr(row, column, self._clock())
                session.commit()
            return getattr(row, column)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def set_listed(self, firebase_uid: str, report_id: str, listed: bool) -> dict[str, Any]:
        """List or unlist the caller's own visible report.

        Listing an unlisted report needs a handle and spends one of the
        account's listings; listing a listed one changes nothing.  Unlisting
        is always allowed and also drops Featured.
        """

        if not _is_report_id(report_id):
            raise ResourceNotFoundError(_NOT_FOUND)
        session = get_session(self._engine)
        try:
            handle = self._lock_account(session, firebase_uid)
            row = session.get(Report, report_id)
            if (
                row is None
                or row.firebase_uid != firebase_uid
                or row.deleted_at is not None
                or row.taken_down_at is not None
            ):
                raise ResourceNotFoundError(_NOT_FOUND)
            if listed and row.listed_at is None:
                now = self._clock()
                self._spend_listing(session, firebase_uid, handle, row.id, now)
                row.listed_at = now
                row.covers = covers(_shown(row)[1])
            elif not listed:
                row.listed_at = None
                row.featured_at = None
            listed_at = row.listed_at
            session.commit()
            return {
                "id": report_id,
                "listed": listed_at is not None,
                "listed_at": _utc(listed_at).isoformat() if listed_at else None,
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def save_edits(self, firebase_uid: str, report_id: str, body: Any) -> dict[str, Any]:
        """Replace the caller's edits of their own visible report; return it as the author reads it.

        Sets ``edited_at`` and leaves ``listed_at`` alone.  The account lock
        is taken first, as listing takes it, so a listing never computes its
        ``covers`` from edits being replaced.
        """

        if not _is_report_id(report_id):
            raise ResourceNotFoundError(_NOT_FOUND)
        session = get_session(self._engine)
        try:
            self._lock_account(session, firebase_uid)
            row = session.get(Report, report_id)
            if (
                row is None
                or row.firebase_uid != firebase_uid
                or row.deleted_at is not None
                or row.taken_down_at is not None
            ):
                raise ResourceNotFoundError(_NOT_FOUND)
            edits = validate_edits(body, row.document)
            row.edits = edits
            row.notes = edits["notes"]
            row.title = edits["title"]
            row.edited_at = self._clock()
            if row.listed_at is not None:
                row.covers = covers(_shown(row)[1])
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        return self.read(report_id, viewer_uid=firebase_uid)

    def set_featured(self, report_id: str, featured: bool) -> dict[str, Any]:
        """An admin pins (or unpins) a listed, visible report as Featured.

        The author's account lock is taken first, as listing and unlisting
        take it, and the report read again under it, so a pin cannot land on
        a report its author unlisted meanwhile.
        """

        if not _is_report_id(report_id):
            raise ResourceNotFoundError(_NOT_FOUND)
        session = get_session(self._engine)
        try:
            owner = session.scalar(select(Report.firebase_uid).where(Report.id == report_id))
            if owner is None:
                raise ResourceNotFoundError(_NOT_FOUND)
            self._lock_account(session, owner)
            row = session.get(Report, report_id, populate_existing=True)
            if row is None or row.deleted_at is not None or row.taken_down_at is not None:
                raise ResourceNotFoundError(_NOT_FOUND)
            if featured and row.listed_at is None:
                raise ConflictError("Only a listed report can be Featured.")
            if featured and row.featured_at is None:
                row.featured_at = self._clock()
            elif not featured:
                row.featured_at = None
            featured_at = row.featured_at
            session.commit()
            return {
                "id": report_id,
                "featured": featured_at is not None,
                "featured_at": _utc(featured_at).isoformat() if featured_at else None,
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def claim_public_handle(self, firebase_uid: str, handle: Any) -> str:
        """The caller chooses a handle, once; an admin changes it after that."""

        if handle is None:
            raise invalid_handle()
        return self._write_handle(firebase_uid, handle, once=True)

    def set_public_handle(self, firebase_uid: str, handle: Any) -> str | None:
        """An admin sets, changes or (with ``None``) clears an account's handle.

        Clearing it also unlists and unfeatures the account's reports.
        """

        return self._write_handle(firebase_uid, handle, once=False)

    def _write_handle(self, firebase_uid: str, value: Any, *, once: bool) -> Any:
        handle = None if value is None else normalize_public_handle(value)
        if value is not None and handle is None:
            raise invalid_handle()
        session = get_session(self._engine)
        try:
            user = session.get(User, firebase_uid)
            if user is None:
                raise ResourceNotFoundError("No such user.")
            if once and user.public_handle is not None:
                raise handle_already_set()
            if handle is not None:
                owner = session.scalar(
                    select(User.firebase_uid).where(
                        func.lower(User.public_handle) == handle, User.firebase_uid != firebase_uid
                    )
                )
                if owner is not None:
                    raise handle_taken()
            change = update(User).where(User.firebase_uid == firebase_uid).values(public_handle=handle)
            if once:
                # Conditional, so of two concurrent first claims only one sets it.
                change = change.where(User.public_handle.is_(None))
            try:
                updated = session.execute(change).rowcount
                if updated and handle is None:
                    # A listed report always names its author, so with no
                    # handle left nothing of theirs stays listed or Featured.
                    session.execute(
                        update(Report)
                        .where(Report.firebase_uid == firebase_uid, Report.listed_at.is_not(None))
                        .values(listed_at=None, featured_at=None)
                    )
                session.commit()
            except IntegrityError:
                # Another account took it after the check: the unique index
                # refuses it on the update or the commit.
                raise handle_taken() from None
            if not updated:
                raise handle_already_set()
            return handle
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- reads -------------------------------------------------------------------

    def _visible(self, report_id: str, *, removed_is_gone: bool = False) -> tuple[Report, str | None]:
        """The visible report and its author's handle.

        A deleted or taken-down report is ``resource_not_found``, or with
        ``removed_is_gone`` the ``410 removed`` naming who removed it (a
        takedown wins over the author's delete).
        """

        if not _is_report_id(report_id):
            raise ResourceNotFoundError(_NOT_FOUND)
        session = get_session(self._engine)
        try:
            found = session.execute(
                select(Report, User.public_handle)
                .join(User, User.firebase_uid == Report.firebase_uid)
                .where(Report.id == report_id)
            ).first()
            if found is None:
                raise ResourceNotFoundError(_NOT_FOUND)
            row, handle = found
            if row.taken_down_at is not None or row.deleted_at is not None:
                if not removed_is_gone:
                    raise ResourceNotFoundError(_NOT_FOUND)
                if row.taken_down_at is not None:
                    raise FlatBodyError(410, "removed", "This report was taken down.", by="takedown")
                raise FlatBodyError(410, "removed", "This report was removed by its author.", by="author")
            session.expunge(row)
            return row, handle
        finally:
            session.close()

    def read(self, report_id: str, *, viewer_uid: str | None) -> dict[str, Any]:
        """The report with its edits applied, with ``is_author`` for the signed-in author.

        A listed report always names its author, with the handle; an unlisted
        one only when it opted in with ``show_author``.  Readers get only the
        card HTML the shown calls draw; the author gets every card's, the
        published ``original`` (its calls with their ``args``, exactly as
        stored) and the stored ``edits``, so a removal can be undone.
        """

        row, handle = self._visible(report_id, removed_is_gone=True)
        listed = row.listed_at is not None
        if handle and listed:
            author: dict[str, str] | None = {"label": f"@{handle}", "handle": handle}
        elif handle and row.show_author:
            author = {"label": f"@{handle}"}
        else:
            author = None
        is_author = viewer_uid is not None and viewer_uid == row.firebase_uid
        document, calls = _shown(row)
        drawn = {call["card"] for call in calls}
        report = {
            "id": row.id,
            "created_at": row.created_at.isoformat(),
            "author": author,
            "is_author": is_author,
            "listed": listed,
            "featured": listed and row.featured_at is not None,
            "document": document,
            "calls": _public_calls(calls),
            "cards": row.cards
            if is_author
            else {name: html for name, html in row.cards.items() if name in drawn},
            "notes": row.notes,
            "edited_at": _utc(row.edited_at).isoformat() if row.edited_at else None,
            "origin": row.origin,
        }
        if is_author:
            report["original"] = {"document": row.document, "calls": row.calls}
            report["edits"] = row.edits
        return report

    def document(self, report_id: str) -> dict[str, Any]:
        """The document readers see of a visible report, its edits applied."""

        return _shown(self._visible(report_id)[0])[0]

    def meta(self, report_id: str) -> dict[str, Any]:
        """The small link-preview summary of a visible report."""

        row, _ = self._visible(report_id)
        document = _shown(row)[0]
        return {
            "id": row.id,
            "title": document["title"],
            "date": document.get("date"),
            "description": describe(document),
            "tags": tag_counts(document),
            "origin": row.origin,
        }

    def feed(self, cursor: str | None = None) -> dict[str, Any]:
        """One page of the public Research feed, newest listing first.

        The first page also carries every Featured report, newest pin first;
        they are left out of ``reports`` on every page, so none shows twice.
        """

        session = self._snapshot_session()
        try:
            featured = (
                self._entries(
                    session,
                    [Report.featured_at.is_not(None)],
                    order=(Report.featured_at.desc(), Report.id.desc()),
                )
                if cursor is None
                else []
            )
            # Also excluded by id, so a report unpinned between the two reads
            # cannot show twice where there is no snapshot (SQLite).
            shown = [entry["id"] for entry in featured]
            page = self._page(session, [Report.featured_at.is_(None), Report.id.not_in(shown)], cursor)
            page["featured"] = featured
            return page
        finally:
            session.close()

    def _snapshot_session(self) -> Any:
        """A session whose reads all see one snapshot.

        On PostgreSQL that is ``REPEATABLE READ``: under the default ``READ
        COMMITTED`` a pin or unpin committed between the two reads would drop
        the report from both lists or show it in both.  SQLite gets no
        snapshot; :meth:`feed` still never shows a report twice there.
        """

        session = get_session(self._engine)
        if self._engine.dialect.name == "postgresql":
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        return session

    def author_feed(self, handle: str, cursor: str | None = None) -> dict[str, Any]:
        """One page of an author's listed reports; an unknown handle is not found."""

        normalized = normalize_public_handle(handle)
        session = get_session(self._engine)
        try:
            firebase_uid = (
                session.scalar(select(User.firebase_uid).where(User.public_handle == normalized))
                if normalized
                else None
            )
            if firebase_uid is None:
                raise ResourceNotFoundError("No author has that handle.")
            return self._page(session, [Report.firebase_uid == firebase_uid], cursor)
        finally:
            session.close()

    def _page(self, session: Any, filters: list[Any], cursor: str | None) -> dict[str, Any]:
        if cursor is not None:
            listed_at, report_id = _decode_cursor(cursor)
            filters = [
                *filters,
                or_(
                    Report.listed_at < listed_at,
                    and_(Report.listed_at == listed_at, Report.id < report_id),
                ),
            ]
        rows = self._entries(
            session, filters, order=(Report.listed_at.desc(), Report.id.desc()), limit=FEED_PAGE_SIZE + 1
        )
        more = len(rows) > FEED_PAGE_SIZE
        rows = rows[:FEED_PAGE_SIZE]
        last = rows[-1] if more else None
        return {
            "featured": [],
            "reports": rows,
            "next_cursor": _encode_cursor(datetime.fromisoformat(last["listed_at"]), last["id"])
            if last
            else None,
        }

    def _entries(
        self, session: Any, filters: list[Any], *, order: tuple[Any, ...], limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Listed, visible reports by authors with a handle, as feed entries.

        Reads only the columns an entry shows, never the calls or card HTML.
        """

        rows = session.execute(
            select(
                Report.id,
                Report.title,
                Report.listed_at,
                Report.created_at,
                Report.document["date"].as_string(),
                Report.covers,
                Report.origin,
                User.public_handle,
            )
            .join(User, User.firebase_uid == Report.firebase_uid)
            .where(
                Report.listed_at.is_not(None),
                Report.deleted_at.is_(None),
                Report.taken_down_at.is_(None),
                User.public_handle.is_not(None),
                *filters,
            )
            .order_by(*order)
            .limit(limit)
        ).all()
        return [
            {
                "id": report_id,
                "title": title,
                "listed_at": _utc(listed_at).isoformat(),
                "created_at": _utc(created_at).isoformat(),
                "date": date,
                "author": {"handle": handle},
                "covers": entry_covers or {"games": [], "players": []},
                "preview_url": f"/api/reports/{report_id}/preview.png",
                "origin": origin,
            }
            for report_id, title, listed_at, created_at, date, entry_covers, origin, handle in rows
        ]
