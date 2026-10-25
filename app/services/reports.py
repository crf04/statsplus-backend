"""Store and serve CourtAI reports (crf04/statsplus#342).

The MCP server publishes a report for a signed-in caller; anyone with the link
reads it.  A report that is missing, deleted by its author or taken down by an
admin is the same ``resource_not_found`` to every reader, and someone else's
report is ``resource_not_found`` to a caller who tries to delete it.
"""

from __future__ import annotations

import secrets
import string
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.domain.report_document import TAGS, PublishedReport, validate_publish_body
from app.errors import ConflictError, InvalidInputError, ResourceNotFoundError
from app.models import get_session
from app.models.report import REPORT_ID_LENGTH, REPORT_ID_MAX_LENGTH, Report
from app.models.user import PUBLIC_HANDLE_MAX_LENGTH, User

#: Reports one account may hold; deleted ones do not count.
REPORT_LIMIT = 1000
REPORT_URL_PREFIX = "https://courtai.app/reports/"
DESCRIPTION_MAX = 200

_ALPHABET = string.ascii_letters + string.digits
_NOT_FOUND = "This report isn't available."
_ID_ATTEMPTS = 3


def new_report_id() -> str:
    """A random base62 id: 10 characters, about 59 bits."""

    return "".join(secrets.choice(_ALPHABET) for _ in range(REPORT_ID_LENGTH))


def _is_report_id(value: str) -> bool:
    return 0 < len(value) <= REPORT_ID_MAX_LENGTH and all(character in _ALPHABET for character in value)


def describe(document: dict[str, Any]) -> str | None:
    """The first opening paragraph, cut to 200 characters on a word boundary."""

    opening = document.get("opening") or []
    if not opening:
        return None
    text = " ".join(opening[0].split())
    if len(text) <= DESCRIPTION_MAX:
        return text
    # One character is left for the ellipsis; a word the cut splits is dropped.
    cut = text[: DESCRIPTION_MAX - 1]
    if text[DESCRIPTION_MAX - 1] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.-") + "…"


def tag_counts(document: dict[str, Any]) -> dict[str, int]:
    """How many items carry each tag, over every tab; absent tags are left out."""

    counts = Counter(item["tag"] for tab in document["tabs"] for item in tab["items"] if "tag" in item)
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
        """Validate and store a report; return its id, URL and creation time."""

        report = validate_publish_body(body)
        session = get_session(self._engine)
        try:
            held = session.scalar(
                select(func.count())
                .select_from(Report)
                .where(Report.firebase_uid == firebase_uid, Report.deleted_at.is_(None))
            )
            # Advisory under concurrency, like the Saved Filter Set cap: the
            # publish rate limit bounds how far two writes can overshoot it.
            if held >= REPORT_LIMIT:
                raise ConflictError(f"An account may hold at most {REPORT_LIMIT:,} reports.")
            created_at = self._clock()
            for attempt in range(_ID_ATTEMPTS):
                row = self._row(firebase_uid, report, created_at)
                session.add(row)
                try:
                    session.commit()
                    break
                except IntegrityError:
                    # An id collision; anything else (no user row) is a real failure.
                    session.rollback()
                    if attempt == _ID_ATTEMPTS - 1 or session.get(Report, row.id) is None:
                        raise
            return {
                "id": row.id,
                "url": f"{REPORT_URL_PREFIX}{row.id}",
                "created_at": created_at.isoformat(),
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

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

    def set_public_handle(self, firebase_uid: str, handle: Any) -> str | None:
        """Set (or, with ``None``, clear) an account's public author label."""

        if handle is not None:
            if not isinstance(handle, str) or not handle.strip():
                raise InvalidInputError("public_handle must be a non-empty string or null.")
            handle = handle.strip()
            if len(handle) > PUBLIC_HANDLE_MAX_LENGTH or not handle.isprintable():
                raise InvalidInputError(
                    f"public_handle must be at most {PUBLIC_HANDLE_MAX_LENGTH} printable characters."
                )
        session = get_session(self._engine)
        try:
            user = session.get(User, firebase_uid)
            if user is None:
                raise ResourceNotFoundError("No such user.")
            user.public_handle = handle
            session.commit()
            return handle
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- reads -------------------------------------------------------------------

    def _visible(self, report_id: str) -> tuple[Report, str | None]:
        if not _is_report_id(report_id):
            raise ResourceNotFoundError(_NOT_FOUND)
        session = get_session(self._engine)
        try:
            found = session.execute(
                select(Report, User.public_handle)
                .join(User, User.firebase_uid == Report.firebase_uid)
                .where(Report.id == report_id, Report.deleted_at.is_(None), Report.taken_down_at.is_(None))
            ).first()
            if found is None:
                raise ResourceNotFoundError(_NOT_FOUND)
            session.expunge(found[0])
            return found[0], found[1]
        finally:
            session.close()

    def read(self, report_id: str, *, viewer_uid: str | None) -> dict[str, Any]:
        """The public report, with ``is_author`` for the signed-in author."""

        row, handle = self._visible(report_id)
        return {
            "id": row.id,
            "created_at": row.created_at.isoformat(),
            "author": {"label": handle} if row.show_author and handle else None,
            "is_author": viewer_uid is not None and viewer_uid == row.firebase_uid,
            "document": row.document,
            "calls": [
                {key: call.get(key) for key in ("id", "tool", "card", "structured", "meta")}
                for call in row.calls
            ],
            "cards": row.cards,
        }

    def document(self, report_id: str) -> dict[str, Any]:
        """The stored document of a visible report."""

        return self._visible(report_id)[0].document

    def meta(self, report_id: str) -> dict[str, Any]:
        """The small link-preview summary of a visible report."""

        row, _ = self._visible(report_id)
        document = row.document
        return {
            "id": row.id,
            "title": document["title"],
            "date": document.get("date"),
            "description": describe(document),
            "tags": tag_counts(document),
        }
