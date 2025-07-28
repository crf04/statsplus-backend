# Installation and Setup Guide

## System Requirements

- **Python**: 3.8 or higher
- **Operating System**: Windows, macOS, or Linux
- **Memory**: 4GB RAM minimum (8GB recommended)
- **Storage**: 2GB free space for database and dependencies

## Prerequisites

### 1. Python Installation
Ensure Python 3.8+ is installed:
```bash
python --version
# Should output: Python 3.8.x or higher
```

### 2. OpenAI API Key
Get your API key from [OpenAI Platform](https://platform.openai.com/api-keys)

## Quick Installation

### 1. Clone Repository
```bash
git clone <repository-url>
cd nba-backend
```

### 2. Create Virtual Environment
```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS/Linux
python -m venv venv
source venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Environment Configuration
Create `.env` file:
```bash
cp .env.example .env
```

Edit `.env` with your settings:
```bash
# LLM Configuration
OPENAI_API_KEY=your_openai_api_key_here
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
SECRET_KEY=your-secret-key-here
```

### 5. Initialize Database
```bash
python run.py
```

The server will start on `http://localhost:5000`

## Detailed Setup Instructions

### Virtual Environment Setup

#### Windows
```bash
# Create virtual environment
python -m venv venv

# Activate virtual environment
venv\Scripts\activate

# Verify activation (command prompt should show (venv))
where python
# Should point to venv\Scripts\python.exe
```

#### macOS/Linux
```bash
# Create virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate

# Verify activation
which python
# Should point to venv/bin/python
```

### Dependency Installation

Install core dependencies:
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

#### Core Dependencies Overview
- **Flask 2.3.3**: Web framework
- **SQLAlchemy 2.0.31**: Database ORM
- **openai ≥1.0.0**: OpenAI API integration
- **spacy ≥3.7.0**: Natural language processing
- **pandas 2.2.2**: Data manipulation
- **nba_api 1.4.1**: NBA statistics

### Database Setup

The application uses SQLite by default. The database file (`nba_play_types.db`) will be created automatically when you first run the application.

#### Manual Database Initialization
```bash
python -c "from app import create_app; app = create_app(); app.app_context().push(); from app.models import db; db.create_all()"
```

### SpaCy Model Setup

Install required language model:
```bash
python -m spacy download en_core_web_sm
```

### Environment Variables

#### Required Variables
```bash
OPENAI_API_KEY=sk-...  # Your OpenAI API key
```

#### Optional Variables
```bash
# LLM Settings
LLM_MODEL=gpt-4o-mini              # OpenAI model
LLM_TEMPERATURE=0                  # Response randomness (0-1)
LLM_MAX_TOKENS=512                 # Max response length
LLM_TIMEOUT=10.0                   # Request timeout (seconds)
LLM_MAX_RETRIES=3                  # Retry attempts
ENABLE_LLM_FALLBACK=True           # Enable LLM fallback
LLM_CONFIDENCE_THRESHOLD=0.7       # NLP confidence threshold

# Database
DATABASE_URL=sqlite:///nba_play_types.db

# Flask
FLASK_ENV=development              # development/production
DEBUG=True                         # Enable debug mode
SECRET_KEY=your-secret-key         # Session encryption key
PORT=5000                          # Server port

# API Settings
CORS_ORIGINS=*                     # CORS allowed origins
API_RATE_LIMIT=100                 # Requests per minute
```

## Verification

### 1. Test API Endpoints
```bash
# Test basic endpoint
curl http://localhost:5000/api/players

# Test natural language query
curl -X POST http://localhost:5000/api/nl_query \
  -H "Content-Type: application/json" \
  -d '{"query": "Show me Lakers players"}'
```

### 2. Run Tests
```bash
python -m pytest tests/ -v
```

### 3. Check Dependencies
```bash
pip list | grep -E "(flask|openai|spacy|pandas|nba-api)"
```

## Common Issues and Solutions

### Issue: ModuleNotFoundError
```bash
# Solution: Activate virtual environment
source venv/bin/activate  # macOS/Linux
venv\Scripts\activate     # Windows
```

### Issue: OpenAI API Key Error
```bash
# Solution: Verify API key in .env file
echo $OPENAI_API_KEY  # Should output your key
```

### Issue: SpaCy Model Missing
```bash
# Solution: Install language model
python -m spacy download en_core_web_sm
```

### Issue: Database Permission Error
```bash
# Solution: Check file permissions
chmod 666 nba_play_types.db  # Unix-like systems
```

### Issue: Port Already in Use
```bash
# Solution: Use different port
export PORT=5001
python run.py
```

## Development Setup

### 1. Install Development Dependencies
```bash
pip install -r requirements-dev.txt
```

### 2. Pre-commit Hooks
```bash
pip install pre-commit
pre-commit install
```

### 3. IDE Configuration

#### VS Code
Install extensions:
- Python
- Flask Snippets
- REST Client

Create `.vscode/settings.json`:
```json
{
    "python.defaultInterpreterPath": "./venv/bin/python",
    "python.terminal.activateEnvironment": true,
    "python.testing.pytestEnabled": true
}
```

#### PyCharm
1. Open project folder
2. Configure Python interpreter to use `venv/bin/python`
3. Mark `app` folder as Sources Root

## Production Considerations

### 1. Environment Variables
```bash
FLASK_ENV=production
DEBUG=False
SECRET_KEY=secure-random-string
```

### 2. Database
Consider PostgreSQL for production:
```bash
DATABASE_URL=postgresql://user:password@localhost/nba_backend
```

### 3. Monitoring
Add monitoring tools:
```bash
pip install sentry-sdk flask-monitoring-dashboard
```

## Next Steps

After successful installation:

1. Read `API_DOCUMENTATION.md` for API usage
2. Check `DEVELOPMENT.md` for development guidelines
3. Review `DEPLOYMENT.md` for production setup
4. Explore example queries in `examples/` folder

## Getting Help

- Check the [Issues](link-to-issues) page
- Review logs in `logs/` directory
- Enable debug mode for detailed error messages
- Verify all environment variables are set correctly