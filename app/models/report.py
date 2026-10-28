"""CourtAI reports (crf04/statsplus#342).

A report is a public, unlisted dashboard an AI assistant published through the
CourtAI connector: the document (tabs, items, grades), the tool calls behind its
cards with their results, and a snapshot of each card's HTML, so an old report
keeps rendering after a card's design changes.  The public id is random base62;
the row is kept until its author deletes it or an admin takes it down, and both
are soft (``deleted_at``, ``taken_down_at``).

A report its author lists (``listed_at``) appears in the public Research feed
(crf04/statsplus#372); an admin may pin a listed one as Featured
(``featured_at``).  ``report_listings`` records every move to listed, which is
what the 5-per-24-hours listing cap counts: unlisting clears ``listed_at`` but
never a listing it already spent.
"""

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, false, text
from sqlalchemy.dialects.postgresql import JSONB

from . import Base

REPORT_ID_LENGTH = 10
REPORT_ID_MAX_LENGTH = 12
REPORT_TITLE_MAX_LENGTH = 120

_DOCUMENT_JSON = JSON().with_variant(JSONB, "postgresql")
_FEED_WHERE = "listed_at IS NOT NULL AND deleted_at IS NULL AND taken_down_at IS NULL"


class Report(Base):
    """One published report."""

    __tablename__ = "reports"

    id = Column(String(REPORT_ID_MAX_LENGTH), primary_key=True, comment="Public random base62 id")
    firebase_uid = Column(
        String(128),
        ForeignKey("users.firebase_uid", ondelete="CASCADE"),
        nullable=False,
        comment="Author account",
    )
    created_at = Column(DateTime(timezone=True), nullable=False, comment="When it was published")
    title = Column(String(REPORT_TITLE_MAX_LENGTH), nullable=False, comment="The document's title")
    document = Column(_DOCUMENT_JSON, nullable=False, comment="The validated Report document")
    calls = Column(_DOCUMENT_JSON, nullable=False, comment="The calls behind the cards, with results")
    cards = Column(_DOCUMENT_JSON, nullable=False, comment="Card name to its HTML snapshot")
    show_author = Column(
        Boolean, nullable=False, default=False, server_default=false(), comment="Author opted in"
    )
    deleted_at = Column(DateTime(timezone=True), comment="When the author deleted it")
    taken_down_at = Column(DateTime(timezone=True), comment="When an admin took it down")
    listed_at = Column(DateTime(timezone=True), comment="When its author listed it; null is unlisted")
    featured_at = Column(DateTime(timezone=True), comment="When an admin pinned it as Featured")
    covers = Column(_DOCUMENT_JSON, comment="Games and players its card calls name, set when listed")

    __table_args__ = (
        Index("idx_reports_owner_created", "firebase_uid", "created_at"),
        # The feed reads listed, visible reports newest listing first.
        Index(
            "idx_reports_feed",
            "listed_at",
            "id",
            postgresql_where=text(_FEED_WHERE),
            sqlite_where=text(_FEED_WHERE),
        ),
    )

    def __repr__(self):
        return f"<Report(id='{self.id}', firebase_uid='{self.firebase_uid}')>"


class ReportListing(Base):
    """One move of a report to listed, kept for the rolling listing cap."""

    __tablename__ = "report_listings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    firebase_uid = Column(
        String(128),
        ForeignKey("users.firebase_uid", ondelete="CASCADE"),
        nullable=False,
        comment="The account that listed it",
    )
    report_id = Column(String(REPORT_ID_MAX_LENGTH), nullable=False, comment="The report listed")
    listed_at = Column(DateTime(timezone=True), nullable=False, comment="When it was listed")

    __table_args__ = (Index("idx_report_listings_owner_listed", "firebase_uid", "listed_at"),)
