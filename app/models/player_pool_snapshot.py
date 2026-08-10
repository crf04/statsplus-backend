"""Persisted, governed Player Pool snapshots."""

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models import Base


class PlayerPoolSnapshot(Base):
    """One atomic Player Pool observation for a season and exact game set."""

    __tablename__ = "player_pool_snapshots"
    __table_args__ = (
        UniqueConstraint("season", "game_ids", name="uq_player_pool_snapshot_scope"),
    )

    season: Mapped[str] = mapped_column(String(7), primary_key=True)
    game_ids: Mapped[str] = mapped_column(Text, primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


__all__ = ["PlayerPoolSnapshot"]
