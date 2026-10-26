"""Authenticated league Diet reference reads."""

from flask import Blueprint, jsonify

from app.errors import route_error_boundary
from app.utils.auth import require_auth

from ._selected_season import season_argument, season_refusals, selected
from ._service_proxy import CurrentAppService

diet_bp = Blueprint("diet", __name__)
diet_baselines_service = CurrentAppService("diet_baselines")


@diet_bp.route("/baselines", methods=["GET"])
@require_auth
@route_error_boundary("Failed to retrieve league diet baselines.")
def get_diet_baselines():
    service = selected(diet_baselines_service, "diet_baselines", season_argument())
    with season_refusals():
        return jsonify(service.get())
