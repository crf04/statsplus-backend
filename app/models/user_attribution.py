"""First-touch acquisition source: one write-once record per account.

Created only by ``POST /api/user/attribution`` (crf04/statsplus#203).  The
primary key is the owning account, so a second record for the same account
cannot exist; the stored values are raw data, never a Referral decision.
"""

from sqlalchemy import Column, DateTime, ForeignKey, String

from . import Base

#: Longest stored value of any text field; longer submissions are truncated.
ATTRIBUTION_VALUE_MAX_LENGTH = 512


class UserAttribution(Base):
    """Where an account's visitor first landed from."""

    __tablename__ = "user_attributions"

    firebase_uid = Column(
        String(128),
        ForeignKey("users.firebase_uid", ondelete="CASCADE"),
        primary_key=True,
        comment="Owning account; one first-touch record per account",
    )
    utm_source = Column(String(ATTRIBUTION_VALUE_MAX_LENGTH), nullable=True)
    utm_medium = Column(String(ATTRIBUTION_VALUE_MAX_LENGTH), nullable=True)
    utm_campaign = Column(String(ATTRIBUTION_VALUE_MAX_LENGTH), nullable=True)
    utm_content = Column(String(ATTRIBUTION_VALUE_MAX_LENGTH), nullable=True)
    utm_term = Column(String(ATTRIBUTION_VALUE_MAX_LENGTH), nullable=True)
    ref = Column(
        String(ATTRIBUTION_VALUE_MAX_LENGTH),
        nullable=True,
        comment="Raw ref query parameter; not yet a Referral",
    )
    referrer = Column(
        String(ATTRIBUTION_VALUE_MAX_LENGTH),
        nullable=True,
        comment="External document.referrer of the first landing",
    )
    landing_path = Column(String(ATTRIBUTION_VALUE_MAX_LENGTH), nullable=True)
    landed_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Client-reported time of the first landing",
    )
    # No server default: the writing service supplies it, like sibling models.
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the server recorded the first touch",
    )

    def __repr__(self):
        return f"<UserAttribution(firebase_uid='{self.firebase_uid}')>"
