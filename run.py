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
from app.routes.health_routes import health_bp

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
app.register_blueprint(health_bp)



if __name__ == '__main__':
    # Railway provides PORT. Default to 5000 for local dev.
    port = int(os.getenv('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=True)