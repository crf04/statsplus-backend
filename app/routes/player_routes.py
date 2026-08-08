import requests
from flask import Blueprint, request, jsonify

from ..errors import (
    InvalidInputError,
    OperationFailedError,
    ProviderUnavailableError,
    ResourceNotFoundError,
    route_error_boundary,
)
from ..utils.auth import require_admin, require_auth_optional
from ._service_proxy import CurrentAppService

# Initialize blueprint and services
player_bp = Blueprint('players', __name__)

player_service = CurrentAppService("player")

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
@route_error_boundary("Failed to store player data.")
def fetch_players():
    success = player_service.store_player_information()
    if success:
        return jsonify({'message': 'Player data processed and stored successfully'})
    raise OperationFailedError("Failed to store player data.")
