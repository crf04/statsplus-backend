from flask import Blueprint, request, jsonify
from sqlalchemy import create_engine
import asyncio
from ..utils.db import get_engine
from ..services.game_service import GameService
from ..utils.auth import require_auth, get_current_user


# Initialize blueprint and services
game_bp = Blueprint('games', __name__)
engine = get_engine()
game_service = GameService(engine)

@game_bp.route('/game_logs', methods=['GET'])
@require_auth
def get_game_logs():
    try:
        player_name = request.args.get('player_name')
        filter_params = {
            'minutes_filter': tuple(map(int, request.args.get('minutes_filter', '0,48').split(','))),
            'players_on': request.args.getlist('players_on[]'),
            'players_off': request.args.getlist('players_off[]'),
            'date_filter': request.args.get('date_filter'),
            'teams_against': request.args.getlist('teams_against[]'),
            'rank_filter': request.args.getlist('rank_filter[]'),
            'location_filter': request.args.get('location_filter', 'Both'),
            'game_filter': request.args.get('game_filter'),
            'season_filter': request.args.get('season_filter','2024-25'),
            'playstyle_range': [
                float(request.args.get('playstyle_RTG_min', '0')),
                float(request.args.get('playstyle_RTG_max', '200'))
            ],
            'self_filters': {
                key[13:-1]: list(map(float, value.split(',')))
                for key, value in request.args.items()
                if key.startswith('self_filters[') and key.endswith(']')
            }
        }

        return asyncio.run(game_service.get_filtered_logs(player_name, filter_params))
    except Exception as e:
        return jsonify({'error': str(e)}), 500