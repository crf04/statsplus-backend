"""HTTP routes for Dabble competitions and player lines."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.errors import InvalidInputError
from app.routes._service_proxy import CurrentAppService
from app.utils.auth import require_auth

dabble_bp = Blueprint("dabble", __name__)
dabble_service = CurrentAppService("dabble")


def _optional_text(name: str) -> str | None:
    value = request.args.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _positive_integer(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise InvalidInputError(f"{name} must be an integer.", detail=error) from error
    if value < 1:
        raise InvalidInputError(f"{name} must be at least 1.")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = request.args.get(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise InvalidInputError(f"{name} must be true or false.")


@dabble_bp.get("/competitions")
@require_auth
def list_competitions():
    """List active Dabble competitions."""

    return jsonify(
        dabble_service.list_competitions(
            sport=_optional_text("sport"),
            sport_id=_optional_text("sport_id"),
        )
    )


@dabble_bp.get("/lines")
@require_auth
def get_lines():
    """Fetch normalized Dabble player lines for a competition or fixture."""

    return jsonify(
        dabble_service.get_lines(
            competition=_optional_text("competition"),
            competition_id=_optional_text("competition_id"),
            fixture_id=_optional_text("fixture_id"),
            player=_optional_text("player"),
            stat=_optional_text("stat"),
            fixture_limit=_positive_integer("limit", 3),
            include_in_play=_boolean("include_in_play", False),
        )
    )


__all__ = ["dabble_bp"]
