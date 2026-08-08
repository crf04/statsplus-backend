from flask import Blueprint, jsonify
from ..errors import OperationFailedError, route_error_boundary
from ..utils.auth import require_admin
from ._service_proxy import CurrentAppService

# Initialize blueprint and services
data_bp = Blueprint('data', __name__)

data_service = CurrentAppService("data")

@data_bp.route('/update_database', methods=['POST'])
@require_admin
@route_error_boundary("Failed to update database.")
def update_database():
    success = data_service.update_all_data()
    if success:
        return jsonify({'message': 'Database updated successfully'})
    raise OperationFailedError("Failed to update database.")

@data_bp.route('/player_PBP', methods=['PUT'])
@require_admin
@route_error_boundary("Failed to store player PBP data.")
def store_player_PBP():
    success = data_service.fetch_PBP_data('player')
    if success:
        return jsonify({'message': 'Player PBP data processed and stored successfully'})
    raise OperationFailedError("Failed to store player PBP data.")

@data_bp.route('/opponent_PBP', methods=['PUT'])
@require_admin
@route_error_boundary("Failed to store opponent PBP data.")
def store_opponent_PBP():
    success = data_service.fetch_PBP_data('opponent')
    if success:
        return jsonify({'message': 'Opponent PBP data processed and stored successfully'})
    raise OperationFailedError("Failed to store opponent PBP data.")

@data_bp.route('/fetch_players_with_teams', methods=['POST'])
@require_admin
@route_error_boundary("Failed to fetch players with their teams.")
def fetch_players_with_teams():
    data_service.save_team()
    player_list = data_service.map_id_to_team()
    return jsonify(player_list)
    
@data_bp.route('/fetch_playtypes', methods=['GET'])
@require_admin
@route_error_boundary("Failed to fetch play types.")
def fetch_playtypes():
    return jsonify(data_service.get_playtypes())
