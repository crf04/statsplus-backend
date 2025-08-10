# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with the services layer of this NBA backend API.

# Services Layer Architecture

This directory contains the business logic layer that orchestrates data processing, natural language processing, and external API integrations.

## Core Service Components

### NL Service (`nl_service.py`)
**Primary coordinator for natural language query processing**
- Initializes and manages the hybrid NLP+LLM pipeline
- Routes queries between traditional NLP parser and LLM fallback
- Handles error fallback when components fail to initialize
- **Key Dependencies**: BaseQueryParser, QueryExecutor, LLMService

### LLM Service (`llm_service.py`)
**OpenAI GPT-4o-mini integration with enterprise-grade reliability**
- Async and sync OpenAI API clients with retry logic
- Environment-driven configuration (LLMConfig class)
- Proper timeout handling and error recovery
- Converts LLM responses to structured QueryComponents
- **Configuration**: Uses OPENAI_API_KEY, LLM_MODEL, LLM_TEMPERATURE, etc.

### Data Service (`data_service.py`)
**NBA API integration and database operations**
- Fetches data from official NBA API endpoints
- Processes and stores team/player statistics
- Handles play-by-play data, opponent stats, shot locations
- Manages bulk data updates for multiple NBA data types
- **External APIs**: nba_api.stats.endpoints, nba_api.stats.static

### NBA Cache (`nba_cache.py`)
**Redis-based caching layer for API responses**
- Reduces NBA API calls through intelligent caching
- Handles cache invalidation and refresh logic
- Optimizes performance for frequently requested data

### Domain-Specific Services
- **game_service.py**: Game logs, matchups, and game-specific logic
- **player_service.py**: Player profiles, statistics, and career data
- **team_service.py**: Team statistics, roster management, and team analytics

## Natural Language Query Processing (`nl_query/`)

This subdirectory contains the modular NLP pipeline components:

### Parser (`parser.py`)
- **BaseQueryParser**: Main parsing coordinator
- **QueryComponents**: Structured data class for parsed queries
- Entity recognition using spaCy for players, teams, dates, metrics
- Query complexity assessment and confidence scoring

### Executor (`executor.py`)  
- **QueryExecutor**: Converts parsed components to SQL queries
- Database query generation and execution
- Result formatting and response preparation
- Handles complex multi-table joins and filtering

### Parameter Mapper (`parameter_mapper.py`)
- Maps natural language entities to database column names
- Handles fuzzy matching for player/team names
- Date parsing and season/game range mapping
- Statistical metric normalization

### Validators (`validators.py`)
- Input sanitization and validation
- Query component validation
- Database constraint checking
- Security input filtering

## Service Integration Patterns

### Initialization Pattern
Services follow dependency injection pattern:
```python
def __init__(self, engine):
    self.engine = engine
    self.initialize_dependencies()
```

### Error Handling Strategy
- Graceful degradation when components fail
- Comprehensive logging with structured messages
- Fallback mechanisms (e.g., LLM when NLP fails)
- User-friendly error messages

### Configuration Management
- Environment variable driven configuration
- Validation of critical settings on startup  
- Support for both development and production environments

## Testing Services

### Unit Testing
```bash
# Test individual services
python -m pytest tests/services/test_llm_service.py -v
python -m pytest tests/services/ -v
```

### Integration Testing  
```bash
# Test service interactions
python -m pytest tests/test_nl_query.py -v
```

## Common Service Development Patterns

### Type Annotations
All services must include comprehensive type hints:
```python
def process_query(self, query: str) -> Dict[str, Any]:
```

### Docstrings
Follow PEP257 conventions for all service methods:
```python
def process_query(self, query: str) -> Dict[str, Any]:
    """Process natural language query with hybrid NLP+LLM routing.
    
    Args:
        query: Natural language query string
        
    Returns:
        Structured query results with metadata
    """
```

### Error Handling
Services should provide specific exception types and detailed logging:
```python
try:
    result = self.process_data()
except DataServiceError as e:
    logger.error(f"Data processing failed: {e}")
    raise
```

## Service Dependencies

### Database Engine
All services receive a SQLAlchemy engine for database operations:
```python
# From utils/db.py - environment driven DB connection
engine = get_engine()
service = DataService(engine)
```

### External APIs
- **OpenAI API**: Requires OPENAI_API_KEY environment variable
- **NBA API**: Uses nba_api package, rate-limited
- **Redis**: Optional caching layer, fallback to in-memory if unavailable

## Performance Considerations

### Caching Strategy
- Use NBA cache for frequently requested NBA API data
- Implement query result caching in nl_service
- Cache parsed query components to avoid re-parsing

### Async Operations
- LLM service supports both sync and async operations
- Use async for non-blocking LLM calls when possible
- Consider async database operations for bulk updates

## Security Notes

- Input sanitization in validators.py prevents SQL injection
- Environment variables for sensitive configuration (API keys)
- Rate limiting considerations for external API calls
- Proper error message sanitization to avoid data leakage