from flask import Blueprint, jsonify
from sqlalchemy import create_engine
from ..services.data_service import DataService

# Initialize blueprint and services
data_bp = Blueprint('data', __name__)
engine = create_engine('sqlite:///nba_play_types.db')
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