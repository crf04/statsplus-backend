from flask import Blueprint, jsonify
from sqlalchemy import create_engine
from ..utils.db import get_engine
from ..services.data_service import DataService

# Initialize blueprint and services
data_bp = Blueprint('data', __name__)
engine = get_engine()
data_service = DataService(engine)

@data_bp.route('/update_database', methods=['GET'])
def update_database():
    try:
        success = data_service.update_all_data()
        if success:
            return jsonify({'message': 'Database updated successfully'})
        else:
            return jsonify({'error': 'Failed to update database'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@data_bp.route('/player_PBP', methods=['PUT'])
def store_player_PBP():
    try:
        success = data_service.fetch_PBP_data('player')
        if success:
            return jsonify({'message': 'Player PBP data processed and stored successfully'})
        else:
            return jsonify({'error': 'Failed to store player PBP data'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@data_bp.route('/opponent_PBP', methods=['PUT'])
def store_opponent_PBP():
    try:
        success = data_service.fetch_PBP_data('opponent')
        if success:
            return jsonify({'message': 'Opponent PBP data processed and stored successfully'})
        else:
            return jsonify({'error': 'Failed to store opponent PBP data'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@data_bp.route('/fetch_players_with_teams', methods=['GET'])
def fetch_players_with_teams():
    try:
        data_service.save_team()
        player_list = data_service.map_id_to_team()
        return jsonify(player_list)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@data_bp.route('/fetch_playtypes', methods=['GET'])
def fetch_playtypes():
    try:
        return jsonify(data_service.get_playtypes())
    except Exception as e:
        return jsonify({'error': str(e)}), 500