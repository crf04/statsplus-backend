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


def cite_ledger_source(engine, publication_id, *, game_id, observation_id, current=True):
    """Record that a publication cites ``observation_id`` for ``game_id``.

    ``current`` makes it the canonical ledger's current source for the game;
    otherwise the ledger names a different (corrected) source.
    """

    import json
    from datetime import datetime, timezone

    from sqlalchemy import text

    stamp = datetime(2026, 1, 15, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO collection_observations (observation_id, client_observation_id, "
            "collector_id, manifest_id, environment, provider, observation_type, scope, "
            "season, cutoff, schema_version, checksum, payload, payload_bytes, "
            "retrieved_at, accepted_at) VALUES (:id, :id, 'test', 'm', 'testing', 'pbp', "
            "'canonical_game_ledger', :scope, '2025-26', :stamp, 1, :checksum, '{}', 2, "
            ":stamp, :stamp)"
        ), {"id": observation_id, "scope": json.dumps({"game_id": game_id}),
            "stamp": stamp, "checksum": observation_id.ljust(64, "0")[:64]})
        connection.execute(text(
            "INSERT INTO publication_observations (publication_id, observation_id, role, "
            "created_at) VALUES (:pub, :id, 'completeness_evidence', :stamp)"
        ), {"pub": publication_id, "id": observation_id, "stamp": stamp})
        current_source = observation_id if current else f"{observation_id}-corrected"
        exists = connection.execute(text(
            "SELECT 1 FROM canonical_game_ledger_games WHERE game_id = :g"
        ), {"g": game_id}).first()
        if exists is None:
            connection.execute(text(
                "INSERT INTO canonical_game_ledger_games (game_id, season, season_type, "
                "game_date, home_team_id, home_team_tricode, away_team_id, away_team_tricode, "
                "status, source_observation_id, checksum, raw_checksum, retrieved_at, updated_at) "
                "VALUES (:g, '2025-26', 'Regular Season', '2026-01-02', 1, 'ATL', 2, 'BOS', "
                "'final', :src, :c, :c, :stamp, :stamp)"
            ), {"g": game_id, "src": current_source, "c": "f" * 64, "stamp": stamp})
        else:
            connection.execute(text(
                "UPDATE canonical_game_ledger_games SET source_observation_id = :src "
                "WHERE game_id = :g"
            ), {"src": current_source, "g": game_id})
