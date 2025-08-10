# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
# Run all tests
python -m pytest tests/

# Run with coverage and verbose output 
python -m pytest tests/ -v --tb=short

# Run specific test categories using the test runner
bash run_tests.sh

# Run individual test files
python -m pytest tests/services/test_llm_service.py -v
python -m pytest tests/test_nl_query.py -v
```

### Database Management
```bash
# Update NBA data from API
curl -X GET http://localhost:5000/api/data/update_database

# Clear cache
python clear_cache.py
```

### Code Quality
Based on .cursorrules, this project emphasizes:
- Type annotations for all functions and classes
- PEP257 docstring conventions
- pytest for all testing (no unittest)
- Comprehensive error handling and logging

## Database Schema
- **SQLite database**: `nba_play_types.db`
- **Tables**: Players, Games, Teams, PlayByPlay, Statistics
- **Relationships**: Player-Game, Team-Game, Player-Statistics

## Architecture Overview

### Natural Language Processing Pipeline
The system uses a hybrid approach combining traditional NLP and LLM integration:

1. **Input Validation**: Sanitize and validate user input
2. **Entity Recognition**: Extract players, teams, dates, metrics using spaCy
3. **Intent Classification**: Determine query type and complexity
4. **Parameter Mapping**: Map entities to database fields
5. **Query Generation**: Build SQL queries or use LLM fallback
6. **Result Processing**: Format and return results

### Service Layer Architecture
- **nl_service.py**: Main natural language processing coordinator
- **llm_service.py**: OpenAI GPT-4o-mini integration with retry logic
- **data_service.py**: Database query execution and caching
- **nba_cache.py**: Redis-based caching for API responses
- **nl_query/**: Modular NL query processing components

### Key Integration Points
- **app/__init__.py**: Flask app initialization and blueprint registration
- **routes/**: RESTful API endpoints grouped by functionality
- **utils/db.py**: Database connection management with environment-driven configuration
- **config/**: YAML configurations and filter mappings

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

---

## AI Development Team Configuration
*Updated by team-configurator on 2025-07-28*

Your project uses: **Flask 2.3.3**, **SQLAlchemy 2.0.31**, **OpenAI GPT-4o-mini**, **spaCy NLP**, **SQLite/MongoDB**

### Specialist Assignments

#### Backend & API Development
- **API Architecture** → @api-architect
  - RESTful endpoint design and optimization
  - API versioning and documentation
  - Authentication and security patterns
  
- **Backend Logic** → @backend-developer
  - Flask application architecture
  - Service layer implementation
  - Business logic optimization
  
- **Python/Flask Specialist** → @django-backend-expert
  - Advanced Python patterns and Flask best practices
  - ORM optimization and database design
  - Performance tuning and scalability
  - *Note: Django expert adapted for Flask - similar Python web framework patterns*

#### Database & Data Management
- **Database Design** → @django-orm-expert
  - SQLAlchemy relationship optimization
  - Query performance and indexing
  - Database schema evolution
  - *Note: ORM expertise translates well between Django and SQLAlchemy*

#### AI & Natural Language Processing
- **LLM Integration** → @backend-developer
  - OpenAI API integration and optimization
  - Prompt engineering and response processing
  - Hybrid AI/traditional query systems
  
- **NLP Pipeline** → @performance-optimizer
  - spaCy and NLTK optimization
  - Entity recognition improvement
  - Query parsing performance

#### Code Quality & Architecture
- **Code Review** → @code-reviewer
  - Code quality assessment
  - Architecture validation
  - Security and best practices review
  
- **Performance** → @performance-optimizer
  - API response time optimization
  - Database query performance
  - Caching strategy implementation
  - LLM response time optimization

#### Documentation & Analysis
- **Project Analysis** → @project-analyst
  - Codebase analysis and insights
  - Architecture documentation
  - Technical debt assessment
  
- **Documentation** → @documentation-specialist
  - API documentation updates
  - Code documentation improvement
  - Technical specification writing

### Task-Based Routing Guide

#### For API Development:
- "Create new NBA player statistics endpoint" → @api-architect
- "Optimize game logs API performance" → @performance-optimizer
- "Add authentication to team endpoints" → @backend-developer

#### For Database Work:
- "Design new player analytics table" → @django-orm-expert
- "Optimize game query performance" → @performance-optimizer
- "Add database indexes for faster lookups" → @django-orm-expert

#### For AI/LLM Features:
- "Improve natural language query parsing" → @backend-developer
- "Add new LLM query capabilities" → @backend-developer
- "Optimize OpenAI API usage and costs" → @performance-optimizer

#### For NLP Processing:
- "Enhance player name entity recognition" → @performance-optimizer
- "Add support for new query types" → @backend-developer
- "Improve fuzzy matching accuracy" → @performance-optimizer

#### For Code Quality:
- "Review my recent changes" → @code-reviewer
- "Analyze codebase architecture" → @project-analyst
- "Document the LLM integration system" → @documentation-specialist

#### For Performance:
- "Speed up database queries" → @performance-optimizer
- "Reduce API response times" → @performance-optimizer
- "Optimize caching strategy" → @performance-optimizer

### Team Collaboration Patterns

#### For Complex Features:
1. **Planning**: @project-analyst → analyze requirements
2. **Architecture**: @api-architect → design endpoints
3. **Implementation**: @backend-developer → core logic
4. **Database**: @django-orm-expert → data layer
5. **Review**: @code-reviewer → quality check
6. **Optimization**: @performance-optimizer → final tuning

#### For Bug Fixes:
1. **Analysis**: @code-reviewer → identify issue
2. **Fix**: @backend-developer → implement solution
3. **Optimization**: @performance-optimizer → if performance-related

#### For New AI Features:
1. **Design**: @backend-developer → LLM integration approach
2. **Implementation**: @backend-developer → core AI logic
3. **Optimization**: @performance-optimizer → speed/cost optimization
4. **Documentation**: @documentation-specialist → feature docs

### How to Use Your AI Team

Simply tag the appropriate specialist in your requests:

**Examples:**
- "Build a new endpoint for player comparison analytics" → @api-architect
- "Optimize the natural language query processing pipeline" → @performance-optimizer
- "Review my LLM integration code for best practices" → @code-reviewer
- "Design a better caching strategy for NBA API calls" → @performance-optimizer
- "Add support for complex multi-player statistical queries" → @backend-developer

Your specialized AI development team is configured and ready to help optimize your NBA backend API!