from app.services.nl_query.parser import BaseQueryParser
from app.services.llm_service import LLMParsedQuery, LLMService
from app.services import nl_shadow
from app.config.settings import RuntimeSettings, get_runtime_settings
import logging
import time

logger = logging.getLogger(__name__)

class NLService:
    def __init__(self, engine, settings: RuntimeSettings | None = None):
        self.engine = engine
        self.settings = settings or get_runtime_settings()
        self.nl_parser = None
        self.llm_service = None
        self.shadow_sampler = None
        self.initialize_nl_system()

    def initialize_nl_system(self):
        """Initialize the natural language query system with LLM fallback"""
        try:
            self.nl_parser = BaseQueryParser(self.engine, settings=self.settings)
        except Exception as e:
            logger.exception("Failed to initialize natural language query system: %s", e)
            return

        # ENABLE_LLM_FALLBACK is the switch; settings already force it off
        # when no API key is configured.
        if self.settings.llm.enable_fallback:
            try:
                self.llm_service = LLMService(settings=self.settings)
                logger.info("LLM Service initialized for fallback routing")
            except Exception as llm_error:
                logger.warning("LLM Service initialization failed: %s", llm_error)
                logger.warning("Will continue with NLP-only mode")
                self.llm_service = None
        else:
            logger.info("LLM fallback disabled; using NLP-only mode")

        rate = self.settings.llm.shadow_sample_rate
        if self.llm_service and rate > 0:
            self.shadow_sampler = nl_shadow.ShadowSampler(rate)
            logger.info("Shadow-sampling %.1f%% of confident NLP parses", rate * 100)

        logger.info("Natural language query system initialized")

    def process_query(self, query):
        """Process natural language query with hybrid NLP+LLM routing"""
        if not query or not query.strip():
            raise ValueError("Empty query provided")

        # Check if NL system is initialized
        if not self.nl_parser:
            raise RuntimeError("Natural language system not initialized")

        query_text = query.strip()

        # Step 1: Parse with NLP first
        parsed_components = self.nl_parser.parse(query_text)

        # Step 2: Check if LLM fallback is needed
        should_use_llm = (
            parsed_components.confidence_breakdown and
            parsed_components.confidence_breakdown.should_use_llm
        )

        if should_use_llm and self.llm_service:
            logger.info("Routing to LLM (confidence %.3f): %s", parsed_components.confidence, query_text[:50])
            try:
                return self._process_with_llm(query_text, parsed_components)
            except Exception as e:
                logger.error("LLM processing error: %s", e)
                logger.info("Falling back to NLP result")
            return self._format_nlp_result(parsed_components, query_text)

        logger.info("Using NLP (confidence %.3f): %s", parsed_components.confidence, query_text[:50])
        result = self._format_nlp_result(parsed_components, query_text)
        if self.shadow_sampler and not should_use_llm:
            # Snapshot on the request thread; the comparison never touches
            # the response the route is about to serialize.
            nlp_view = nl_shadow.comparable_view(result)
            self.shadow_sampler.maybe_submit(
                lambda: self._shadow_compare(query_text, parsed_components, nlp_view)
            )
        return result

    def _shadow_compare(self, query_text: str, parsed_components, nlp_view: dict):
        """Parse a confident query with the LLM too and log whether they agree."""
        started = time.monotonic()
        try:
            llm_result = self._process_with_llm(query_text, parsed_components)
        except Exception as error:
            return nl_shadow.log_comparison(
                query_text, parsed_components.confidence, started, error=error
            )
        diff = nl_shadow.differences(nlp_view, nl_shadow.comparable_view(llm_result))
        return nl_shadow.log_comparison(
            query_text, parsed_components.confidence, started, diff=diff
        )

    def _process_with_llm(self, query_text: str, parsed_components):
        """Parse with the LLM, seeded with the players the NLP pass resolved."""
        player_context = self._extract_player_context(parsed_components)
        llm_query = self.llm_service.parse_query(query_text, player_context)
        return self._format_llm_result(llm_query, query_text, parsed_components)

    def _format_llm_result(self, llm_query: LLMParsedQuery, query_text: str, nlp_components):
        """Format a validated LLM parse to match frontend expectations.

        Every player name the model wrote goes through the parser's roster
        resolver, so the response only ever names players we know. An
        unresolved main player falls back to the NLP name; unresolved
        teammates are dropped.
        """
        teams_against = [f.filter_type for f in llm_query.opponent_filters]
        rank_filter = [str(f.rank) for f in llm_query.opponent_filters]

        minutes_filter = (
            [llm_query.minutes_filter.min, llm_query.minutes_filter.max]
            if llm_query.minutes_filter
            else None
        )
        self_filters = []
        for sf in llm_query.self_filters:
            # Minutes are a dedicated filter, not a self filter.
            if sf.stat_column.upper() in ("MIN", "MINUTES"):
                if sf.operator == "between" and sf.value2 is not None and minutes_filter is None:
                    minutes_filter = [_whole(sf.value), _whole(sf.value2)]
                continue
            self_filters.append({
                'stat_column': sf.stat_column,
                'operator': sf.operator,
                'value': _whole(sf.value),
                'value2': _whole(sf.value2),
            })

        player_name = (
            self._resolve_player(llm_query.player_name)
            or getattr(nlp_components, 'player_name', None)
        )

        result = {
            'player_name': player_name,
            'team_name': llm_query.team_name,
            'game_count': llm_query.game_count,
            'location': llm_query.location,
            'players_on': self._resolve_players(llm_query.players_on),
            'players_off': self._resolve_players(llm_query.players_off),
            'teams_against': teams_against,
            'minutes_filter': minutes_filter,
            'date_filter': llm_query.date_range,
            'self_filters': self_filters,
            'rank_filter': rank_filter,
            'season': llm_query.season or self.settings.nba.current_season,
            'confidence': llm_query.confidence,
            'intent': llm_query.intent or 'game_logs',
            'time_period': llm_query.time_period,
            'original_query': query_text,
            # The LLM always parses with the NLP player context in hand.
            'parsed_by': 'hybrid'
        }

        logger.info("LLM parsed query with %.3f confidence", result['confidence'])
        return result

    def _resolve_player(self, name):
        """Map a model-written player name onto the roster, or None."""
        if not name:
            return None
        resolved = self.nl_parser.resolve_player_name(name)
        if not resolved:
            logger.warning("Dropping unresolved LLM player name: %s", name)
        return resolved

    def _resolve_players(self, names):
        resolved = []
        for name in names:
            player = self._resolve_player(name)
            if player and player not in resolved:
                resolved.append(player)
        return resolved

    def _format_nlp_result(self, parsed_components, query_text: str):
        """Format NLP parser result to frontend expectations"""
        # Convert opponent_filters to frontend-compatible format
        teams_against = []
        rank_filter = []

        if parsed_components.opponent_filters:
            for stat, rank in parsed_components.opponent_filters:
                teams_against.append(stat)
                rank_filter.append(str(rank))

        # Convert to frontend-compatible format
        result = {
            'player_name': parsed_components.player_name,
            'team_name': parsed_components.team_name,
            'game_count': parsed_components.game_count,
            'location': parsed_components.location,
            'players_on': parsed_components.players_on,
            'players_off': parsed_components.players_off,
            'teams_against': teams_against,
            'minutes_filter': parsed_components.minutes_filter,
            'date_filter': parsed_components.date_range,
            'self_filters': parsed_components.self_filters,
            'rank_filter': rank_filter,
            'season': getattr(
                parsed_components, 'season', self.settings.nba.current_season
            ),
            'confidence': parsed_components.confidence,
            'intent': parsed_components.intent,
            'time_period': parsed_components.time_period,
            'original_query': query_text,
            'parsed_by': 'nlp'  # Flag to indicate NLP was used
        }

        return result

    def _extract_player_context(self, parsed_components):
        """Extract player context from NLP result to pass to LLM"""
        player_context = {}

        # Extract main player if found with reasonable confidence
        if parsed_components.player_name:
            player_context['player_name'] = parsed_components.player_name

        # Extract players on court if found
        if parsed_components.players_on:
            player_context['players_on'] = parsed_components.players_on

        # Extract players off court if found
        if parsed_components.players_off:
            player_context['players_off'] = parsed_components.players_off

        logger.info(f"📋 Extracted player context: {player_context}")
        return player_context


def _whole(value):
    """Render 25.0 as 25 so LLM thresholds read like the NLP parser's."""
    if value is not None and float(value).is_integer():
        return int(value)
    return value
