from flask import Flask
from flask_cors import CORS
from sqlalchemy import create_engine
from app.services.game_service import GameService
from app.services.team_service import TeamService
from app.services.data_service import DataService

# Initialize Flask app
app = Flask(__name__)
CORS(app)

# Set up database connection
engine = create_engine('sqlite:///nba_play_types.db')

# Initialize services
game_service = GameService(engine)
team_service = TeamService(engine)
data_service = DataService(engine)

if __name__ == '__main__':
    app.run(debug=True)