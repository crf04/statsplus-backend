"""CourtAI reports (crf04/statsplus#342).

A report is a public, unlisted dashboard an AI assistant published through the
CourtAI connector: the document (tabs, items, grades), the tool calls behind its
cards with their results, and a snapshot of each card's HTML, so an old report
keeps rendering after a card's design changes.  The public id is random base62;
the row is kept until its author deletes it or an admin takes it down, and both
are soft (``deleted_at``, ``taken_down_at``).
"""

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Index, String, false
from sqlalchemy.dialects.postgresql import JSONB

from . import Base

REPORT_ID_LENGTH = 10
REPORT_ID_MAX_LENGTH = 12
REPORT_TITLE_MAX_LENGTH = 120

_DOCUMENT_JSON = JSON().with_variant(JSONB, "postgresql")


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

    __table_args__ = (Index("idx_reports_owner_created", "firebase_uid", "created_at"),)

    def __repr__(self):
        return f"<Report(id='{self.id}', firebase_uid='{self.firebase_uid}')>"
