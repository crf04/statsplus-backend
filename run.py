from flask import Flask
from flask_cors import CORS
from sqlalchemy import create_engine

# Import blueprints
from app.routes.player_routes import player_bp
from app.routes.game_routes import game_bp
from app.routes.team_routes import team_bp
from app.routes.data_update_routes import data_bp

# Initialize Flask app
app = Flask(__name__)
CORS(app)

# Set up database connection
engine = create_engine('sqlite:///nba_play_types.db')

# Register blueprints
app.register_blueprint(player_bp, url_prefix='/api/players')
app.register_blueprint(game_bp, url_prefix='/api/games')
app.register_blueprint(team_bp, url_prefix='/api/teams')
app.register_blueprint(data_bp, url_prefix='/api/data')

if __name__ == '__main__':
    app.run(debug=True)