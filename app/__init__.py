from flask import Flask
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from config import config

db = SQLAlchemy()

def create_app(config_name='default'):
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    
    CORS(app)
    db.init_app(app)

    # Register blueprints
    from app.routes.game_routes import game_bp
    from app.routes.player_routes import player_bp
    from app.routes.team_routes import team_bp
    from app.routes.data_update_routes import data_bp

    app.register_blueprint(game_bp, url_prefix='/api/games')
    app.register_blueprint(player_bp, url_prefix='/api/players')
    app.register_blueprint(team_bp, url_prefix='/api/teams')
    app.register_blueprint(data_bp, url_prefix='/api/data')

    return app