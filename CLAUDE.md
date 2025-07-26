# NBA Backend API - Claude Context

## Project Overview
This is a Flask-based REST API backend that provides NBA statistics, game logs, and natural language query processing. The system integrates traditional database queries with AI-powered natural language processing and LLM integration for enhanced query capabilities.

## Technology Stack

### Core Framework
- **Flask 2.3.3** - Web framework
- **Flask-CORS 3.0.10** - Cross-origin resource sharing
- **Flask-SQLAlchemy 3.0.5** - Database ORM
- **SQLAlchemy 2.0.31** - Database toolkit
- **SQLite** - Database (nba_play_types.db)

### Authentication & Security
- **Flask-JWT-Extended 4.4.0** - JWT authentication
- **Flask-Bcrypt 1.0.1** - Password hashing
- **bcrypt 4.2.0** - Cryptographic hashing

### Data Processing & Analysis
- **pandas 2.2.2** - Data manipulation
- **numpy 1.26.4** - Numerical computing
- **nba_api 1.4.1** - Official NBA API integration
- **pyarrow 15.0.2** - Columnar data processing

### Natural Language Processing
- **spacy ≥3.7.0** - Advanced NLP pipeline
- **nltk ≥3.8.0** - Natural language toolkit
- **rapidfuzz ≥3.0.0** - Fuzzy string matching
- **dateparser ≥1.1.0** - Date parsing
- **word2number ≥1.1.0** - Text to number conversion
- **PyYAML ≥6.0.0** - YAML configuration

### LLM Integration
- **openai ≥1.0.0** - OpenAI GPT integration
- **python-dotenv 0.21.0** - Environment configuration

### Database & Storage
- **pymongo 4.3.3** - MongoDB driver
- **Flask-PyMongo 2.3.0** - Flask MongoDB integration

### Utility Libraries
- **requests 2.32.2** - HTTP client
- **python-dateutil 2.9.0.post0** - Date utilities
- **pytz 2024.1** - Timezone handling

## Project Structure

```
app/
├── __init__.py                 # App initialization
├── config/                     # Configuration files
│   ├── filter_mappings.py      # Query filter mappings
│   ├── player_aliases.yaml     # Player name aliases
│   └── query_schemas.py        # Query validation schemas
├── models/                     # Database models
├── routes/                     # API route handlers
│   ├── data_update_routes.py   # Data update endpoints
│   ├── game_routes.py          # Game-related endpoints
│   ├── nl_routes.py            # Natural language endpoints
│   ├── player_routes.py        # Player-related endpoints
│   └── team_routes.py          # Team-related endpoints
├── services/                   # Business logic services
│   ├── data_service.py         # Data retrieval service
│   ├── game_service.py         # Game logic service
│   ├── llm_service.py          # LLM integration service
│   ├── nl_service.py           # Natural language service
│   ├── player_service.py       # Player logic service
│   ├── team_service.py         # Team logic service
│   └── nl_query/               # NL query processing
│       ├── executor.py         # Query execution
│       ├── parameter_mapper.py # Parameter mapping
│       ├── parser.py           # Query parsing
│       └── validators.py       # Input validation
└── utils/                      # Utility functions
    ├── database_utils.py       # Database utilities
    ├── date_parser.py          # Date parsing utilities
    ├── filters.py              # Data filtering utilities
    └── helpers.py              # General helpers
```

## API Endpoints

### Player Endpoints (`/api/players`)
- `GET /` - Get all players
- `GET /profile` - Get player profile
- `PUT /fetch` - Fetch/update player data

### Game Endpoints (`/api/games`)
- `GET /logs` - Get game logs with filtering
- Advanced filtering by date, opponent, performance metrics

### Team Endpoints (`/api/teams`)
- `GET /` - Get all teams
- `GET /stats` - Get team statistics

### Natural Language Endpoints (`/api`)
- `POST /nl_query` - Process natural language queries
- `POST /llm_query` - Direct LLM query processing

### Data Management (`/api/data`)
- `PUT /player_PBP` - Store player play-by-play data
- `PUT /opponent_PBP` - Store opponent play-by-play data
- `GET /update_database` - Update database with latest data

## Key Features

### Natural Language Query Processing
The system supports two approaches:
1. **Traditional NLP**: Using spaCy, NLTK, and custom parsers
2. **LLM Integration**: OpenAI GPT-4o-mini for complex query understanding

### Hybrid Query System
- Primary: Rule-based NLP parsing with entity recognition
- Fallback: LLM-powered query interpretation
- Confidence scoring and intelligent routing

### Advanced Filtering
- Multi-dimensional filtering (date, opponent, performance, etc.)
- Fuzzy matching for player and team names
- Statistical performance filters
- Date range and season filtering

### Data Integration
- Real-time NBA API integration
- Play-by-play data processing
- Statistical aggregation and analysis
- Caching for performance optimization

## Environment Configuration

### Required Environment Variables
```bash
# LLM Configuration
OPENAI_API_KEY=your_openai_api_key
LLM_MODEL=gpt-4o-mini
LLM_TEMPERATURE=0
LLM_MAX_TOKENS=512
LLM_TIMEOUT=10.0
LLM_MAX_RETRIES=3
ENABLE_LLM_FALLBACK=True
LLM_CONFIDENCE_THRESHOLD=0.7

# Database Configuration
DATABASE_URL=sqlite:///nba_play_types.db

# Flask Configuration
FLASK_ENV=development
DEBUG=True
```

## Development Commands

### Start Development Server
```bash
python run.py
```
Runs on http://localhost:5000 with debug mode enabled.

### Run Tests
```bash
python -m pytest tests/
```

### Update Database
```bash
python run_tests.py
```

## Database Schema
- **SQLite database**: `nba_play_types.db`
- **Tables**: Players, Games, Teams, PlayByPlay, Statistics
- **Relationships**: Player-Game, Team-Game, Player-Statistics

## Natural Language Processing Pipeline

### Query Processing Flow
1. **Input Validation**: Sanitize and validate user input
2. **Entity Recognition**: Extract players, teams, dates, metrics
3. **Intent Classification**: Determine query type and complexity
4. **Parameter Mapping**: Map entities to database fields
5. **Query Generation**: Build SQL queries or use LLM fallback
6. **Result Processing**: Format and return results

### Supported Query Types
- Player performance queries
- Game log filtering
- Team statistics
- Comparative analysis
- Date-based filtering
- Statistical thresholds

## LLM Integration

### OpenAI GPT-4o-mini Configuration
- **Model**: gpt-4o-mini (optimized for speed and cost)
- **Temperature**: 0 (deterministic responses)
- **Max Tokens**: 512 (concise responses)
- **Timeout**: 10 seconds
- **Retry Logic**: 3 attempts with exponential backoff

### Fallback Strategy
1. Attempt traditional NLP parsing
2. If confidence < threshold, use LLM
3. Parse LLM response into structured query
4. Execute query and return results

## Performance & Optimization
- **Caching**: Query result caching for common requests
- **Database Indexing**: Optimized indexes for fast lookups
- **Connection Pooling**: Efficient database connections
- **Async Processing**: Asynchronous LLM calls where applicable

## Testing
- **Unit Tests**: Service and utility function testing
- **Integration Tests**: API endpoint testing
- **NLP Tests**: Query parsing accuracy testing
- **LLM Tests**: AI query processing validation

## Error Handling
- Comprehensive error logging
- Graceful fallback mechanisms
- User-friendly error messages
- Performance monitoring and alerting

## Security Considerations
- Input sanitization and validation
- SQL injection prevention
- Rate limiting on API endpoints
- Secure environment variable management
- JWT-based authentication ready

## Legacy API Support
The system maintains backward compatibility with legacy endpoints for seamless frontend integration during migration periods.