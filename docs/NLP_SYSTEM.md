# NLP System

The backend turns free-text NBA stat questions into structured API parameters. It uses deterministic parsing first, then optional LLM fallback when the parser marks a query as low-confidence or complex.

## Request path

1. Client calls `POST /api/nl-query` with `{ "query": "LeBron last 10 home games" }`.
2. `app/routes/nl_routes.py` requires Firebase auth when Firebase Admin is configured.
3. `NLService.process_query()` validates the query and runs `BaseQueryParser`.
4. The parser returns query components and confidence metadata.
5. If the confidence breakdown recommends LLM fallback and `OPENAI_API_KEY` is configured, `LLMService` parses the query with the optimized prompt.
6. `NLService` formats the response for the frontend. LLM responses may be merged with NLP player context and marked as `hybrid`.

## Main modules

- `app/services/nl_service.py`: Orchestrates NLP parsing, LLM fallback, and response formatting.
- `app/services/nl_query/parser.py`: Extracts player/team names, date ranges, locations, opponent filters, stat thresholds, game counts, seasons, and intent.
- `app/services/nl_query/parameter_mapper.py`: Converts parsed components into route/service parameter shapes.
- `app/services/nl_query/validators.py`: Validates parsed ranges and enum-like fields.
- `app/services/nl_query/executor.py`: Executes mapped queries against the service layer.
- `app/services/llm_service.py`: Wraps OpenAI calls, prompt loading, retries, and JSON response handling.
- `prompts/system_prompt_optimized.txt`: LLM parsing instructions.

## Response shape

`POST /api/nl-query` returns structured fields rather than executing the game-log endpoint directly:

```json
{
  "player_name": "Stephen Curry",
  "team_name": null,
  "game_count": 10,
  "location": "Home",
  "players_on": [],
  "players_off": [],
  "teams_against": [],
  "minutes_filter": null,
  "date_filter": null,
  "self_filters": [
    {
      "stat_column": "PTS",
      "operator": "gte",
      "value": 25
    }
  ],
  "rank_filter": [],
  "season": "2025-26",
  "confidence": 0.9,
  "intent": "game_logs",
  "time_period": null,
  "original_query": "Stephen Curry last 10 home games with 25+ points",
  "parsed_by": "nlp"
}
```

`parsed_by` can be `nlp`, `llm`, or `hybrid`.

## spaCy and aliases

The parser depends on spaCy and project-specific rule logic. `requirements.txt` includes the `en_core_web_sm` model wheel. If installation cannot fetch the wheel, install it manually:

```bash
python -m spacy download en_core_web_sm
```

Player aliases live in `app/config/player_aliases.yaml`, and fuzzy matching is used where services need to resolve names against database tables.

## LLM fallback

LLM fallback is optional. Required environment:

```bash
OPENAI_API_KEY=...
```

Common optional settings:

```bash
LLM_MODEL=gpt-4o-mini
LLM_TEMPERATURE=0
LLM_MAX_TOKENS=512
LLM_TIMEOUT=10.0
LLM_MAX_RETRIES=3
ENABLE_LLM_FALLBACK=True
LLM_CONFIDENCE_THRESHOLD=0.7
```

If the OpenAI client cannot initialize or a call fails, the service logs the failure and returns the NLP result when available.

## Supported concepts

The rule parser is designed around:

- Player and team names, including aliases and nicknames.
- Game counts, such as "last 10".
- Locations, such as home, away, or both.
- Date expressions, such as "since January 1".
- Stat thresholds, such as "30+ points" or "under 5 turnovers".
- Opponent ranking filters, such as "top 5 defenses" or "worst 10 three-point defenses".
- Teammate on/off filters.
- Intent detection for game logs, player profiles, and team stats.

## Local testing

With the app running:

```bash
curl -X POST http://localhost:5000/api/nl-query \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <firebase-id-token>" \
  -d '{"query": "Show me LeBron James last 10 games at home"}'
```

For credential-free local development, enable the explicit local-only bypass:

```text
FIREBASE_ADMIN_DISABLED=true
```

Unit and integration coverage lives in the pytest suite:

```bash
python -m pytest
```

## Troubleshooting

- Empty query: the route returns `400`.
- Firebase credentials configured but no token sent: protected route returns `401`.
- No OpenAI key: LLM fallback is unavailable, but deterministic NLP can still run.
- Unexpected player resolution: check `player_aliases.yaml`, fuzzy-match thresholds, and whether the bundled database contains the player.
- External NBA data failures: queries that depend on live rankings or data refreshes can fail when upstream APIs are unavailable.
