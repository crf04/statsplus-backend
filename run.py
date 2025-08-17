from flask import Flask
from flask_cors import CORS
from sqlalchemy import create_engine
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Import blueprints
from app.routes.player_routes import player_bp
from app.routes.game_routes import game_bp
from app.routes.team_routes import team_bp
from app.routes.data_update_routes import data_bp
from app.routes.nl_routes import nl_bp
from app.routes.health_routes import health_bp
from app.routes.user_routes import user_bp

# Initialize Flask app
app = Flask(__name__)
CORS(app)

from app.utils.db import get_engine
from app.utils.firebase_admin import initialize_firebase_admin
from app.models import create_all_tables

# Set up database connection (env-driven)
engine = get_engine()

# Initialize Firebase Admin SDK
initialize_firebase_admin()

# Create database tables (only creates if they don't exist)
try:
    create_all_tables()
except Exception as e:
    print(f"Warning: Could not create database tables: {e}")
    print("The application will continue, but user features may not work properly.")

# Global error handlers
@app.errorhandler(401)
def handle_unauthorized(e):
    from flask import jsonify
    return jsonify({
        'error': 'Unauthorized',
        'message': 'Please sign in to access this resource'
    }), 401

@app.errorhandler(403)
def handle_forbidden(e):
    from flask import jsonify
    return jsonify({
        'error': 'Forbidden',
        'message': 'You do not have permission to access this resource'
    }), 403

# Register blueprints
app.register_blueprint(player_bp, url_prefix='/api/players')
app.register_blueprint(game_bp, url_prefix='/api/games')
app.register_blueprint(team_bp, url_prefix='/api/teams')
app.register_blueprint(data_bp, url_prefix='/api/data')
app.register_blueprint(nl_bp, url_prefix='/api')
app.register_blueprint(health_bp)
app.register_blueprint(user_bp, url_prefix='/api/user')



if __name__ == '__main__':
    # Railway provides PORT. Default to 5000 for local dev.
    port = int(os.getenv('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=True)