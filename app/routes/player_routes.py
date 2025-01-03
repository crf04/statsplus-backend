from flask import Blueprint, request, jsonify
from sqlalchemy import create_engine
from ..services.player_service import PlayerService

# Initialize blueprint and services
player_bp = Blueprint('players', __name__)
engine = create_engine('sqlite:///nba_play_types.db')
player_service = PlayerService(engine)

@player_bp.route('', methods=['GET'])
def get_players():
    try:
        players = player_service.get_all_players()
        return jsonify(players)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@player_bp.route('/profile', methods=['GET'])
def get_player_profile():
    try:
        player = request.args.get('player_name')
        category = request.args.get('category')
        opp_team = request.args.get('opp_team')
        
        profile_data = player_service.get_player_profile(player, category, opp_team)
        return jsonify(profile_data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@player_bp.route('/fetch', methods=['PUT','GET'])
def fetch_players():
    try:
        success = player_service.store_player_information()
        if success:
            return jsonify({'message': 'Player data processed and stored successfully'})
        else:
            return jsonify({'error': 'Failed to store player data'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@player_bp.route('/test', methods=['PUT','GET'])
def test():
    try:
        success = player_service._get_shooting_type('LeBron James')
        if success:
            return jsonify({'message': 'Player data processed and stored successfully'})
        else:
            return jsonify({'error': 'Failed to store player data'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500