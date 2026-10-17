from flask import Blueprint, jsonify, request

from ..errors import route_error_boundary
from ..utils.auth import require_auth
from ._service_proxy import CurrentAppService

on_off_bp = Blueprint("on_off", __name__)

on_off_service = CurrentAppService("on_off")


def _ids(name: str) -> list[str]:
    """``on[]=1&on[]=2`` (or repeated ``on=``), each value comma-splittable."""

    values = request.args.getlist(f"{name}[]") + request.args.getlist(name)
    return [part for value in values for part in value.split(",") if part.strip()]


@on_off_bp.route("/on_off", methods=["GET"])
@require_auth
@route_error_boundary("Failed to retrieve on/off splits.")
def get_on_off():
    return jsonify(
        on_off_service.get(
            request.args.get("team"),
            request.args.get("season"),
            on=_ids("on"),
            off=_ids("off"),
        )
    )
