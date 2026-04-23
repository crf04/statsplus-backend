from flask import Blueprint, request, jsonify
from ..utils.db import get_engine
from ..services.team_service import TeamService
from ..utils.auth import require_auth_optional

# Initialize blueprint and services
team_bp = Blueprint('teams', __name__)
engine = get_engine()
team_service = TeamService(engine)

@team_bp.route('/stats', methods=['GET'])
@require_auth_optional
def get_team_stats():
    try:
        category = request.args.get('category')
        team = request.args.get('team')
        date = request.args.get('date')
        
        team_stats = team_service.get_team_stats(category, team, date)
        if not team_stats:
            return jsonify({"error": "No data found for the specified team and category"}), 404
        return jsonify(team_stats)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@team_bp.route('', methods=['GET'])
@require_auth_optional
def get_teams():
    try:
        teams = team_service.get_all_teams()
        return jsonify(teams)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
