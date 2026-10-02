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


def revoked_fences(engine, stream_key):
    """The ``revoked_fence`` of each revoked row, oldest pointer move first."""

    table = PublicationPointerHistory.__table__
    with engine.connect() as connection:
        return [
            row.revoked_fence
            for row in connection.execute(
                select(table.c.revoked_fence)
                .where(table.c.stream_key == stream_key, table.c.revoked_at.is_not(None))
                .order_by(table.c.fence)
            )
        ]


def retirements(engine, stream_key):
    """``(publication_id, retired_by)`` of each retired row, oldest pointer move first."""

    table = PublicationPointerHistory.__table__
    with engine.connect() as connection:
        return [
            (row.publication_id, row.retired_by)
            for row in connection.execute(
                select(table.c.publication_id, table.c.retired_by)
                .where(table.c.stream_key == stream_key, table.c.retired_at.is_not(None))
                .order_by(table.c.fence)
            )
        ]
