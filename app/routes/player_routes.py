import requests
from flask import Blueprint, request, jsonify

from ..errors import (
    InvalidInputError,
    ProviderUnavailableError,
    ResourceNotFoundError,
    route_error_boundary,
)
from ..services.job_service import DataRefreshJobService
from ..services.player_service import PlayerService
from ..utils.auth import require_admin, require_auth_optional
from ._service_proxy import CurrentAppService

# Initialize blueprint and services
player_bp = Blueprint('players', __name__)


def _build_player_service(engine, settings):
    return PlayerService(engine, settings=settings)


def _build_job_service(engine, settings):
    return DataRefreshJobService(engine)


player_service = CurrentAppService("player", _build_player_service)
player_jobs_service = CurrentAppService("player_jobs", _build_job_service)

@player_bp.route('', methods=['GET'])
@require_auth_optional
@route_error_boundary("Failed to retrieve players.")
def get_players():
    players = player_service.get_all_players()
    return jsonify(players)

@player_bp.route('/profile', methods=['GET'])
@require_auth_optional
@route_error_boundary("Failed to retrieve the player profile.")
def get_player_profile():
    player = request.args.get('player_name')
    category = request.args.get('category')
    opp_team = request.args.get('opp_team')

    if not player or not category:
        raise InvalidInputError("player_name and category are required.")

    try:
        profile_data = player_service.get_player_profile(player, category, opp_team)
        if profile_data is None:
            raise ResourceNotFoundError(
                "The requested player profile was not found.",
                detail=f"player_name={player!r}, category={category!r}",
            )
        return jsonify(profile_data)
    except requests.exceptions.RequestException as error:
        raise ProviderUnavailableError(detail=error) from error

@player_bp.route('/fetch', methods=['PUT'])
@require_admin
@route_error_boundary("Failed to schedule the player data refresh.")
def fetch_players():
    job_state = player_jobs_service.start(
        "fetch_players", player_service.store_player_information
    )
    return jsonify(job_state), 202
