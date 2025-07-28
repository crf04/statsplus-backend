# Development Guidelines

## Code Standards

### Python Style Guide
- Follow **PEP 8** style guidelines
- Use **4 spaces** for indentation
- Maximum line length: **88 characters** (Black formatter standard)
- Use **type hints** where applicable

### Naming Conventions
```python
# Variables and functions: snake_case
player_name = "LeBron James"
def get_player_stats():
    pass

# Classes: PascalCase
class PlayerService:
    pass

# Constants: UPPER_CASE
MAX_RETRY_ATTEMPTS = 3

# Private methods: _leading_underscore
def _internal_helper():
    pass
```

### Import Organization
```python
# Standard library
import os
import json
from datetime import datetime

# Third-party packages
import pandas as pd
from flask import Flask, request
import openai

# Local imports
from app.models import Player
from app.services.player_service import PlayerService
```

## Project Architecture

### Directory Structure
```
app/
├── __init__.py                 # App factory
├── config/                     # Configuration files
│   ├── filter_mappings.py      # Query filter mappings
│   ├── player_aliases.yaml     # Player name aliases
│   └── query_schemas.py        # Query validation schemas
├── models/                     # Database models
│   ├── __init__.py
│   ├── player.py
│   ├── game.py
│   └── team.py
├── routes/                     # API route handlers
│   ├── __init__.py
│   ├── player_routes.py
│   ├── game_routes.py
│   ├── team_routes.py
│   ├── nl_routes.py            # Natural language endpoints
│   └── data_update_routes.py
├── services/                   # Business logic
│   ├── __init__.py
│   ├── player_service.py
│   ├── game_service.py
│   ├── team_service.py
│   ├── llm_service.py          # LLM integration
│   ├── nl_service.py           # Natural language processing
│   └── nl_query/               # NL query processing
│       ├── executor.py
│       ├── parameter_mapper.py
│       ├── parser.py
│       └── validators.py
└── utils/                      # Utility functions
    ├── __init__.py
    ├── database_utils.py
    ├── date_parser.py
    ├── filters.py
    └── helpers.py
```

### Design Patterns

#### Service Layer Pattern
Separate business logic from route handlers:

```python
# routes/player_routes.py
@bp.route('/players/<int:player_id>')
def get_player(player_id):
    player = PlayerService.get_player_by_id(player_id)
    return jsonify(player.to_dict())

# services/player_service.py
class PlayerService:
    @staticmethod
    def get_player_by_id(player_id):
        return Player.query.get_or_404(player_id)
```

#### Repository Pattern
Abstract database operations:

```python
# repositories/player_repository.py
class PlayerRepository:
    @staticmethod
    def find_by_name(name):
        return Player.query.filter_by(name=name).first()
    
    @staticmethod
    def find_active_players():
        return Player.query.filter_by(active=True).all()
```

## Database Guidelines

### Model Definitions
```python
class Player(db.Model):
    __tablename__ = 'players'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    team = db.relationship('Team', backref='players')
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'team': self.team.name if self.team else None
        }
```

### Query Optimization
- Use **eager loading** for relationships: `Player.query.options(joinedload(Player.team)).all()`
- Add **database indexes** for frequently queried fields
- Use **pagination** for large result sets
- Implement **query result caching** where appropriate

### Migration Best Practices
```bash
# Create migration
flask db migrate -m "Add player statistics table"

# Review migration file before applying
# Apply migration
flask db upgrade
```

## API Development

### Route Organization
```python
from flask import Blueprint

bp = Blueprint('players', __name__, url_prefix='/api/players')

@bp.route('/')
def get_players():
    """Get all players."""
    pass

@bp.route('/<int:player_id>')
def get_player(player_id):
    """Get specific player by ID."""
    pass
```

### Error Handling
```python
from flask import jsonify
from werkzeug.exceptions import HTTPException

@bp.errorhandler(404)
def not_found(error):
    return jsonify({
        'error': 'Player not found',
        'code': 'PLAYER_NOT_FOUND'
    }), 404

@bp.errorhandler(ValidationError)
def validation_error(error):
    return jsonify({
        'error': str(error),
        'code': 'VALIDATION_ERROR'
    }), 400
```

### Response Formatting
```python
# Standard success response
{
    "data": [...],
    "status": "success",
    "message": "Players retrieved successfully"
}

# Error response
{
    "error": "Error description",
    "code": "ERROR_CODE",
    "status": "error"
}
```

## Natural Language Processing

### Query Processing Pipeline
1. **Input Validation**: Sanitize user input
2. **Entity Recognition**: Extract players, teams, dates
3. **Intent Classification**: Determine query type
4. **Parameter Mapping**: Map entities to database fields
5. **Query Generation**: Build SQL or use LLM
6. **Result Processing**: Format response

### Adding New Query Types
```python
# nl_query/parser.py
class QueryParser:
    def parse_player_stats_query(self, query):
        """Parse player statistics queries."""
        entities = self.extract_entities(query)
        return {
            'type': 'player_stats',
            'player': entities.get('player'),
            'stats': entities.get('stats'),
            'filters': entities.get('filters')
        }
```

### LLM Integration Guidelines
```python
# services/llm_service.py
class LLMService:
    @staticmethod
    def process_query(query, context=None):
        """Process query using OpenAI GPT."""
        try:
            response = openai.ChatCompletion.create(
                model=Config.LLM_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": query}
                ],
                temperature=Config.LLM_TEMPERATURE,
                max_tokens=Config.LLM_MAX_TOKENS,
                timeout=Config.LLM_TIMEOUT
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"LLM processing failed: {e}")
            raise LLMProcessingError(str(e))
```

## Testing Guidelines

### Test Structure
```
tests/
├── conftest.py                 # Test configuration
├── test_models.py              # Model tests
├── test_services.py            # Service layer tests
├── test_routes.py              # API endpoint tests
├── test_nl_processing.py       # NLP tests
└── test_llm_integration.py     # LLM tests
```

### Unit Testing
```python
import pytest
from app.services.player_service import PlayerService

class TestPlayerService:
    def test_get_player_by_id(self, mock_player):
        player = PlayerService.get_player_by_id(1)
        assert player.name == "LeBron James"
    
    def test_get_player_not_found(self):
        with pytest.raises(PlayerNotFoundError):
            PlayerService.get_player_by_id(99999)
```

### Integration Testing
```python
def test_get_players_endpoint(client):
    response = client.get('/api/players')
    assert response.status_code == 200
    assert 'players' in response.json
```

### NLP Testing
```python
def test_player_name_extraction():
    query = "Show me LeBron James stats"
    entities = QueryParser.extract_entities(query)
    assert entities['player'] == "LeBron James"
```

### Running Tests
```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_services.py -v

# Run with coverage
python -m pytest tests/ --cov=app --cov-report=html
```

## Configuration Management

### Environment-based Configuration
```python
# config.py
import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY')
    DATABASE_URL = os.environ.get('DATABASE_URL')
    
class DevelopmentConfig(Config):
    DEBUG = True
    DATABASE_URL = 'sqlite:///dev.db'

class ProductionConfig(Config):
    DEBUG = False
    DATABASE_URL = os.environ.get('DATABASE_URL')

class TestingConfig(Config):
    TESTING = True
    DATABASE_URL = 'sqlite:///:memory:'
```

### Feature Flags
```python
# config/features.py
FEATURE_FLAGS = {
    'ENABLE_LLM_FALLBACK': os.environ.get('ENABLE_LLM_FALLBACK', 'True').lower() == 'true',
    'ENABLE_CACHING': os.environ.get('ENABLE_CACHING', 'True').lower() == 'true',
    'ENABLE_RATE_LIMITING': os.environ.get('ENABLE_RATE_LIMITING', 'False').lower() == 'true'
}
```

## Logging

### Logging Configuration
```python
import logging
from logging.handlers import RotatingFileHandler

def setup_logging(app):
    if not app.debug:
        file_handler = RotatingFileHandler(
            'logs/nba_backend.log', 
            maxBytes=10240000, 
            backupCount=10
        )
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s '
            '[in %(pathname)s:%(lineno)d]'
        ))
        file_handler.setLevel(logging.INFO)
        app.logger.addHandler(file_handler)
        app.logger.setLevel(logging.INFO)
```

### Logging Best Practices
```python
import logging

logger = logging.getLogger(__name__)

# Info level for normal operations
logger.info(f"Processing query for player: {player_name}")

# Warning for recoverable issues
logger.warning(f"Player not found, using fuzzy match: {player_name}")

# Error for exceptions
logger.error(f"Database connection failed: {str(e)}")

# Debug for development
logger.debug(f"Query parameters: {query_params}")
```

## Performance Guidelines

### Caching Strategy
```python
from flask_caching import Cache

cache = Cache()

@cache.memoize(timeout=300)
def get_player_stats(player_id, season):
    return PlayerService.get_stats(player_id, season)
```

### Database Optimization
- Use **connection pooling**
- Implement **query result caching**
- Add **appropriate indexes**
- Use **batch operations** for bulk updates

### API Performance
- Implement **response compression**
- Use **pagination** for large datasets
- Add **rate limiting**
- Monitor **response times**

## Security Guidelines

### Input Validation
```python
from marshmallow import Schema, fields, validate

class PlayerQuerySchema(Schema):
    player_name = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    season = fields.Str(validate=validate.Regexp(r'^\d{4}-\d{2}$'))
```

### SQL Injection Prevention
- Always use **parameterized queries**
- Never concatenate user input directly into SQL
- Use **SQLAlchemy ORM** instead of raw SQL where possible

### API Security
- Implement **rate limiting**
- Add **CORS** configuration
- Use **HTTPS** in production
- Validate **all input parameters**

## Git Workflow

### Branching Strategy
```bash
# Main branches
main                    # Production-ready code
develop                 # Integration branch

# Feature branches
feature/player-stats    # New features
bugfix/query-parser     # Bug fixes
hotfix/security-patch   # Critical fixes
```

### Commit Messages
```bash
# Format: type(scope): description
feat(api): add natural language query endpoint
fix(nlp): resolve player name parsing issue
docs(readme): update installation instructions
test(services): add player service unit tests
```

### Code Reviews
- **All changes** require pull request review
- Check for **code style** compliance
- Verify **test coverage**
- Review **security implications**
- Test **functionality** manually

## Monitoring and Debugging

### Application Monitoring
```python
# Performance monitoring
import time
from functools import wraps

def monitor_performance(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        start_time = time.time()
        result = f(*args, **kwargs)
        duration = time.time() - start_time
        logger.info(f"{f.__name__} executed in {duration:.2f} seconds")
        return result
    return decorated_function
```

### Error Tracking
- Use **Sentry** for error tracking in production
- Implement **health check** endpoints
- Monitor **database performance**
- Track **API response times**

### Development Tools
- **Flask-DebugToolbar** for debugging
- **Flask-Profiler** for performance analysis
- **pytest** for testing
- **black** for code formatting
- **flake8** for linting

This development guide should be followed by all team members to ensure code consistency, maintainability, and quality.