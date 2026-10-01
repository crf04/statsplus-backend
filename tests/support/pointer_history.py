"""Read a stream's pointer history as comparable tuples."""

from __future__ import annotations

from sqlalchemy import select

from app.models.collection_control import PublicationPointerHistory


def pointer_history(engine, stream_key):
    """``(publication_id, season, fence, revoked)`` rows, oldest pointer move first."""

    table = PublicationPointerHistory.__table__
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                table.c.publication_id,
                table.c.season,
                table.c.fence,
                table.c.revoked_at,
            )
            .where(table.c.stream_key == stream_key)
            .order_by(table.c.fence)
        ).all()
    return [
        (publication_id, season, fence, revoked_at is not None)
        for publication_id, season, fence, revoked_at in rows
    ]
