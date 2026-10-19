"""The connector reports its tool calls here (crf04/statsplus#261)."""

import re

from flask import Blueprint, request

from app.errors import InvalidInputError, route_error_boundary
from app.models.usage_event import OUTCOMES, SOURCE_MCP
from app.services.usage_events import CLIENT_HEADER, request_source
from app.utils.auth import get_current_user, require_auth

from ._service_proxy import CurrentAppService

usage_bp = Blueprint("usage", __name__)

usage_event_service = CurrentAppService("usage_event")

_TOOL_PATTERN = re.compile(r"[a-z_]{1,64}")
MAX_LATENCY_MS = 600_000


@usage_bp.route("/mcp-tool-calls", methods=["POST"])
@require_auth
@route_error_boundary("Failed to record the tool call.")
def post_mcp_tool_call():
    """Store one tool-call event for the signed-in account; answer 204.

    Recording never changes the answer: once the request is valid, a storage
    failure is logged by the service and the response is still 204.
    """

    if request_source() != SOURCE_MCP:
        raise InvalidInputError(f"The {CLIENT_HEADER} header must be mcp.")

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise InvalidInputError('Body must be {"tool", "outcome", "latency_ms"}.')
    tool, outcome, latency_ms = body.get("tool"), body.get("outcome"), body.get("latency_ms")
    if not isinstance(tool, str) or _TOOL_PATTERN.fullmatch(tool) is None:
        raise InvalidInputError("tool must match ^[a-z_]{1,64}$.")
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise InvalidInputError(f"outcome must be one of: {', '.join(OUTCOMES)}.")
    if (
        isinstance(latency_ms, bool)
        or not isinstance(latency_ms, int)
        or not 0 <= latency_ms <= MAX_LATENCY_MS
    ):
        raise InvalidInputError(f"latency_ms must be an integer from 0 to {MAX_LATENCY_MS}.")

    user = get_current_user()
    if user and user.get("uid") and not user.get("is_dev_bypass"):
        usage_event_service.submit_tool_call(user["uid"], tool, outcome, latency_ms)
    return "", 204
