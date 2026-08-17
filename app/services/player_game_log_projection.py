"""Indexed storage for immutable player-game-log publication rows."""

from __future__ import annotations

from dataclasses import asdict
import json
from typing import Any, Iterable, Mapping

from sqlalchemy import insert

from app.models.player_game_log import PublicationPlayerGameLog


def build_player_game_log_projection(
    publication_id: str,
    payload: Any,
    *,
    season: str,
) -> tuple[dict[str, Any], ...]:
    """Validate one publication and build canonical indexed row documents."""

    from app.services.database_first_activation import (
        PublicationPayloadError,
        decode_player_game_logs,
    )

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise PublicationPayloadError(
                "player_game_logs publication payload is not valid JSON"
            ) from error
    records = decode_player_game_logs(payload, season=season)
    result = []
    identities: set[tuple[int, str]] = set()
    for record in records:
        identity = (record.player_id, record.game_id)
        if identity in identities:
            raise PublicationPayloadError(
                "player_game_logs publication contains duplicate identities"
            )
        identities.add(identity)
        row = asdict(record)
        row["game_date"] = record.game_date.isoformat()
        result.append(
            {
                "publication_id": publication_id,
                "player_id": record.player_id,
                "game_id": record.game_id,
                "game_date": record.game_date,
                "opponent_team_id": record.opponent_team_id,
                "row_payload": json.dumps(
                    row,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
    return tuple(result)


def write_player_game_log_projection(
    target: Any,
    publication_id: str,
    payload: Any,
    *,
    season: str,
) -> int:
    """Validate and insert the immutable indexed rows in the caller transaction."""

    rows = build_player_game_log_projection(
        publication_id,
        payload,
        season=season,
    )
    target.execute(insert(PublicationPlayerGameLog), rows)
    return len(rows)


def decode_player_game_log_projection(
    row_payloads: Iterable[str],
    *,
    season: str,
) -> tuple[Any, ...]:
    """Decode only the indexed rows selected for one repository query."""

    from app.services.database_first_activation import (
        PublicationPayloadError,
        decode_player_game_logs,
    )

    rows: list[Mapping[str, Any]] = []
    for row_payload in row_payloads:
        try:
            row = json.loads(row_payload)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise PublicationPayloadError(
                "player_game_logs projected row is not valid JSON"
            ) from error
        if not isinstance(row, Mapping):
            raise PublicationPayloadError(
                "player_game_logs projected row must be an object"
            )
        rows.append(row)
    if not rows:
        return ()
    return tuple(decode_player_game_logs(rows, season=season))


__all__ = [
    "build_player_game_log_projection",
    "decode_player_game_log_projection",
    "write_player_game_log_projection",
]
