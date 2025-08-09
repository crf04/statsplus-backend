# NBA Backend API

A Flask-based REST API backend that provides NBA statistics, game logs, and natural language query processing with AI-powered LLM integration.

## 🏀 Features

- **NBA Statistics & Game Logs**: Comprehensive player and team statistics
- **Natural Language Queries**: Ask questions in plain English about NBA data
- **AI-Powered Processing**: OpenAI GPT-4o-mini integration for complex queries
- **Real-time Data**: Integration with official NBA API
- **Advanced Filtering**: Multi-dimensional data filtering and search
- **Hybrid Query System**: Traditional NLP + LLM fallback for optimal results

## 🚀 Quick Start

### Prerequisites

- Python 3.8+
- OpenAI API key

### Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd nba-backend
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Set up environment variables:
```bash
cp .env.example .env
# Edit .env with your OpenAI API key and other configurations
```

4. Initialize the database:
```bash
python run.py
```

The server will start on `http://localhost:5000`

## 📚 API Documentation

### Core Endpoints

#### Players
- `GET /api/players` - Get all players
- `GET /api/players/profile` - Get player profile
- `PUT /api/players/fetch` - Fetch/update player data

#### Games
- `GET /api/games/logs` - Get game logs with filtering

#### Natural Language
- `POST /api/nl_query` - Process natural language queries
- `POST /api/llm_query` - Direct LLM query processing

### Example Queries

**Natural Language Examples:**
```bash
curl -X POST http://localhost:5000/api/nl_query \
  -H "Content-Type: application/json" \
  -d '{"query": "Show me LeBron James games with 30+ points this season"}'
```

**Traditional API:**
```bash
curl "http://localhost:5000/api/games/logs?player=LeBron James&min_points=30"
```

## 🛠️ Technology Stack

- **Framework**: Flask 2.3.3
- **Database**: SQLite with SQLAlchemy 2.0.31
- **AI/LLM**: OpenAI GPT-4o-mini
- **NLP**: spaCy, NLTK, rapidfuzz
- **Data**: pandas, numpy, nba_api
- **Authentication**: Flask-JWT-Extended (ready)

## 📁 Project Structure

```
app/
├── routes/          # API endpoints
├── services/        # Business logic
├── models/          # Database models
├── utils/           # Utility functions
└── config/          # Configuration files
```

## 🔧 Configuration

### Environment Variables

```bash
# LLM Configuration
OPENAI_API_KEY=your_openai_api_key
LLM_MODEL=gpt-4o-mini
LLM_TEMPERATURE=0
LLM_MAX_TOKENS=512
ENABLE_LLM_FALLBACK=True

# Database
DATABASE_URL=sqlite:///nba_play_types.db

# Flask
FLASK_ENV=development
DEBUG=True
```

## 🧪 Testing

Run tests:
```bash
python -m pytest tests/
```

Run specific test categories:
```bash
# NLP tests
python -m pytest tests/services/test_nl_service.py

# Integration tests
python -m pytest tests/integration/
```

## 📊 Natural Language Processing

The system supports two query processing approaches:

1. **Traditional NLP**: Fast, rule-based parsing using spaCy and NLTK
2. **LLM Integration**: AI-powered understanding for complex queries

### Supported Query Types

- Player performance: "Show me Curry's best games this month"
- Team statistics: "Lakers vs Warriors head-to-head this season"
- Comparative analysis: "Compare LeBron and Jordan playoff stats"
- Date filtering: "Games from last week with overtime"
- Statistical thresholds: "Triple-doubles in January"

## 🔄 Data Updates

Update NBA data:
```bash
curl -X GET http://localhost:5000/api/data/update_database
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests
5. Submit a pull request

## 📄 License

This project is licensed under the MIT License.

## 🆘 Support

For issues and questions:
- Create an issue on GitHub
- Check the API documentation
- Review the development guidelines

## 🚀 Deployment

See `DEPLOYMENT.md` for production deployment instructions.

### Railway

1. Create a new Railway project and connect this repo.
2. Set service root to `nba-backend/`.
3. Environment variables:
   - `OPENAI_API_KEY`
   - `DATABASE_URL` (use Railway Postgres URL or fallback to SQLite)
4. Build & start:
   - Install: `pip install -r requirements.txt`
   - Start: defined in `Procfile` as `web: gunicorn --bind 0.0.0.0:${PORT} wsgi:app`
5. Deploy. Railway will expose a URL. API is available under `/api/...`.