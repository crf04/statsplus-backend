# NLP System

The backend turns free-text NBA stat questions into structured API parameters. It uses deterministic parsing first, then optional LLM fallback when the parser marks a query as low-confidence or complex.

## Request path

1. Client calls `POST /api/nl-query` with `{ "query": "LeBron last 10 home games" }`.
2. `app/routes/nl_routes.py` requires Firebase auth when Firebase Admin is configured.
3. `NLService.process_query()` validates the query and runs `BaseQueryParser`.
4. The parser returns query components and confidence metadata.
5. If the parser's confidence is below `LLM_CONFIDENCE_THRESHOLD` and the fallback is enabled (`ENABLE_LLM_FALLBACK` with `OPENAI_API_KEY`), `LLMService` parses the query into a strict structured-output schema, with the players the parser already resolved as context.
6. `NLService` resolves every player name in the LLM result against the parser's roster, then formats the response for the frontend. LLM results are marked `hybrid`.

## Main modules

- `app/services/nl_service.py`: Orchestrates NLP parsing, LLM fallback, and response formatting.
- `app/services/nl_query/parser.py`: Extracts player/team names, date ranges, locations, opponent filters, stat thresholds, game counts, seasons, and intent.
- `app/services/llm_service.py`: Wraps OpenAI structured-output calls (`LLMParsedQuery`), prompt loading, and retries.
- `prompts/system_prompt_optimized.txt`: LLM parsing instructions (field meanings, opponent filter vocabulary, rank sign). The output shape comes from the schema, not the prompt.

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

`parsed_by` can be `nlp`, `llm`, or `hybrid`. The current service emits `nlp`
for the deterministic path and `hybrid` for an LLM parse.

## spaCy and aliases

The parser depends on spaCy and project-specific rule logic. `requirements.txt` includes the `en_core_web_sm` model wheel. If installation cannot fetch the wheel, install it manually:

```bash
python -m spacy download en_core_web_sm
```

The accepted query vocabulary is English. Every general `dateparser` fallback
therefore declares English explicitly; allowing automatic language discovery
would compile hundreds of locale patterns on each worker's first otherwise
deterministic query.

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
LLM_TIMEOUT=8.0
LLM_MAX_RETRIES=1
ENABLE_LLM_FALLBACK=True
LLM_CONFIDENCE_THRESHOLD=0.9
LLM_SHADOW_SAMPLE_RATE=0
```

`ENABLE_LLM_FALLBACK=false` disables the fallback even when a key is set.
`LLM_CONFIDENCE_THRESHOLD` is the parser confidence below which a query is
sent to the LLM; raising it sends more traffic to the LLM. A query whose
opponent-filter keywords produced no opponent filter is always sent.

The model must fill the `LLMParsedQuery` schema (strict JSON schema with
opponent-filter and operator enums), so a reply either validates or fails.
Player names are free text in the schema and are resolved through
`BaseQueryParser.resolve_player_name`, the same alias, exact, last-name, and
fuzzy matching the parser uses. An unresolved main player falls back to the
NLP name; unresolved teammates are dropped, so the response never names a
player the roster does not know.

If the OpenAI client cannot initialize, a call fails, or the model refuses or
returns no parse, the service logs the failure and returns the NLP result.

## Measuring confident parses (shadow sampling)

A query the parser is confident about never reaches the LLM, so its mistakes
are invisible. `LLM_SHADOW_SAMPLE_RATE` (0 to 1, default `0`, which is off) sends
that fraction of *confident* queries to the LLM as well, **in the background**:

- The response is always the NLP result, and it never waits for the LLM.
- Both results are normalized (tuples vs lists, casing, `both` vs no location)
  and compared on the fields that choose games: player, team, game count,
  location, teammates on/off, opponent filters with their ranks, minutes,
  stat thresholds, date and season. Confidence, intent and time period are
  not compared.
- Each sample logs one `nl_shadow {json}` line with `outcome` (`agree`,
  `disagree` or `error`), the query, the NLP confidence, the latency and, for
  a disagreement, each differing field with both values.
- At most four comparisons are in flight per process; a sample arriving while
  four are outstanding is dropped (`nl_shadow dropped`), never queued.
- Nothing is sampled when the fallback is off. Each sample costs one LLM call,
  so the rate sets the spend: `0.1` costs one extra call per ten confident
  queries.

Summarize the results from the logs:

```bash
railway logs | python scripts/summarize_nl_shadow.py --examples 10
```

The output reports the agreement rate (errors excluded, since an LLM failure
says nothing about NLP accuracy), disagreement counts by field, the median
latency and example disagreements. A disagreement means the parsers differ,
not which one is right; read the examples to decide.
The default fallback budget is one eight-second attempt. Natural-language
parsing is optional enrichment, so retrying inside a public request must not
hold the deterministic result for tens of seconds.

GPT-5 models use `max_completion_tokens` and minimal reasoning for this
structured extraction task. Earlier chat models use `max_tokens` and the
configured temperature. `LLM_MAX_TOKENS` controls the combined reasoning and
visible-output allowance for GPT-5 models.

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

### Self-filter migration

The parser may emit its legacy `SelfFilter(stat_column, operator, value,
value2)` objects, and the HTTP route still accepts `self_filters[STAT]=min,max`.
`GameLogQuery` normalizes both forms to an ordered list of typed
`app.models.game_logs.SelfFilter` models. The range form means inclusive
`between`; typed inputs preserve the exact `gte`, `gt`, `lt`, `lte`, `eq`, or
`between` operator. Repeating a stat preserves each constraint in order, so
`self_filters[PTS]=20,48&self_filters[PTS]=0,30` remains two filters rather
than silently overwriting the first one.
