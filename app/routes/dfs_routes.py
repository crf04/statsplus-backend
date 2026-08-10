"""HTTP adapter for the authenticated DFS Board.

There is one route, and it decides nothing.  Retrieval, comparison, filtering,
size limits, serialization, entity tags, and the conditional outcome all belong
to the board services below it; this module authenticates the caller, converts
the query string into a typed board read, and turns the representation it is
handed into a private, revalidatable HTTP response.

No provider, Redis, or database client is created here, at import or at
request time: the route reads the one graph the application factory assembled.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from app.dependencies import get_dependencies
from app.errors import route_error_boundary
from app.services.dfs_board_query import parse_board_request
from app.utils.auth import require_auth

dfs_bp = Blueprint("dfs", __name__)

#: An authenticated board belongs to its caller alone.  A shared cache must
#: never keep it, and a private cache must revalidate it before reusing it.
BOARD_CACHE_CONTROL = "private, no-cache, max-age=0, must-revalidate"


@dfs_bp.route("/board", methods=["GET"])
@require_auth
@route_error_boundary("The DFS board could not be assembled.")
def dfs_board():
    """Return the central factual DFS Board for the authenticated caller."""

    dependencies = get_dependencies()
    board_request = parse_board_request(
        request.args, settings=dependencies.settings
    )
    representation = dependencies.dfs_board_response_service.respond(
        board_request,
        if_none_match=request.headers.get("If-None-Match"),
    )
    return _board_response(representation)


def _board_response(representation):
    """One board representation as a private, conditional HTTP response."""

    if representation.is_not_modified:
        response = current_app.response_class(status=304)
    else:
        response = jsonify(representation.payload)

    # The tag is weak because it identifies the board's stated facts rather
    # than the exact bytes of one reading of them.
    response.set_etag(representation.etag, weak=True)
    response.headers["Cache-Control"] = BOARD_CACHE_CONTROL
    response.headers["Vary"] = "Authorization"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


__all__ = ["dfs_bp"]
