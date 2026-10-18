import logging

from flask import Blueprint, jsonify, request

from ..errors import InvalidInputError, OperationFailedError, route_error_boundary
from ..utils.auth import get_current_user, require_auth
from ._service_proxy import CurrentAppService

logger = logging.getLogger(__name__)

# Initialize blueprint and services
nl_bp = Blueprint("nl", __name__)

nl_service = CurrentAppService("nl")

# Real queries are a short phrase; keep in step with the parser's
# MAX_FALLBACK_QUERY_CHARS, which bounds regex backtracking.
MAX_QUERY_LENGTH = 500


@nl_bp.route("/nl-query", methods=["POST"])
@require_auth
@route_error_boundary("Failed to process the natural-language query.")
def process_natural_language_query():
    """Process natural language queries and return structured results"""
    # Get authenticated user
    user = get_current_user()

    # Get query from request
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or "query" not in data:
        raise InvalidInputError("A query is required.")

    query = data["query"]
    if not isinstance(query, str) or not query.strip():
        raise InvalidInputError("A non-empty query is required.")

    if len(query) > MAX_QUERY_LENGTH:
        raise InvalidInputError(f"The query must be at most {MAX_QUERY_LENGTH} characters.")

    logger.info("NL query from %s: %.100s", user.get("uid"), query)

    try:
        result = nl_service.process_query(query, user_id=user.get("uid"))
        return jsonify(result)
    except ValueError as error:
        raise InvalidInputError("The natural-language query is invalid.", detail=error) from error
    except RuntimeError as error:
        raise OperationFailedError(
            "The natural-language query service is unavailable.", detail=error
        ) from error
