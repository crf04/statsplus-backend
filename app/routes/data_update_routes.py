from flask import Blueprint, jsonify
from ..errors import AppError, OperationFailedError
from ..services.data_service import DataService
from ..utils.auth import require_admin
from ._service_proxy import CurrentAppService

# Initialize blueprint and services
data_bp = Blueprint('data', __name__)


def _build_data_service(engine, settings):
    return DataService(engine, settings=settings)


data_service = CurrentAppService("data", _build_data_service)

@data_bp.route('/update_database', methods=['POST'])
@require_admin
def update_database():
    try:
        success = data_service.update_all_data()
        if success:
            return jsonify({'message': 'Database updated successfully'})
        raise OperationFailedError("Failed to update database.")
    except AppError:
        raise
    except Exception as error:
        raise OperationFailedError("Failed to update database.", detail=error) from error

@data_bp.route('/player_PBP', methods=['PUT'])
@require_admin
def store_player_PBP():
    try:
        success = data_service.fetch_PBP_data('player')
        if success:
            return jsonify({'message': 'Player PBP data processed and stored successfully'})
        raise OperationFailedError("Failed to store player PBP data.")
    except AppError:
        raise
    except Exception as error:
        raise OperationFailedError(
            "Failed to store player PBP data.", detail=error
        ) from error

@data_bp.route('/opponent_PBP', methods=['PUT'])
@require_admin
def store_opponent_PBP():
    try:
        success = data_service.fetch_PBP_data('opponent')
        if success:
            return jsonify({'message': 'Opponent PBP data processed and stored successfully'})
        raise OperationFailedError("Failed to store opponent PBP data.")
    except AppError:
        raise
    except Exception as error:
        raise OperationFailedError(
            "Failed to store opponent PBP data.", detail=error
        ) from error

@data_bp.route('/fetch_players_with_teams', methods=['POST'])
@require_admin
def fetch_players_with_teams():
    try:
        data_service.save_team()
        player_list = data_service.map_id_to_team()
        return jsonify(player_list)
    except AppError:
        raise
    except Exception as error:
        raise OperationFailedError(
            "Failed to fetch players with their teams.", detail=error
        ) from error
    
@data_bp.route('/fetch_playtypes', methods=['GET'])
@require_admin
def fetch_playtypes():
    try:
        return jsonify(data_service.get_playtypes())
    except AppError:
        raise
    except Exception as error:
        raise OperationFailedError("Failed to fetch play types.", detail=error) from error
