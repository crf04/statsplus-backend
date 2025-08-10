# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with the routes layer of this NBA backend API.

# Routes Layer Architecture

This directory contains Flask Blueprint definitions that handle HTTP requests and responses for the NBA backend API. Each route file represents a distinct domain of functionality.

## Route Organization

### Natural Language Routes (`nl_routes.py`)
**Core AI-powered query processing endpoints**
- `POST /api/nl-query` - Process natural language queries using hybrid NLP+LLM system
- **Service**: NLService for query processing
- **Error Handling**: Specific error types (ValueError, RuntimeError) with appropriate HTTP status codes

### Player Routes (`player_routes.py`) 
**Player-centric data and operations**
- `GET /api/players` - Retrieve all players list
- `GET /api/players/profile` - Get detailed player profile with filtering (player_name, category, opp_team)
- `PUT/GET /api/players/fetch` - Fetch and store fresh player data from NBA API
- `PUT/GET /api/players/test` - Test endpoint for shooting type data (development)
- **Service**: PlayerService for all player-related operations

### Game Routes (`game_routes.py`)
**Game logs, matchups, and game-specific queries**
- `GET /api/games/logs` - Retrieve game logs with advanced filtering
- **Service**: GameService for game data processing
- **Features**: Date filtering, opponent filtering, performance metrics

### Team Routes (`team_routes.py`)
**Team statistics and roster management**
- `GET /api/teams` - Get all teams data
- `GET /api/teams/stats` - Team statistics with various aggregations
- **Service**: TeamService for team-related operations

### Data Management Routes (`data_update_routes.py`)
**Bulk data operations and NBA API synchronization**
- `GET /api/data/update_database` - Full database refresh from NBA API
- `PUT /api/data/player_PBP` - Store player play-by-play data
- `PUT /api/data/opponent_PBP` - Store opponent play-by-play data
- `GET /api/data/fetch_players_with_teams` - Map players to teams
- **Service**: DataService for bulk operations and NBA API integration

### Cache Management Routes (`cache_routes.py`)
**Cache control and monitoring**
- Cache invalidation endpoints
- Cache statistics and monitoring
- **Service**: NBA Cache management

### Health Routes (`health_routes.py`)
**System health and monitoring**
- Health check endpoints
- System status monitoring
- Service availability checks

## Blueprint Registration Pattern

All routes follow this initialization pattern in `run.py`:
```python
from app.routes.player_routes import player_bp
app.register_blueprint(player_bp, url_prefix='/api/players')
```

## Common Route Patterns

### Service Injection Pattern
Each route file follows dependency injection:
```python
from ..utils.db import get_engine
from ..services.player_service import PlayerService

engine = get_engine()
player_service = PlayerService(engine)
```

### Error Handling Pattern
Consistent error handling across all routes:
```python
try:
    result = service.method()
    return jsonify(result)
except ValueError as e:
    return jsonify({'error': str(e)}), 400
except RuntimeError as e:
    return jsonify({'error': str(e)}), 500
except Exception as e:
    return jsonify({'error': str(e)}), 500
```

### Request Validation Pattern
Parameter extraction and validation:
```python
# GET parameters
player = request.args.get('player_name')
category = request.args.get('category')

# POST JSON body
data = request.get_json()
if not data or 'query' not in data:
    return jsonify({'error': 'No query provided'}), 400
```

## API Endpoint Structure

### RESTful Conventions
- `GET` - Retrieve data (players, games, teams, stats)
- `POST` - Process complex queries (natural language)
- `PUT` - Update/store data (fetch from NBA API, store PBP data)

### URL Patterns
- `/api/{domain}` - Main resource endpoints
- `/api/{domain}/{action}` - Specific actions (profile, fetch, logs)
- `/api/{action}` - Cross-domain actions (nl-query)

### Response Format
All endpoints return JSON with consistent structure:
```json
{
  "data": { ... },      // Successful response data
  "message": "...",     // Success message
  "error": "..."        // Error message (with appropriate HTTP status)
}
```

## Testing Routes

### Individual Route Testing
```bash
# Test natural language endpoint
curl -X POST http://localhost:5000/api/nl-query \
  -H "Content-Type: application/json" \
  -d '{"query": "LeBron games with 30+ points"}'

# Test player profile
curl "http://localhost:5000/api/players/profile?player_name=LeBron James"

# Test database update
curl "http://localhost:5000/api/data/update_database"
```

### Route Integration Tests
```bash
python -m pytest tests/routes/ -v
```

## Security Considerations

### Input Validation
- JSON request validation for POST endpoints
- Query parameter sanitization for GET endpoints
- Error message sanitization to prevent information leakage

### Rate Limiting Preparation
Routes are structured to easily add rate limiting:
- Consistent blueprint organization
- Service layer abstraction for caching
- Error handling ready for rate limit responses

### CORS Configuration
CORS is handled at the application level in `run.py`:
```python
from flask_cors import CORS
CORS(app)
```

## Development Guidelines

### Adding New Routes
1. Create new blueprint file following naming convention: `{domain}_routes.py`
2. Import and initialize required service with database engine
3. Define route methods with proper error handling
4. Register blueprint in `run.py` with appropriate URL prefix
5. Add route tests in `tests/routes/`

### Route Documentation
Each route should have descriptive docstrings:
```python
@bp.route('/endpoint', methods=['POST'])
def endpoint_handler():
    """Process specific NBA data request.
    
    Expected JSON body:
    {
        "parameter": "value"
    }
    
    Returns:
        JSON response with processed data or error message
    """
```

### Error Response Standards
- `400 Bad Request` - Invalid input, missing parameters
- `404 Not Found` - Resource not found
- `500 Internal Server Error` - Service errors, database issues
- Always include descriptive error messages in JSON format

## Performance Optimization

### Service Layer Caching
Routes delegate caching to service layer:
- NBA API responses cached in nba_cache.py
- Query results cached in respective services
- Route layer focuses on HTTP concerns only

### Async Considerations
While current implementation is synchronous, routes are structured for easy async conversion:
- Service layer abstraction ready for async operations
- Error handling compatible with async/await patterns

## Route Dependencies

### Database Connection
All routes use shared database engine from `utils.db.get_engine()`

### Service Layer
Each route depends on corresponding service:
- `nl_routes.py` → `NLService`
- `player_routes.py` → `PlayerService` 
- `game_routes.py` → `GameService`
- `team_routes.py` → `TeamService`
- `data_update_routes.py` → `DataService`

### Configuration
Routes inherit configuration through services:
- Environment variables managed at service layer
- Database connections configured in utils layer
- API keys and external service config handled by services