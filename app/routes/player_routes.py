import requests
from flask import Blueprint, request, jsonify

from ..errors import (
    AppError,
    InvalidInputError,
    OperationFailedError,
    ProviderUnavailableError,
    ResourceNotFoundError,
)
from ..services.player_service import PlayerService
from ..utils.auth import require_admin, require_auth_optional
from ._service_proxy import CurrentAppService

# Initialize blueprint and services
player_bp = Blueprint('players', __name__)


def _build_player_service(engine, settings):
    return PlayerService(engine, settings=settings)


player_service = CurrentAppService("player", _build_player_service)

@player_bp.route('', methods=['GET'])
@require_auth_optional
def get_players():
    try:
        players = player_service.get_all_players()
        return jsonify(players)
    except AppError:
        raise
    except Exception as error:
        raise OperationFailedError("Failed to retrieve players.", detail=error) from error

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
    except AppError:
        raise
    except requests.exceptions.RequestException as error:
        raise ProviderUnavailableError(detail=error) from error
    except Exception as error:
        raise OperationFailedError(
            "Failed to retrieve the player profile.", detail=error
        ) from error

@player_bp.route('/fetch', methods=['PUT'])
@require_admin
def fetch_players():
    try:
        success = player_service.store_player_information()
        if success:
            return jsonify({'message': 'Player data processed and stored successfully'})
        raise OperationFailedError("Failed to store player data.")
    except AppError:
        raise
    except Exception as error:
        raise OperationFailedError("Failed to store player data.", detail=error) from error
