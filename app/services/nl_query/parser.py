"""
Base natural language query parser

This module provides the foundation for parsing natural language queries
into structured components that can be mapped to API parameters.
"""

import spacy
import pandas as pd
import re
import yaml
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from rapidfuzz import process, fuzz

@dataclass
class QueryComponents:
    """Structured representation of a parsed natural language query"""
    
    # Core entities
    player_name: Optional[str] = None
    team_name: Optional[str] = None
    
    # Time-related filters
    time_period: Optional[str] = None  # "recent", "season", "month", etc.
    game_count: Optional[int] = None   # Number of games for "last X games"
    date_range: Optional[str] = None   # Start date for filtering
    
    # Filtering criteria
    opponent_filters: List[Tuple[str, int]] = field(default_factory=list)  # [(filter_type, rank), ...]
    location: Optional[str] = None     # "home", "away", "both"
    
    # Query metadata
    intent: Optional[str] = None       # "game_logs", "player_profile", "team_stats"
    confidence: float = 0.0            # Confidence score (0-1)
    raw_query: str = ""               # Original query text
    
    # Additional filters (for future expansion)
    stat_categories: List[str] = field(default_factory=list)
    players_on: List[str] = field(default_factory=list)
    players_off: List[str] = field(default_factory=list)

class BaseQueryParser:
    """
    Base natural language query parser for NBA analytics
    
    This class provides the foundation for parsing natural language queries
    about NBA players and teams into structured components that can be used
    with the existing API.
    """
    
    def __init__(self, db_engine):
        """
        Initialize the parser with database connection
        
        Args:
            db_engine: SQLAlchemy database engine for data access
        """
        self.engine = db_engine
        self.nlp = self._load_spacy_model()
        
        # Load reference data from existing database
        self.players = self._load_players()
        self.teams = self._load_teams()
        
        # Load player aliases from configuration file
        self.player_aliases = self._load_player_aliases()
        
        # Load configuration
        from ...config.query_schemas import INTENT_PATTERNS
        from ...config.filter_mappings import (
            FILTER_MAPPINGS, LOCATION_SYNONYMS, 
            TIME_PERIOD_SYNONYMS, NUMBER_WORDS, RANKING_TERMS
        )
        
        self.intent_patterns = INTENT_PATTERNS
        self.filter_mappings = FILTER_MAPPINGS
        self.location_synonyms = LOCATION_SYNONYMS
        self.time_synonyms = TIME_PERIOD_SYNONYMS
        self.number_words = NUMBER_WORDS
        self.ranking_terms = RANKING_TERMS
    
    def _load_spacy_model(self):
        """Load spaCy model with error handling"""
        try:
            import spacy
            return spacy.load("en_core_web_sm")
        except OSError:
            raise RuntimeError(
                "spaCy English model not found. Please run: "
                "python -m spacy download en_core_web_sm"
            )
    
    def _load_players(self) -> List[str]:
        """Load player names from existing database"""
        # Try different possible table and column combinations
        possible_queries = [
            "SELECT DISTINCT PLAYER_NAME FROM player_play_types",
            "SELECT DISTINCT PLAYER_NAME FROM play_types", 
            "SELECT DISTINCT PLAYER_NAME FROM nba_play_types",
            "SELECT DISTINCT Player_Name FROM player_play_types",
            "SELECT DISTINCT Player_Name FROM play_types",
            "SELECT DISTINCT player_name FROM player_play_types",
            "SELECT DISTINCT player_name FROM play_types",
        ]
        
        for query in possible_queries:
            try:
                with self.engine.connect() as conn:
                    df = pd.read_sql(query, conn)
                players = df.iloc[:, 0].dropna().unique().tolist()  # Get first column regardless of name
                if players:  # Only use if we got results
                    # Successfully loaded players from database
                    return players
            except Exception as e:
                pass  # Query failed, try next one
                continue
        
        # Could not load players from database - all queries failed
        return []
    
    def _load_teams(self) -> Dict[str, str]:
        """Load team information from NBA API"""
        try:
            from nba_api.stats.static import teams
            nba_teams = teams.get_teams()
            
            team_mapping = {}
            for team in nba_teams:
                full_name = team['full_name']
                abbr = team['abbreviation']
                city = team['city']
                nickname = team['nickname']
                
                # Create multiple mappings for flexible matching
                team_mapping[full_name.lower()] = abbr
                team_mapping[abbr.lower()] = abbr
                team_mapping[city.lower()] = abbr
                team_mapping[nickname.lower()] = abbr
            
            # Successfully loaded teams from database
            return team_mapping
        except Exception as e:
            # Warning: Could not load teams from database
            return {}
    
    def _load_player_aliases(self) -> Dict[str, str]:
        """Load player aliases from YAML configuration file"""
        try:
            # Get the path to the aliases file
            current_dir = os.path.dirname(os.path.abspath(__file__))
            aliases_path = os.path.join(current_dir, '..', '..', 'config', 'player_aliases.yaml')
            
            if os.path.exists(aliases_path):
                with open(aliases_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)
                    aliases = config.get('aliases', {})
                    # Successfully loaded player aliases from configuration
                    return aliases
            else:
                # Player aliases file not found - using empty aliases
                return {}
        except Exception as e:
            # Warning: Could not load player aliases
            return {}
    
    def parse(self, query: str) -> QueryComponents:
        """
        Parse a natural language query into structured components
        
        Args:
            query: Natural language query string
            
        Returns:
            QueryComponents object with parsed information
        """
        components = QueryComponents(raw_query=query)
        
        # Preprocess query
        processed_query = self._preprocess_query(query)
        doc = self.nlp(processed_query)
        
        # OPTIMIZATION: Extract player information in single pass
        # This avoids calling _extract_players_with_syntax twice
        components.player_name, components.players_on, components.players_off = self._extract_players_with_syntax(query, doc)
        
        # Extract other components
        components.team_name = self._extract_team_name(query, doc)
        components.time_period, components.game_count = self._extract_time_period(query)
        components.location = self._extract_location(query)
        components.opponent_filters = self._extract_opponent_filters(query)
        components.intent = self._classify_intent(query, components)
        
        # Calculate confidence score
        components.confidence = self._calculate_confidence(components)
        
        return components
    
    def _preprocess_query(self, query: str) -> str:
        """Clean and normalize query text"""
        # Remove extra whitespace
        query = ' '.join(query.split())
        
        # Handle common contractions
        query = query.replace("'s", "")  # "Mitchell's" -> "Mitchell"
        
        # Normalize common terms
        replacements = {
            "pts": "points",
            "rebs": "rebounds", 
            "asts": "assists",
            "3pt": "three point",
            "vs": "against",
            "v": "against"
        }
        
        for abbr, full in replacements.items():
            query = re.sub(rf'\b{abbr}\b', full, query, flags=re.IGNORECASE)
        
        return query
    
    def _extract_player_name(self, query: str, doc) -> Optional[str]:
        """Extract main player name from full query using spaCy syntax analysis
        
        NOTE: This method is no longer used in the main parse() flow to avoid redundant processing.
        The main parse() method calls _extract_players_with_syntax() directly.
        This method is kept for backwards compatibility and testing purposes.
        """
        main_player, _, _ = self._extract_players_with_syntax(query, doc)
        return main_player
    
    def _extract_single_player_name(self, text: str, context: str = "fragment") -> Optional[str]:
        """Universal player name extraction method - handles both full queries and text fragments"""
        if not text or len(text.strip()) < 2:
            return None
        
        text = text.strip()
        text_lower = text.lower()
        
        # STEP 1: Check player aliases first (highest priority)
        # Collect ALL matches with their positions, then sort by position
        found_matches = []
        alias_list = list(self.player_aliases.keys())
        
        # For short abbreviations, use word boundary matching to avoid substring matches
        # This prevents "ad" from matching within "bread" in "hector banana-bread"
        short_abbrevs = ['ad', 'kd', 'cp3', 'pg', 'jt', 'sga']  # Common short abbreviations
        for abbrev in short_abbrevs:
            if abbrev in self.player_aliases:
                pattern = r'\b' + re.escape(abbrev) + r'\b'
                matches = list(re.finditer(pattern, text_lower))
                for match in matches:
                    found_matches.append({
                        'alias': abbrev,
                        'player': self.player_aliases[abbrev],
                        'position': match.start(),
                        'type': 'short_abbrev'
                    })
        
        # For other aliases, use substring matching
        for alias in alias_list:
            if alias not in short_abbrevs and alias in text_lower:
                start_pos = text_lower.find(alias)
                found_matches.append({
                    'alias': alias,
                    'player': self.player_aliases[alias],
                    'position': start_pos,
                    'type': 'substring'
                })
        
        # Sort by position (left to right) and return the first match
        if found_matches:
            found_matches.sort(key=lambda x: x['position'])
            first_match = found_matches[0]
            # Found alias match
            return first_match['player']
        
        # STEP 2: Try exact substring matching first (prioritize longer matches)
        words = text_lower.split()
        # Check all possible phrases from longest to shortest
        for length in range(min(4, len(words)), 0, -1):  # Start with longest possible phrases
            for i in range(len(words) - length + 1):
                phrase = ' '.join(words[i:i+length])
                if len(phrase) > 2:
                    # Try exact substring matching
                    for alias in alias_list:
                        if phrase == alias:
                            # Found exact phrase match
                            return self.player_aliases[alias]
        
        # STEP 3: Try fuzzy matching on word combinations (as fallback)
        for i in range(len(words)):
            for j in range(i + 1, min(i + 4, len(words) + 1)):
                phrase = ' '.join(words[i:j])
                if len(phrase) > 2:
                    alias_match = process.extractOne(
                        phrase,
                        alias_list,
                        scorer=fuzz.partial_ratio,
                        score_cutoff=85
                    )
                    if alias_match:
                        matched_alias = alias_match[0]
                        # Found fuzzy phrase match
                        return self.player_aliases[matched_alias]
        
        # STEP 4: Try direct fuzzy matching against player database
        match = process.extractOne(
            text,
            self.players,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=75
        )
        if match:
            return match[0]
        
        
        return None
    
    def _extract_team_name(self, query: str, doc) -> Optional[str]:
        """Extract team name from query"""
        query_lower = query.lower()
        
        # Check for team mentions
        for team_variant, abbr in self.teams.items():
            if team_variant in query_lower:
                return abbr
        
        return None
    
    def _extract_time_period(self, query: str) -> Tuple[Optional[str], Optional[int]]:
        """Extract time period and game count from query"""
        query_lower = query.lower()
        
        # Pattern: "last/past X games"
        game_patterns = [
            r'(?:last|past|recent)\s+(\d+)\s+(?:games?|matchups?|contests?)',
            r'(?:last|past|recent)\s+(\w+)\s+(?:games?|matchups?|contests?)'
        ]
        
        for pattern in game_patterns:
            match = re.search(pattern, query_lower)
            if match:
                number_str = match.group(1)
                try:
                    if number_str.isdigit():
                        return "recent", int(number_str)
                    elif number_str in self.number_words:
                        return "recent", self.number_words[number_str]
                    else:
                        # Try word2number for more complex numbers
                        from word2number import w2n
                        return "recent", w2n.word_to_num(number_str)
                except:
                    pass
        
        # Check for season references
        #add things for last year, two years ago, etc
        season_terms = ["season", "this year", "2024-25", "current season"]
        if any(term in query_lower for term in season_terms):
            return "season", None
        
        # Check for month references
        month_terms = ["this month", "past month", "last 30 days"]
        if any(term in query_lower for term in month_terms):
            return "month", None
        
        return None, None
    
    def _extract_location(self, query: str) -> Optional[str]:
        """Extract home/away location preference"""
        query_lower = query.lower()
        
        for location, synonyms in self.location_synonyms.items():
            if any(synonym in query_lower for synonym in synonyms):
                return location.lower()
        
        return None
    
    def _extract_opponent_filters(self, query: str) -> List[Tuple[str, int]]:
        """Extract opponent-based filters (e.g., 'top 10 defenses')"""
        filters = []
        query_lower = query.lower()
        
        # Pattern: "against top/worst X category"
        # Use greedy matching to capture full multi-word categories like "three point defense"
        pattern = r'against\s+(top|best|worst|bottom)\s+(\d+)\s+([\w\s]+?)(?:\s+(?:teams?|matchups?|games?)|$)'
        matches = re.finditer(pattern, query_lower)
        
        for match in matches:
            direction, number, category = match.groups()
            try:
                rank_num = int(number)
                
                # Map category to filter system - find the best match (most specific)
                best_match = None
                best_match_score = 0
                
                for filter_name, config in self.filter_mappings.items():
                    matching_keywords = [keyword for keyword in config["keywords"] if keyword in category]
                    if matching_keywords:
                        # Score based on total length of matching keywords (more specific = higher score)
                        score = sum(len(keyword) for keyword in matching_keywords)
                        if score > best_match_score:
                            best_match_score = score
                            best_match = (filter_name, config)
                
                if best_match:
                    filter_name, config = best_match
                    # Determine ranking direction
                    if direction in self.ranking_terms["positive"]:
                        if config["ranking_direction"] == "ascending":
                            # For ascending stats (like defense), "top 10" means bottom 10 values
                            rank = -rank_num
                        else:
                            # For descending stats (like offense), "top 10" means top 10 values
                            rank = rank_num
                    else:  # negative terms
                        if config["ranking_direction"] == "ascending":
                            # For ascending stats, "worst 10" means top 10 values
                            rank = rank_num
                        else:
                            # For descending stats, "worst 10" means bottom 10 values
                            rank = -rank_num
                    
                    filters.append((config["api_filters"][0], rank))
            except ValueError:
                continue
        
        return filters
    
    def _extract_players_on_off(self, query: str, main_player: Optional[str]) -> Tuple[List[str], List[str]]:
        """Extract players that should be on court (with) or off court (without) using spaCy syntax
        
        NOTE: This method is no longer used in the main parse() flow to avoid redundant processing.
        The main parse() method calls _extract_players_with_syntax() directly.
        This method is kept for backwards compatibility and testing purposes.
        """
        doc = self.nlp(query)
        _, players_on, players_off = self._extract_players_with_syntax(query, doc)
        
        # Remove main player from on/off lists if present
        if main_player:
            players_on = [p for p in players_on if p != main_player]
            players_off = [p for p in players_off if p != main_player]
        
        return players_on, players_off
    
    def _extract_players_with_syntax(self, query: str, doc) -> Tuple[Optional[str], List[str], List[str]]:
        """
        Enhanced hybrid approach: First extract all players using alias matching,
        then use spaCy to understand their grammatical relationships
        
        Returns: (main_player, players_on, players_off)
        """
        # STEP 1: Extract all possible players from the query using our proven alias matching
        all_players_found = []
        
        # Find all player matches with positions
        found_matches = []
        query_lower = query.lower()
        
        # Short abbreviations with word boundary matching
        short_abbrevs = ['ad', 'kd', 'cp3', 'pg', 'jt', 'sga']
        for abbrev in short_abbrevs:
            if abbrev in self.player_aliases:
                pattern = r'\b' + re.escape(abbrev) + r'\b'
                matches = list(re.finditer(pattern, query_lower))
                for match in matches:
                    found_matches.append({
                        'alias': abbrev,
                        'player': self.player_aliases[abbrev],
                        'position': match.start(),
                        'end_position': match.end(),
                        'type': 'short_abbrev'
                    })
        
        # All other aliases (substring matching) - prioritize longer matches
        alias_list = sorted(self.player_aliases.keys(), key=len, reverse=True)
        for alias in alias_list:
            if alias not in short_abbrevs and alias in query_lower:
                start_pos = query_lower.find(alias)
                found_matches.append({
                    'alias': alias,
                    'player': self.player_aliases[alias],
                    'position': start_pos,
                    'end_position': start_pos + len(alias),
                    'type': 'alias'
                })
        
        # Try full name matching for players not found by aliases
        if not found_matches:
            for player in self.players:
                player_lower = player.lower()
                if player_lower in query_lower:
                    start_pos = query_lower.find(player_lower)
                    found_matches.append({
                        'alias': player_lower,
                        'player': player,
                        'position': start_pos,
                        'end_position': start_pos + len(player_lower),
                        'type': 'full_name'
                    })
        
        # Sort by position and remove overlaps, prioritizing longer matches
        found_matches.sort(key=lambda x: (x['position'], -len(x['alias'])))
        unique_players = []
        for match in found_matches:
            # Check for overlaps with existing matches
            overlap = False
            for existing in unique_players:
                if (match['position'] < existing['end_position'] and 
                    match['end_position'] > existing['position']):
                    # If there's an overlap, keep the longer match
                    if len(match['alias']) > len(existing['alias']):
                        # Remove the shorter existing match
                        unique_players.remove(existing)
                        break
                    else:
                        overlap = True
                        break
            
            if not overlap:
                unique_players.append(match)
        if not unique_players:
            # Last resort: fuzzy matching fallback
            main_player = self._extract_single_player_name(query, context="spacy_fallback")
            if main_player:
                    # spaCy fallback found a player
                pass
            return main_player, [], []
        
        # STEP 2: Use pattern matching to determine relationships
        players_on = []
        players_off = []
        main_player = None
        
        # Enhanced pattern matching for WITH relationships
        with_patterns = [
            r'\bwith\s+([^,]+?)(?:\s+(?:playing|on\s+court|on\s+the\s+court|in\s+the\s+lineup|in\s+the\s+game))?(?:\s|$|,)',
            r'\bwith\s+(.+?)(?:\s+(?:playing|on\s+court|on\s+the\s+court|in\s+the\s+lineup|in\s+the\s+game))?$',
            r'\bwith\s+(.+?)(?:,|$)',
            r'\bwhen\s+([^,]+?)\s+(?:plays|is\s+playing|on\s+court)(?:\s|$|,)',
            r'\balongside\s+(.+?)(?:\s|$|,)',
            r'\bwhen\s+([^,]+?)\s+(?:is\s+)?(?:in|active|available)(?:\s|$|,)',
            r'\bwith\s+([^,]+?)\s+(?:in\s+the\s+)?(?:lineup|game)(?:\s|$|,)',
        ]
        
        # Enhanced pattern matching for WITHOUT relationships  
        without_patterns = [
            r'\bwithout\s+([^,]+?)(?:\s+(?:playing|on\s+court|on\s+the\s+court))?(?:\s|$|,)',
            r'\bwhen\s+([^,]+?)\s+(?:sits|is\s+out|is\s+sitting|doesn\'t\s+play)(?:\s|$|,)',
            r'\bwhen\s+([^,]+?)\s+(?:is\s+)?(?:out|inactive|unavailable|injured)(?:\s|$|,)',
            r'\bwithout\s+([^,]+?)\s+(?:in\s+the\s+)?(?:lineup|game)(?:\s|$|,)',
            r'\bminus\s+([^,]+?)(?:\s|$|,)',
        ]
        
        # Find WITH players
        with_matches = []
        for pattern in with_patterns:
            matches = re.finditer(pattern, query_lower)
            for match in matches:
                player_text = match.group(1).strip()
                start_pos = match.start(1)
                end_pos = match.end(1)
                found_players = self._extract_multiple_players(player_text)
                for player in found_players:
                    with_matches.append({
                        'player': player,
                        'text': player_text,
                        'start': start_pos,
                        'end': end_pos,
                        'length': len(player_text)
                    })
        
        # Remove overlapping matches, prioritizing longer ones
        with_matches.sort(key=lambda x: (-x['length'], x['start']))  # Sort by length desc, then position
        filtered_with = []
        for match in with_matches:
            # Check if this match overlaps with any already accepted match
            overlap = False
            for existing in filtered_with:
                if (match['start'] < existing['end'] and match['end'] > existing['start']):
                    overlap = True
                    break
            if not overlap:
                filtered_with.append(match)
                players_on.append(match['player'])
        
        # Find WITHOUT players
        without_matches = []
        for pattern in without_patterns:
            matches = re.finditer(pattern, query_lower)
            for match in matches:
                player_text = match.group(1).strip()
                start_pos = match.start(1)
                end_pos = match.end(1)
                found_players = self._extract_multiple_players(player_text)
                for player in found_players:
                    without_matches.append({
                        'player': player,
                        'text': player_text,
                        'start': start_pos,
                        'end': end_pos,
                        'length': len(player_text)
                    })
        
        # Remove overlapping matches, prioritizing longer ones
        without_matches.sort(key=lambda x: (-x['length'], x['start']))  # Sort by length desc, then position
        filtered_without = []
        for match in without_matches:
            # Check if this match overlaps with any already accepted match
            overlap = False
            for existing in filtered_without:
                if (match['start'] < existing['end'] and match['end'] > existing['start']):
                    overlap = True
                    break
            if not overlap:
                filtered_without.append(match)
                players_off.append(match['player'])
        
        # STEP 3: Determine main player
        all_companion_players = set(players_on + players_off)
        available_main_players = [p for p in unique_players if p['player'] not in all_companion_players]
        
        if available_main_players:
            # Use the first player not mentioned in WITH/WITHOUT context
            main_player = available_main_players[0]['player']
        elif unique_players:
            # Fallback: use the first player mentioned
            main_player = unique_players[0]['player']
        
        # Remove duplicates and clean up
        players_on = list(dict.fromkeys(players_on))
        players_off = list(dict.fromkeys(players_off))
        
        # Remove main player from companions if accidentally included
        if main_player:
            players_on = [p for p in players_on if p != main_player]
            players_off = [p for p in players_off if p != main_player]
        
        # Debug output
        if main_player or players_on or players_off:
            # spaCy parsing completed successfully
            pass
        
        print(main_player)
        print(players_on)
        print(players_off)
        
        return main_player, players_on, players_off
    
    def _extract_multiple_players(self, text: str) -> List[str]:
        """Extract multiple player names from text separated by 'and', commas, etc."""
        if not text or len(text.strip()) < 2:
            return []
        
        # Split on common separators
        separators = [
            r'\s+and\s+',      # " and "
            r'\s*,\s*',        # ", " or ","
            r'\s*&\s*',        # " & " or "&"
            r'\s*\+\s*',       # " + " or "+"
        ]
        
        # Split the text using all separators
        parts = [text]
        for separator in separators:
            new_parts = []
            for part in parts:
                new_parts.extend(re.split(separator, part, flags=re.IGNORECASE))
            parts = new_parts
        
        # Extract player names from each part
        player_names = []
        for part in parts:
            part = part.strip()
            if part:
                player_name = self._extract_single_player_name(part, context="players_on_off")
                if player_name:
                    player_names.append(player_name)
        
        return player_names
    
    def _classify_intent(self, query: str, components: QueryComponents) -> Optional[str]:
        """Classify the intent of the query"""
        query_lower = query.lower()
        
        # Check each intent pattern
        for intent, patterns in self.intent_patterns.items():
            for pattern in patterns:
                if re.search(pattern, query_lower):
                    return intent
        
        # Default to game_logs if player found
        if components.player_name:
            return "game_logs"
        
        return None
    
    def _calculate_confidence(self, components: QueryComponents) -> float:
        """Calculate confidence score for the parsed query"""
        confidence = 0.0
        
        # Player identification is crucial
        if components.player_name:
            confidence += 0.4
        
        # Intent classification
        if components.intent:
            confidence += 0.2
        
        # Time period understanding
        if components.time_period or components.game_count:
            confidence += 0.2
        
        # Filter understanding
        if components.opponent_filters:
            confidence += 0.1
        
        # Location understanding
        if components.location:
            confidence += 0.1
        
        # Players on/off understanding
        if components.players_on or components.players_off:
            confidence += 0.1
        
        return min(confidence, 1.0)  # Cap at 1.0


if __name__ == "__main__":
    # NBA Natural Language Query Parser - Base Implementation
    pass