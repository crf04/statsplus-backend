from flask import Blueprint, request, jsonify
from sqlalchemy import create_engine
from ..services.team_service import TeamService

# Initialize blueprint and services
team_bp = Blueprint('teams', __name__)
engine = create_engine('sqlite:///nba_play_types.db')
team_service = TeamService(engine)

@team_bp.route('/stats', methods=['GET'])
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
def get_teams():
    try:
        teams = team_service.get_all_teams()
        return jsonify(teams)
    except Exception as e:
        return jsonify({'error': str(e)}), 500