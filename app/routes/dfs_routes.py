"""HTTP adapter for the authenticated DFS Board.

There is one route, and it decides nothing.  Publication, parsing, retrieval,
comparison, filtering, size limits, serialization, entity tags, and the
conditional outcome belong to the board response service below it; this module
authenticates the caller, hands that service the raw query string, and turns
the representation it is handed into a private, revalidatable HTTP response.

Two things happen here because only here can they.  Authentication is above the
service, so an unauthenticated request is a 401 that reaches no board and is
recorded as no board request at all.  And the single telemetry event for a
board request is finalized here, from the response's own status, because the
status a caller receives is decided by this layer and the central error
handlers -- not by what the service intended to publish.

No provider, Redis, or database client is created here, at import or at
request time: the route reads the one graph the application factory assembled.
"""

from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from app.dependencies import get_dependencies
from app.errors import route_error_boundary
from app.services.dfs_board_response import PendingBoardObservation
from app.utils.auth import require_auth

dfs_bp = Blueprint("dfs", __name__)

#: An authenticated board belongs to its caller alone.  A shared cache must
#: never keep it, and a private cache must revalidate it before reusing it.
BOARD_CACHE_CONTROL = "private, no-cache, max-age=0, must-revalidate"


#: Where one request's own open observation lives while it is being decided.
_OBSERVATION = "dfs_board_observation"


@dfs_bp.route("/board", methods=["GET"])
@require_auth
@route_error_boundary("The DFS board could not be assembled.")
def dfs_board():
    """Return the central factual DFS Board for the authenticated caller."""

    # Opened before the dependency graph is read, so a request that fails
    # before it reaches a board is still one request that happened.  It is
    # request-scoped state, not the service's, which is why it lives in ``g``
    # and is handed to the service rather than reached for inside it.
    observation = PendingBoardObservation()
    setattr(g, _OBSERVATION, observation)

    representation = get_dependencies().dfs_board_response_service.respond_to_query(
        request.args,
        if_none_match=request.headers.get("If-None-Match"),
        observation=observation,
    )
    return _board_response(representation)


@dfs_bp.after_request
def _private_board_response(response):
    """Make every board response one caller's alone, whatever its status.

    A 400, a 404, a 503, and a centrally handled 500 are as specific to the
    authenticated caller as the board is, and each of them is produced by a
    different layer -- the parser, the publication gate, the error handlers.
    Stating the policy once, at the blueprint that owns this route, is the only
    way none of them can be published without it, and keeps every other
    blueprint's caching untouched.

    An entity tag is deliberately not set here: it identifies one board, and a
    failure is not a board a caller could revalidate.

    This is also the one place that sees the status the caller will actually
    receive -- the parser's 400, the gate's 404, the outage's 503, a centrally
    handled 500 from a dependency that never resolved or a serialization that
    raised, the 304, and the 200 -- so it is where the request's single event
    is finalized.  A request that reached no observation, an unauthenticated
    one, records nothing.
    """

    response.headers["Cache-Control"] = BOARD_CACHE_CONTROL
    # Added rather than assigned, so a Vary another layer states -- CORS naming
    # Origin, say -- is preserved beside this one.
    response.vary.add("Authorization")
    response.headers["X-Content-Type-Options"] = "nosniff"

    observation = g.pop(_OBSERVATION, None)
    if observation is not None:
        observation.finalize(response.status_code)
    return response


def _board_response(representation):
    """One board representation as a conditional HTTP response."""

    if representation.is_not_modified:
        response = current_app.response_class(status=304)
    else:
        response = jsonify(representation.payload)

    # The tag is weak because it identifies the board's stated facts rather
    # than the exact bytes of one reading of them.
    response.set_etag(representation.etag, weak=True)
    return response


__all__ = ["dfs_bp"]
