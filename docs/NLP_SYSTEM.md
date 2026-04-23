### NLP System Overview

This backend uses a hybrid Natural Language Processing (NLP) system to turn free‑text NBA questions into structured parameters for the API.

- Fast path: Deterministic NLP parser (spaCy + rule/pattern logic) → produces QueryComponents
- Safe fallback: LLM parser (OpenAI) when the NLP confidence is low or the query is complex

### Architecture

- `app/services/nl_service.py`
  - Orchestrates the pipeline: initializes NLP, routes to LLM fallback when needed, merges results.
- `app/services/nl_query/parser.py`
  - Core NLP parser. Uses spaCy pipeline (tokenization, NER if available) plus custom pattern logic to extract:
    - player_name, team_name
    - game_count, date_range, time_period
    - opponent_filters (e.g., catch-and-shoot, pullups)
    - location (home/away)
    - minutes_filter, self_filters
    - intent (game_logs, player_profile, team_stats)
    - confidence and field-level confidence
- `app/services/nl_query/parameter_mapper.py`
  - Maps QueryComponents into API route parameter shapes.
- `app/services/nl_query/validators.py`
  - Validates ranges, enum values, consistency.
- `app/services/nl_query/executor.py`
  - Executes mapped queries (joins to services).
- `app/services/llm_service.py`
  - LLM fallback with prompt management, retries, and result shaping.

### Initialization flow

The NL system is set up in `NLService`:

```1:34:statsplus-backend/app/services/nl_service.py
from app.services.nl_query.parser import BaseQueryParser
from app.services.nl_query.executor import QueryExecutor
from app.services.llm_service import LLMService

class NLService:
    def __init__(self, engine):
        self.engine = engine
        self.nl_parser = None
        self.query_executor = None
        self.llm_service = None
        self.initialize_nl_system()

    def initialize_nl_system(self):
        try:
            self.nl_parser = BaseQueryParser(self.engine)
            self.query_executor = QueryExecutor(self.engine)
            try:
                self.llm_service = LLMService()
            except Exception:
                self.llm_service = None
            print("✅ Natural Language Query System initialized successfully")
        except Exception as e:
            print(f"❌ Failed to initialize NL Query System: {e}")
```

### spaCy model and NER

The parser expects a spaCy English pipeline. If a model is not installed/loaded, the pipeline may be empty and accessing NER will fail with:

- [E001] No component 'ner' found in pipeline. Available names: []

Install the small English model for dev and production:

- Local (PowerShell)
  - `python -m spacy download en_core_web_sm`
- Or pin the wheel in `requirements.txt` so deploys include it automatically:
  - `https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.7.1/en_core_web_sm-3.7.1-py3-none-any.whl`

Then load it in your parser code (example):

```python
import spacy
try:
    nlp = spacy.load("en_core_web_sm")
except OSError:
    nlp = spacy.blank("en")
    if nlp.has_factory("ner") and not nlp.has_pipe("ner"):
        nlp.add_pipe("ner")
```

Notes:
- If your pipeline logic relies on entities (players/teams), the model must be present at runtime.
- The rule/pattern logic remains functional even with a blank model, but entity accuracy will be reduced.

### How parsing works (high level)

1. Preprocessing and tokenization (spaCy)
2. Rule/pattern extraction for:
   - player names and team names (fuzzy match and aliasing)
   - time expressions (see NBADateParser)
   - filters like catch‑and‑shoot, pullups, playtype terms
   - numeric/stat thresholds (e.g., "30+ points", minutes ranges)
3. Build QueryComponents with confidence scores (field‑level and overall)
4. If confidence < threshold (configurable), route to LLM fallback
5. Merge LLM output with NLP context using selective overrides

### Configuration

Environment variables (see README.md for full list):

- LLM
  - OPENAI_API_KEY (required for fallback)
  - LLM_MODEL (default: gpt-4o-mini)
  - LLM_TEMPERATURE (default: 0)
  - LLM_MAX_TOKENS (default: 512)
  - LLM_CONFIDENCE_THRESHOLD (default: 0.7)
  - ENABLE_LLM_FALLBACK (default: True)

- NLP
  - spaCy model is brought in via requirements or `python -m spacy download ...`

### End-to-end request path

- Frontend calls `POST /api/nl-query` with `{ "query": "LeBron last 10 games at home" }`
- Backend:
  - NLP parser → QueryComponents with confidence and extracted parameters
  - If low confidence → LLM fallback parses JSON intent & fields
  - Parameter mapper → route/service params
  - Executor → service calls to fetch/aggregate data
  - Response contains structured results and provenance (parsed_by: nlp/llm/hybrid)

### Troubleshooting

- Error: [E001] No component 'ner' found in pipeline.
  - Install `en_core_web_sm` locally or include its wheel in `requirements.txt` for deploys.
  - Ensure your parser loads `en_core_web_sm` with a blank fallback.

- LLM fallback not available / failing
  - Verify OPENAI_API_KEY is set.
  - Check network egress and model name.

- Unexpected parsing output
  - Inspect confidence breakdown and raw tokens.
  - Tweak pattern rules or add aliases.

### Local testing

- Quick NL endpoint test (PowerShell example):

```powershell
curl -Method Post `
  -Uri http://localhost:5000/api/nl-query `
  -Headers @{"Content-Type"="application/json"} `
  -Body '{"query": "Show me LeBron James last 10 games at home"}'
```

### Production recommendations

- Pin spaCy model wheel in `requirements.txt` to avoid runtime downloads.
- Keep ENABLE_LLM_FALLBACK=True for complex user queries.
- Log confidence and parsed fields for continuous improvement.
