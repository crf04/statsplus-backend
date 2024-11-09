from .game_routes import game_bp
from .player_routes import player_bp
from .team_routes import team_bp
from .data_update_routes import data_bp

__all__ = ['game_bp', 'player_bp', 'team_bp', 'data_bp']