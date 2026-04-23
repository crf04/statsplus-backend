from .game_routes import game_bp
from .data_update_routes import data_bp
from .health_routes import health_bp
from .nl_routes import nl_bp
from .player_routes import player_bp
from .team_routes import team_bp
from .user_routes import user_bp

__all__ = [
    'data_bp',
    'game_bp',
    'health_bp',
    'nl_bp',
    'player_bp',
    'team_bp',
    'user_bp',
]
