"""
LLM Service for NBA Query Processing

This service handles integration with OpenAI's GPT-4o-mini for natural language
query processing with proper error handling, retry logic, and configuration.
"""

import os
import json
import asyncio
from typing import Optional, Dict, Any, List
from dataclasses import asdict
import logging
from openai import OpenAI, AsyncOpenAI
from openai.types.chat import ChatCompletion
from dotenv import load_dotenv

from ..services.nl_query.parser import QueryComponents


# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class LLMConfig:
    """Configuration class for LLM service settings"""
    
    def __init__(self):
        self.api_key: str = os.getenv('OPENAI_API_KEY', '')
        self.model: str = os.getenv('LLM_MODEL', 'gpt-4o-mini')
        self.temperature: float = float(os.getenv('LLM_TEMPERATURE', '0'))
        self.max_tokens: int = int(os.getenv('LLM_MAX_TOKENS', '512'))
        self.timeout: float = float(os.getenv('LLM_TIMEOUT', '10.0'))
        self.max_retries: int = int(os.getenv('LLM_MAX_RETRIES', '3'))
        self.enable_fallback: bool = os.getenv('ENABLE_LLM_FALLBACK', 'True').lower() == 'true'
        self.confidence_threshold: float = float(os.getenv('LLM_CONFIDENCE_THRESHOLD', '0.7'))
    
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
    """
    Service for processing NBA queries using OpenAI's GPT-4o-mini.
    
    Handles prompt management, error handling, retry logic, and response parsing.
    """
    
    def __init__(self, engine=None):
        """
        Initialize the LLM service.
        
        Args:
            engine: Database engine for loading player/team aliases (optional)
        """
        self.config = LLMConfig()
        if not self.config.validate():
            raise LLMError("Invalid LLM configuration")
        
        # Initialize OpenAI clients
        self.client = OpenAI(api_key=self.config.api_key)
        self.async_client = AsyncOpenAI(api_key=self.config.api_key)
        
        self.engine = engine
        self.player_aliases: Dict[str, str] = {}
        self.team_aliases: Dict[str, str] = {}
        
        # Load aliases if engine is provided
        if self.engine:
            self._load_aliases()
        
        logger.info(f"LLM Service initialized with model: {self.config.model}")
    
    def _load_aliases(self) -> None:
        """Load player and team aliases from database"""
        try:
            # Load player aliases
            with self.engine.connect() as conn:
                result = conn.execute("SELECT DISTINCT PLAYER_NAME FROM nba_play_types")
                players = [row[0] for row in result.fetchall()]
                self.player_aliases = {player.lower(): player for player in players}
            
            # Load team aliases (simplified - you may want to expand this)
            self.team_aliases = {
                'lal': 'Los Angeles Lakers',
                'gsw': 'Golden State Warriors',
                'bos': 'Boston Celtics',
                'mia': 'Miami Heat',
                'chi': 'Chicago Bulls',
                # Add more as needed
            }
            
            logger.info(f"Loaded {len(self.player_aliases)} player aliases and {len(self.team_aliases)} team aliases")
            
        except Exception as e:
            logger.warning(f"Failed to load aliases from database: {e}")
    
    def _get_default_prompt(self) -> str:
        """
        Get the default system prompt.
        
        Returns:
            str: Default system prompt
        """
        return """You are an expert at parsing NBA statistics queries and converting them into structured JSON requests.

Parse the user's query and return a JSON object with these fields:
- player_name: Main player name (string or null)
- team_name: Team name (string or null)  
- game_count: Number of games (integer or null)
- date_range: Date range YYYY-MM-DD (string or null)
- opponent_filters: List of [filter_type, rank] pairs or []
- location: "home", "away", or null
- minutes_filter: [min, max] tuple or null
- self_filters: List of stat filters or []
- players_on: List of teammate names or []
- players_off: List of excluded player names or []
- season: Season string or null
- intent: "game_logs", "player_profile", "team_stats", or null
- confidence: Overall confidence score 0-1
- field_confidence: Object with confidence for each field

Focus on accuracy and provide confidence scores for each extracted component."""

    def _build_system_prompt(self) -> str:
        """
        Build the complete system prompt with context.
        
        Returns:
            str: Complete system prompt
        """
        # Try to load from file first
        prompt = self._load_system_prompt_from_file()
        
        # Add dynamic context if available
        if self.player_aliases:
            sample_players = list(self.player_aliases.values())[:20]
            prompt += f"\n\nKNOWN PLAYERS (sample): {', '.join(sample_players)}"
        
        if self.team_aliases:
            prompt += f"\n\nKNOWN TEAMS: {', '.join(self.team_aliases.values())}"
        
        return prompt
    
    def _load_system_prompt_from_file(self, file_path: str = "prompts/system_prompt.txt") -> str:
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
    

    
    def query_llm(self, user_query: str, system_prompt: Optional[str] = None) -> Dict[str, Any]:
        """
        Raw LLM query for testing prompts and responses.
        
        Args:
            user_query: The user's natural language query
            system_prompt: Optional custom system prompt (uses default if None)
            
        Returns:
            Dict containing the LLM response and metadata
        """
        if system_prompt is None:
            system_prompt = self._build_system_prompt()
        
        try:
            for attempt in range(self.config.max_retries):
                try:
                    response: ChatCompletion = self.client.chat.completions.create(
                        model=self.config.model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_query}
                        ],
                        temperature=self.config.temperature,
                        max_tokens=self.config.max_tokens,
                        timeout=self.config.timeout
                    )
                    
                    # Extract response content
                    content = response.choices[0].message.content
                    
                    # Try to parse as JSON
                    try:
                        parsed_content = json.loads(content)
                    except json.JSONDecodeError:
                        parsed_content = {"raw_response": content, "parsing_error": True}
                    
                    return {
                        "success": True,
                        "content": parsed_content,
                        "raw_response": content,
                        "usage": response.usage.model_dump() if response.usage else None,
                        "model": response.model,
                        "attempt": attempt + 1
                    }
                    
                except Exception as e:
                    logger.warning(f"LLM attempt {attempt + 1} failed: {e}")
                    if attempt == self.config.max_retries - 1:
                        raise
                    import time
                    time.sleep(2 ** attempt)  # Exponential backoff
            
        except Exception as e:
            logger.error(f"LLM query failed after {self.config.max_retries} attempts: {e}")
            return {
                "success": False,
                "error": str(e),
                "content": None
            }
    
    async def query_llm_async(self, user_query: str, system_prompt: Optional[str] = None) -> Dict[str, Any]:
        """
        Async version of query_llm for better performance.
        
        Args:
            user_query: The user's natural language query
            system_prompt: Optional custom system prompt
            
        Returns:
            Dict containing the LLM response and metadata
        """
        if system_prompt is None:
            system_prompt = self._build_system_prompt()
        
        try:
            for attempt in range(self.config.max_retries):
                try:
                    response = await self.async_client.chat.completions.create(
                        model=self.config.model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_query}
                        ],
                        temperature=self.config.temperature,
                        max_tokens=self.config.max_tokens,
                        timeout=self.config.timeout
                    )
                    
                    content = response.choices[0].message.content
                    
                    try:
                        parsed_content = json.loads(content)
                    except json.JSONDecodeError:
                        parsed_content = {"raw_response": content, "parsing_error": True}
                    
                    return {
                        "success": True,
                        "content": parsed_content,
                        "raw_response": content,
                        "usage": response.usage.model_dump() if response.usage else None,
                        "model": response.model,
                        "attempt": attempt + 1
                    }
                    
                except Exception as e:
                    logger.warning(f"Async LLM attempt {attempt + 1} failed: {e}")
                    if attempt == self.config.max_retries - 1:
                        raise
                    await asyncio.sleep(2 ** attempt)
            
        except Exception as e:
            logger.error(f"Async LLM query failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "content": None
            }
    
    def parse_query(self, query: str) -> Optional[QueryComponents]:
        """
        Parse a query into QueryComponents using the LLM.
        
        Args:
            query: The natural language query to parse
            
        Returns:
            QueryComponents object or None if parsing failed
        """
        result = self.query_llm(query)
        
        if not result["success"]:
            logger.error(f"LLM parsing failed for query: {query}")
            return None
        
        content = result["content"]
        if isinstance(content, dict) and "parsing_error" not in content:
            try:
                # Convert LLM response to QueryComponents
                # Note: You may need to adjust field mappings based on your QueryComponents structure
                return QueryComponents(
                    player_name=content.get("player_name"),
                    team_name=content.get("team_name"),
                    game_count=content.get("game_count"),
                    date_range=content.get("date_range"),
                    opponent_filters=content.get("opponent_filters", []),
                    location=content.get("location"),
                    minutes_filter=tuple(content["minutes_filter"]) if content.get("minutes_filter") else None,
                    self_filters=content.get("self_filters", []),
                    players_on=content.get("players_on", []),
                    players_off=content.get("players_off", []),
                    intent=content.get("intent"),
                    confidence=content.get("confidence", 0.0),
                    raw_query=query
                )
            except Exception as e:
                logger.error(f"Failed to convert LLM response to QueryComponents: {e}")
                return None
        
        return None
    
    def test_queries(self, queries: List[str]) -> List[Dict[str, Any]]:
        """
        Test multiple queries for prompt development and validation.
        
        Args:
            queries: List of test queries
            
        Returns:
            List of results for each query
        """
        results = []
        for query in queries:
            logger.info(f"Testing query: {query}")
            result = self.query_llm(query)
            results.append({
                "query": query,
                "result": result
            })
        
        return results
    
    def test_prompt_file(self, prompt_file: str, test_query: str) -> Dict[str, Any]:
        """
        Test a specific prompt file with a query.
        
        Args:
            prompt_file: Path to the prompt file to test
            test_query: Query to test with
            
        Returns:
            Dict containing the test result
        """
        try:
            custom_prompt = self._load_system_prompt_from_file(prompt_file)
            
            # Add aliases to custom prompt
            if self.player_aliases:
                sample_players = list(self.player_aliases.values())[:10]
                custom_prompt += f"\n\nKNOWN PLAYERS (sample): {', '.join(sample_players)}"
            
            if self.team_aliases:
                custom_prompt += f"\n\nKNOWN TEAMS: {', '.join(self.team_aliases.values())}"
            
            result = self.query_llm(test_query, system_prompt=custom_prompt)
            result["prompt_file"] = prompt_file
            return result
            
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to test prompt file {prompt_file}: {e}",
                "prompt_file": prompt_file
            }


# Example usage and testing
if __name__ == "__main__":
    # Test queries for development
    test_queries = [
        "LeBron James last 10 games",
        "Stephen Curry vs catch and shoot teams",
        "Kevin Durant with 25+ points last 5 games",
        "Luka Doncic against top 5 transition teams",
        "Giannis at home with 30+ points and 10+ rebounds"
    ]
    
    try:
        # Initialize service (without database for testing)
        llm_service = LLMService()
        
        # Test individual query
        result = llm_service.query_llm("LeBron James last 10 games")
        print("Test Result:")
        print(json.dumps(result, indent=2))
        
        # Test multiple queries
        print("\nBatch Test Results:")
        batch_results = llm_service.test_queries(test_queries[:3])  # Test first 3
        for result in batch_results:
            print(f"Query: {result['query']}")
            print(f"Success: {result['result']['success']}")
            if result['result']['success']:
                print(f"Content: {json.dumps(result['result']['content'], indent=2)}")
            print("-" * 50)
            
    except LLMError as e:
        print(f"LLM Service Error: {e}")
        print("Make sure to set OPENAI_API_KEY in your environment variables")
    except Exception as e:
        print(f"Unexpected error: {e}") 