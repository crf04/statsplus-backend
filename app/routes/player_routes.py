import requests
from flask import Blueprint, request, jsonify

from app.config.settings import get_runtime_settings

from ..errors import (
    InvalidInputError,
    ProviderUnavailableError,
    ResourceNotFoundError,
)
from ..utils.db import get_engine
from ..services.player_service import PlayerService
from ..utils.auth import require_admin, require_auth_optional

# Initialize blueprint and services
player_bp = Blueprint('players', __name__)
runtime_settings = get_runtime_settings()
engine = get_engine(runtime_settings)
player_service = PlayerService(engine, settings=runtime_settings)

@player_bp.route('', methods=['GET'])
@require_auth_optional
def get_players():
    try:
        players = player_service.get_all_players()
        return jsonify(players)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@player_bp.route('/profile', methods=['GET'])
@require_auth_optional
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
    except (InvalidInputError, ResourceNotFoundError):
        raise
    except requests.exceptions.RequestException as error:
        raise ProviderUnavailableError(detail=error) from error

@player_bp.route('/fetch', methods=['PUT'])
@require_admin
def fetch_players():
    try:
        success = player_service.store_player_information()
        if success:
            return jsonify({'message': 'Player data processed and stored successfully'})
        else:
            return jsonify({'error': 'Failed to store player data'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500
