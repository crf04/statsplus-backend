"""
LLM Service for NBA Query Processing

This service asks an OpenAI chat model to parse a natural-language query into
a strict JSON schema. The schema is the contract: the SDK sends it as a
structured-output response format, so the model cannot return a shape the
backend does not understand, and the reply is validated into
``LLMParsedQuery`` before anything else reads it.

Player names in the reply are free text. Resolving them against the roster is
the caller's job (``NLService``), because the deterministic parser already owns
the alias, exact, and fuzzy matching.
"""

import os
import time
from typing import Any, Dict, List, Literal, Optional
import logging

from openai import OpenAI
from pydantic import BaseModel

from app.config.settings import LLMSettings, RuntimeSettings, get_runtime_settings


logger = logging.getLogger(__name__)

DEFAULT_PROMPT_PATH = "prompts/system_prompt_optimized.txt"

# The opponent filters the game-log endpoint accepts (docs/API_DOCUMENTATION.md,
# "Common opponent filters"). An enum keeps the model inside that vocabulary.
OpponentFilterType = Literal[
    "OPP_PTS", "OPP_REB", "OPP_AST", "OPP_STOCKS", "OPP_FTA", "OPP_TOV",
    "OPP_BLK", "OPP_STL", "OPP_FG3M", "OPP_FG3A",
    "C&S PTS", "C&S 3s", "C&S 3A", "PU PTS", "PU 2s", "PU 3s",
    "Less Than 10 ft",
    "PRBallHandler", "PRRollMan", "Transition", "Isolation", "Spotup", "Cut",
    "Handoff", "OffScreen", "Postup", "OffRebound", "Misc",
]


class LLMOpponentFilter(BaseModel):
    filter_type: OpponentFilterType
    # +N = the N teams with the highest value of the metric, -N = the lowest
    # (GameService._select_rank over a most-allowed-first ranking).
    rank: int


class LLMSelfFilter(BaseModel):
    stat_column: str
    operator: Literal["gte", "gt", "lt", "lte", "eq", "between"]
    value: float
    value2: Optional[float]


class LLMMinutesRange(BaseModel):
    min: int
    max: int


class LLMParsedQuery(BaseModel):
    """The structured-output schema the model must fill.

    Structured outputs require every field, so optional values are nullable
    rather than defaulted.
    """

    player_name: Optional[str]
    team_name: Optional[str]
    game_count: Optional[int]
    date_range: Optional[str]
    location: Optional[Literal["home", "away"]]
    minutes_filter: Optional[LLMMinutesRange]
    self_filters: List[LLMSelfFilter]
    opponent_filters: List[LLMOpponentFilter]
    players_on: List[str]
    players_off: List[str]
    season: Optional[str]
    intent: Optional[Literal["game_logs", "player_profile", "team_stats"]]
    time_period: Optional[Literal["recent", "season", "month", "week"]]
    confidence: float


class LLMConfig:
    """Configuration class for LLM service settings"""

    def __init__(self, settings: RuntimeSettings | None = None):
        llm_settings: LLMSettings = (
            settings.llm if settings is not None else get_runtime_settings().llm
        )
        self.api_key: str = llm_settings.api_key or ''
        self.model: str = llm_settings.model
        self.temperature: float = llm_settings.temperature
        self.max_tokens: int = llm_settings.max_tokens
        self.timeout: float = llm_settings.timeout_seconds
        self.max_retries: int = llm_settings.max_retries

    def validate(self) -> bool:
        """Validate configuration settings"""
        if not self.api_key:
            logger.error("OpenAI API key not found in environment variables")
            return False

        if self.temperature < 0 or self.temperature > 2:
            logger.warning(f"Temperature {self.temperature} outside recommended range [0, 2]")

        if self.max_tokens < 1:
            logger.error(f"Max tokens must be positive, got {self.max_tokens}")
            return False

        return True


class LLMError(Exception):
    """Custom exception for LLM-related errors"""
    pass


class LLMService:
    """Parse NBA queries into ``LLMParsedQuery`` with an OpenAI chat model."""

    def __init__(self, settings: RuntimeSettings | None = None, client=None):
        self.config = LLMConfig(settings=settings)
        if not self.config.validate():
            raise LLMError("Invalid LLM configuration")

        self.client = client or OpenAI(api_key=self.config.api_key)
        self.system_prompt = self._load_system_prompt_from_file(DEFAULT_PROMPT_PATH)

        logger.info(f"LLM Service initialized with model: {self.config.model}")

    def _get_default_prompt(self) -> str:
        """Prompt used when the prompt file is missing."""
        return (
            "You are an expert at parsing NBA statistics queries into the "
            "provided JSON schema. Fill every field; use null or [] for "
            "anything the query does not mention."
        )

    def _load_system_prompt_from_file(self, file_path: str = DEFAULT_PROMPT_PATH) -> str:
        """
        Load the system prompt from a text file.

        Args:
            file_path: Path to the prompt file

        Returns:
            str: Content of the prompt file
        """
        try:
            # Try relative to current working directory first
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    return f.read().strip()

            # Try relative to this file's directory
            current_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.dirname(os.path.dirname(current_dir))
            full_path = os.path.join(project_root, file_path)

            if os.path.exists(full_path):
                with open(full_path, 'r', encoding='utf-8') as f:
                    return f.read().strip()

            logger.warning(f"Prompt file not found at {file_path} or {full_path}, using default prompt")
            return self._get_default_prompt()

        except Exception as e:
            logger.error(f"Error loading prompt file: {e}")
            return self._get_default_prompt()

    def _completion_options(self) -> Dict[str, int | float | str]:
        """Return model-compatible Chat Completions options."""
        if self.config.model.lower().startswith("gpt-5"):
            return {
                "max_completion_tokens": self.config.max_tokens,
                "reasoning_effort": "minimal",
            }
        return {
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }

    def _build_prompt(self, player_context: Dict[str, Any]) -> str:
        """Append the parser's resolved players so the model reuses them."""
        if not player_context:
            return self.system_prompt
        return (
            f"{self.system_prompt}\n\n"
            "RESOLVED PLAYERS (matched against our roster; reuse these exact "
            "names when the query refers to the same people):\n"
            f"{self._format_player_context(player_context)}"
        )

    def parse_query(
        self, user_query: str, player_context: Optional[Dict[str, Any]] = None
    ) -> LLMParsedQuery:
        """
        Parse one query into the structured-output schema.

        Args:
            user_query: The user's natural language query
            player_context: Players the deterministic parser already resolved

        Returns:
            LLMParsedQuery: The validated model reply

        Raises:
            LLMError: When every attempt fails, is refused, or returns no parse
        """
        messages = [
            {"role": "system", "content": self._build_prompt(player_context or {})},
            {"role": "user", "content": user_query},
        ]
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries):
            try:
                response = self.client.chat.completions.parse(
                    model=self.config.model,
                    messages=messages,
                    response_format=LLMParsedQuery,
                    timeout=self.config.timeout,
                    **self._completion_options(),
                )
                message = response.choices[0].message
                if message.refusal:
                    raise LLMError(f"Model refused the query: {message.refusal}")
                if message.parsed is None:
                    raise LLMError("Model returned no structured output")
                return message.parsed
            except Exception as e:
                last_error = e
                logger.warning(f"LLM attempt {attempt + 1} failed: {e}")
                if attempt < self.config.max_retries - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff

        raise LLMError(
            f"LLM query failed after {self.config.max_retries} attempts: {last_error}"
        ) from last_error

    def _format_player_context(self, player_context: Dict[str, Any]) -> str:
        """Format player context for inclusion in LLM prompt"""
        context_parts = []

        if player_context.get('player_name'):
            context_parts.append(f"Main player: {player_context['player_name']}")

        if player_context.get('players_on'):
            context_parts.append(f"Players on court: {', '.join(player_context['players_on'])}")

        if player_context.get('players_off'):
            context_parts.append(f"Players off court: {', '.join(player_context['players_off'])}")

        return '\n'.join(context_parts) if context_parts else "No player context provided"
