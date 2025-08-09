from flask import Flask
from flask_cors import CORS
from sqlalchemy import create_engine
import os

# Import blueprints
from app.routes.player_routes import player_bp
from app.routes.game_routes import game_bp
from app.routes.team_routes import team_bp
from app.routes.data_update_routes import data_bp
from app.routes.nl_routes import nl_bp

# Initialize Flask app
app = Flask(__name__)
CORS(app)

from app.utils.db import get_engine

# Set up database connection (env-driven)
engine = get_engine()

# Register blueprints
app.register_blueprint(player_bp, url_prefix='/api/players')
app.register_blueprint(game_bp, url_prefix='/api/games')
app.register_blueprint(team_bp, url_prefix='/api/teams')
app.register_blueprint(data_bp, url_prefix='/api/data')
app.register_blueprint(nl_bp, url_prefix='/api')

# Add legacy API routes for compatibility
from app.routes.game_routes import game_bp as legacy_game_bp
from app.routes.player_routes import player_bp as legacy_player_bp
from app.routes.team_routes import team_bp as legacy_team_bp
from app.routes.data_update_routes import data_bp as legacy_data_bp

# Register legacy routes at root level for compatibility
@app.route('/api/game_logs', methods=['GET'])
def legacy_game_logs():
    from app.routes.game_routes import get_game_logs
    return get_game_logs()

@app.route('/api/players', methods=['GET'])
def legacy_players():
    from app.routes.player_routes import get_players
    return get_players()

@app.route('/api/team_stats', methods=['GET'])
def legacy_team_stats():
    from app.routes.team_routes import get_team_stats
    return get_team_stats()

@app.route('/api/get_teams', methods=['GET'])
def legacy_get_teams():
    from app.routes.team_routes import get_teams
    return get_teams()

@app.route('/api/player_profile', methods=['GET'])
def legacy_player_profile():
    from app.routes.player_routes import get_player_profile
    return get_player_profile()

@app.route('/api/player_PBP', methods=['PUT'])
def legacy_player_pbp():
    from app.routes.data_update_routes import store_player_PBP
    return store_player_PBP()

@app.route('/api/opponent_PBP', methods=['PUT'])
def legacy_opponent_pbp():
    from app.routes.data_update_routes import store_opponent_PBP
    return store_opponent_PBP()

@app.route('/api/fetch_players', methods=['PUT'])
def legacy_fetch_players():
    from app.routes.player_routes import fetch_players
    return fetch_players()

@app.route('/api/update_database', methods=['GET'])
def legacy_update_database():
    from app.routes.data_update_routes import update_database
    return update_database()

if __name__ == '__main__':
    # Railway provides PORT. Default to 5000 for local dev.
    port = int(os.getenv('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=True)