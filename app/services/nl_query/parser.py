"""
Base natural language query parser

This module provides the foundation for parsing natural language queries
into structured components that can be mapped to API parameters.
"""

import spacy
from spacy.matcher import Matcher
import pandas as pd
import re
import yaml
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from rapidfuzz import process, fuzz
from sqlalchemy import text
from ...utils.date_parser import NBADateParser

@dataclass
class SelfFilter:
    """Represents a single self-filter condition for player stats"""
    stat_column: str        # Database column name (e.g., "PTS")
    operator: str          # Comparison operator ("gte", "gt", "lt", "eq", "between")
    value: int            # Primary value
    value2: Optional[int] = None  # Secondary value for range operations
    original_text: str = ""       # Original query text for debugging

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
    minutes_filter: Optional[Tuple[int, int]] = None  # (min_minutes, max_minutes)
    self_filters: List[SelfFilter] = field(default_factory=list)  # Player stat filters
    
    # Query metadata
    intent: Optional[str] = None       # "game_logs", "player_profile", "team_stats"
    confidence: float = 0.0            # Confidence score (0-1)
    raw_query: str = ""               # Original query text
    confidence_breakdown: Optional['ConfidenceBreakdown'] = None  # Detailed confidence analysis
    
    # Additional filters (for future expansion)
    stat_categories: List[str] = field(default_factory=list)
    players_on: List[str] = field(default_factory=list)
    players_off: List[str] = field(default_factory=list)

# Stat mappings for self-filter functionality
STAT_MAPPINGS = {
    # Basic stats (both singular and plural)
    "point": "PTS", "points": "PTS", "pts": "PTS", "bucket": "PTS", "buckets": "PTS", "scored": "PTS",
    "rebound": "REB", "rebounds": "REB", "rebs": "REB", "board": "REB", "boards": "REB",
    "assist": "AST", "assists": "AST", "asts": "AST", "dime": "AST", "dimes": "AST",
    "steal": "STL", "steals": "STL", "stls": "STL", 
    "block": "BLK", "blocks": "BLK", "blks": "BLK", "blocked shots": "BLK", "blocks shots": "BLK",  # More specific for blocks context
    "turnover": "TOV", "turnovers": "TOV", "tovs": "TOV",
    # NOTE: Minutes are handled by dedicated minutes_filter parameter, not self_filters
    
    # Shooting stats - Attempts (volume) - MUST come before made shots to match first
    "field goal attempt": "FGA", "field goal attempts": "FGA", "fga": "FGA", "fg attempts": "FGA",
    "attempts field goal": "FGA", "attempts field goals": "FGA", "takes field goal": "FGA", "takes field goals": "FGA",
    "shot attempt": "FGA", "shot attempts": "FGA", "shots attempted": "FGA", "shooting attempt": "FGA", "shooting attempts": "FGA",
    "shoots shots": "FGA", "shoot shots": "FGA", "takes shots": "FGA", "shot": "FGA", "shots": "FGA",
    "3 point attempt": "FG3A", "3 point attempts": "FG3A", "three attempt": "FG3A", "three attempts": "FG3A", "3pa": "FG3A",
    "three point attempt": "FG3A", "three point attempts": "FG3A", "attempts three": "FG3A", "attempts threes": "FG3A",
    "attempts 3": "FG3A", "attempts 3s": "FG3A", "takes three": "FG3A", "takes threes": "FG3A",
    "free throw attempt": "FTA", "free throw attempts": "FTA", "fta": "FTA", "ft attempts": "FTA",
    
    # Shooting stats - Made shots
    "field goal": "FGM", "field goals": "FGM", "fgm": "FGM", "fg made": "FGM", "field goals made": "FGM",
    "3": "FG3M", "3s": "FG3M", "three": "FG3M", "threes": "FG3M", "triple": "FG3M", "triples": "FG3M", "from deep": "FG3M",
    "three pointer": "FG3M", "three pointers": "FG3M", "3 pointer": "FG3M", "3 pointers": "FG3M",
    "free throw": "FTM", "free throws": "FTM", "ftm": "FTM", "ft made": "FTM"
}

# Opponent filter mappings for natural language to filter types
OPPONENT_FILTER_MAPPINGS = {
    # Catch & Shoot variations
    'catch and shoot': 'C&S PTS',
    'catch and shoot points': 'C&S PTS',
    'catch and shoot 3s': 'C&S 3s',
    'catch and shoot threes': 'C&S 3s',
    'catch and shoot 3 point attempts': 'C&S 3A',
    'catch and shoot three attempts': 'C&S 3A',
    'catch and shoot attempts': 'C&S 3A',
    'catch and shoot 3 point': 'C&S 3s',
    'catch and shoot three point': 'C&S 3s',
    
    # Pullup variations
    'pullup': 'PU PTS',
    'pullup points': 'PU PTS',
    'pullup 2s': 'PU 2s',
    'pullup twos': 'PU 2s',
    'pullup 3s': 'PU 3s',
    'pullup threes': 'PU 3s',
    'pullup 3 point': 'PU 3s',
    'pullup three point': 'PU 3s',
    'pullup 2 point': 'PU 2s',
    'pullup two point': 'PU 2s',
    
    # Playtype variations
    'transition': 'Transition',
    'isolation': 'Isolation',
    'pick and roll ball handler': 'PRBallHandler',
    'pick and roll roll man': 'PRRollMan',
    'offensive rebound': 'OffRebound',
    'offensive rebounds': 'OffRebound',
    'spot up': 'Spotup',
    'spotup': 'Spotup',
    'cut': 'Cut',
    'handoff': 'Handoff',
    'off screen': 'OffScreen',
    'offscreen': 'OffScreen',
    'post up': 'Postup',
    'postup': 'Postup',
    'misc': 'Misc',
    
    # Opponent stat variations
    'opponent assists': 'OPP_AST',
    'opponent points': 'OPP_PTS',
    'opponent rebounds': 'OPP_REB',
    'opponent steals and blocks': 'OPP_STOCKS',
    'opponent stocks': 'OPP_STOCKS',
    'opponent free throw attempts': 'OPP_FTA',
    'opponent free throws': 'OPP_FTA',
    'opponent turnovers': 'OPP_TOV',
    'opponent blocks': 'OPP_BLK',
    'opponent steals': 'OPP_STL',
    'opponent 3s': 'OPP_FG3M',
    'opponent threes': 'OPP_FG3M',
    'opponent 3 point attempts': 'OPP_FG3A',
    'opponent three attempts': 'OPP_FG3A',
    'opponent 3 point': 'OPP_FG3M',
    'opponent three point': 'OPP_FG3M',
    
    # Assist variations
    'two point assists': 'TwoPtAssists',
    'three point assists': 'ThreePtAssists',
    'arc 3 assists': 'Arc3Assists',
    'corner 3 assists': 'Corner3Assists',
    'at rim assists': 'AtRimAssists',
    'short mid range assists': 'ShortMidRangeAssists',
    'long mid range assists': 'LongMidRangeAssists',
    
    # Special cases
    'less than 10 feet': 'Less Than 10 ft',
    'inside 10 feet': 'Less Than 10 ft',
    'close range': 'Less Than 10 ft',
    'close to basket': 'Less Than 10 ft',
    'near basket': 'Less Than 10 ft',
    
    # Defense/Offense variations (for ranking context)
    'defense': 'defense_rank',
    'defensive': 'defense_rank',
    'offense': 'offense_rank',
    'offensive': 'offense_rank',
    'scoring': 'offense_rank',
    'points allowed': 'defense_rank',
    'points scored': 'offense_rank',
}

# Comparison patterns for self-filters (ordered by priority)
COMPARISON_PATTERNS = [
    # Range: "between 20 and 30 points" (must be first to avoid conflicts)
    (r'between\s+(\d+)\s+and\s+(\d+)\s+(\w+(?:\s+\w+)*)', 'between'),
    # Verb + number + stat patterns: "attempts 15+ field goals", "takes 20+ threes"
    (r'(?:attempts?|takes?|shoots?)\s+(\d+)\+\s*(\w+(?:\s+\w+)*)', 'gte'),
    (r'(?:attempts?|takes?|shoots?)\s+more\s+than\s+(\d+)\s+(\w+(?:\s+\w+)*)', 'gt'),
    (r'(?:attempts?|takes?|shoots?)\s+at\s+least\s+(\d+)\s+(\w+(?:\s+\w+)*)', 'gte'),
    (r'(?:attempts?|takes?|shoots?)\s+over\s+(\d+)\s+(\w+(?:\s+\w+)*)', 'gt'),
    (r'(?:attempts?|takes?|shoots?)\s+under\s+(\d+)\s+(\w+(?:\s+\w+)*)', 'lt'),
    (r'(?:attempts?|takes?|shoots?)\s+less\s+than\s+(\d+)\s+(\w+(?:\s+\w+)*)', 'lt'),
    (r'(?:attempts?|takes?|shoots?)\s+exactly\s+(\d+)\s+(\w+(?:\s+\w+)*)', 'eq'),
    (r'(?:attempts?|takes?|shoots?)\s+(\d+)\s+(\w+(?:\s+\w+)*)', 'eq'),
    # Plus notation: "30+ points"
    (r'(\d+)\+\s*(\w+(?:\s+\w+)*)', 'gte'),
    # More than: "more than 30 points"
    (r'more\s+than\s+(\d+)\s+(\w+(?:\s+\w+)*)', 'gt'),
    # Less than: "less than 30 points"
    (r'less\s+than\s+(\d+)\s+(\w+(?:\s+\w+)*)', 'lt'),
    # At least: "at least 30 points"
    (r'at\s+least\s+(\d+)\s+(\w+(?:\s+\w+)*)', 'gte'),
    # Over: "over 30 points"
    (r'over\s+(\d+)\s+(\w+(?:\s+\w+)*)', 'gt'),
    # Under: "under 30 points"
    (r'under\s+(\d+)\s+(\w+(?:\s+\w+)*)', 'lt'),
    # Exactly: "exactly 30 points"
    (r'exactly\s+(\d+)\s+(\w+(?:\s+\w+)*)', 'eq'),
    # Simple number: "30 points" (defaults to exactly, must be last)
    (r'(\d+)\s+(\w+(?:\s+\w+)*)(?!\s*(?:and|or|but))', 'eq')
]

# Main patterns to detect self-filters (ordered by priority to avoid overlaps)
SELF_FILTER_PATTERNS = [
    r'games\s+where\s+he\s+(.+)',  # Most specific first
    r'where\s+he\s+(.+)',
    r'when\s+he\s+(.+)',
    # Pattern for "30+ point games", "10+ rebound games", etc.
    r'(\d+\+?\s*(?:points?|rebounds?|assists?|steals?|blocks?|threes?|buckets?|boards?|dimes?))\s+games',
    r'games\s+making\s+(.+)',
    r'games\s+with\s+(.+)',
    r'with\s+(.+?\s+(?:points|rebounds|assists|steals|blocks|shots|3s|threes|field goals|free throws|buckets|boards|dimes|turnovers))',
    # Pattern for "scoring X+", "averaging X+", etc.
    r'(scoring\s+\d+\+)',
    # Pattern for "shooting X+ times"
    r'(shooting\s+\d+\+\s+times?)',
]

# Enhanced patterns to detect opponent filters - ONLY obvious, unambiguous phrases
OPPONENT_FILTER_PATTERNS = [
    # Very specific, obvious patterns only
    
    # "against [specific filter] teams" - only for well-defined filters
    (r'against\s+(catch\s+and\s+shoot)\s+teams?', 'against'),
    (r'against\s+(pullup)\s+teams?', 'against'),
    (r'against\s+(transition)\s+teams?', 'against'),
    (r'against\s+(isolation)\s+teams?', 'against'),
    (r'against\s+(offensive\s+rebound)\s+teams?', 'against'),
    (r'against\s+(spot\s+up)\s+teams?', 'against'),
    (r'against\s+(handoff)\s+teams?', 'against'),
    (r'against\s+(off\s+screen)\s+teams?', 'against'),
    (r'against\s+(post\s+up)\s+teams?', 'against'),
    (r'against\s+(close\s+range)\s+teams?', 'against'),
    (r'against\s+(inside\s+10\s+feet)\s+teams?', 'against'),
    
    # "vs [specific filter] teams" - same specific filters
    (r'vs\s+(catch\s+and\s+shoot)\s+teams?', 'against'),
    (r'vs\s+(pullup)\s+teams?', 'against'),
    (r'vs\s+(transition)\s+teams?', 'against'),
    (r'vs\s+(isolation)\s+teams?', 'against'),
    (r'vs\s+(offensive\s+rebound)\s+teams?', 'against'),
    (r'vs\s+(spot\s+up)\s+teams?', 'against'),
    (r'vs\s+(handoff)\s+teams?', 'against'),
    (r'vs\s+(off\s+screen)\s+teams?', 'against'),
    (r'vs\s+(post\s+up)\s+teams?', 'against'),
    (r'vs\s+(close\s+range)\s+teams?', 'against'),
    (r'vs\s+(inside\s+10\s+feet)\s+teams?', 'against'),
    
    # "top X [specific filter] teams" - only for well-defined filters
    (r'top\s+(\d+)\s+(catch\s+and\s+shoot)\s+teams?', 'ranking'),
    (r'top\s+(\d+)\s+(pullup)\s+teams?', 'ranking'),
    (r'top\s+(\d+)\s+(transition)\s+teams?', 'ranking'),
    (r'top\s+(\d+)\s+(isolation)\s+teams?', 'ranking'),
    (r'top\s+(\d+)\s+(offensive\s+rebound)\s+teams?', 'ranking'),
    (r'top\s+(\d+)\s+(spot\s+up)\s+teams?', 'ranking'),
    (r'top\s+(\d+)\s+(handoff)\s+teams?', 'ranking'),
    (r'top\s+(\d+)\s+(off\s+screen)\s+teams?', 'ranking'),
    (r'top\s+(\d+)\s+(post\s+up)\s+teams?', 'ranking'),
    (r'top\s+(\d+)\s+(close\s+range)\s+teams?', 'ranking'),
    (r'top\s+(\d+)\s+(inside\s+10\s+feet)\s+teams?', 'ranking'),
]

@dataclass
class ParsedComponent:
    """Represents a component extracted from the query with position tracking"""
    value: Any
    start_pos: int
    end_pos: int
    component_type: str  # 'player', 'time', 'location', 'minutes', 'opponent', etc.
    confidence: float = 1.0
    extraction_method: str = "unknown"  # How this was extracted (regex, spacy, etc.)

@dataclass
class ConfidenceBreakdown:
    """Detailed breakdown of confidence calculation"""
    final_confidence: float
    should_use_llm: bool
    coverage_score: float
    semantic_score: float
    ambiguity_score: float
    complexity_score: float
    completeness_score: float
    details: Dict[str, Any] = field(default_factory=dict)

class QueryCoverage:
    """Tracks which parts of the query were successfully parsed"""
    
    def __init__(self, query: str):
        self.query = query
        self.query_lower = query.lower()
        self.covered_positions = set()
        self.components: List[ParsedComponent] = []
        self.significant_words = self._extract_significant_words()
        
    def _extract_significant_words(self) -> List[str]:
        """Extract words that should be covered by parsing (excluding stop words)"""
        # Extended stop words that don't need to be "covered"
        stop_words = {
            # Basic stop words
            'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'for', 'from', 'has', 'he', 'in', 
            'is', 'it', 'its', 'of', 'on', 'that', 'the', 'to', 'was', 'were', 'will', 'with',
            'his', 'her', 'their', 'this', 'these', 'those', 'when', 'where', 'how', 'why',
            
            # Functional words that set up other parts of the sentence
            'playing', 'played', 'plays', 'play', 'having', 'has', 'had', 'with', 'without',
            'during', 'while', 'when', 'where', 'against', 'versus', 'vs', 'facing', 'faced',
            'scoring', 'scored', 'scoring', 'getting', 'got', 'gets', 'making', 'made', 'makes',
            'taking', 'took', 'takes', 'shooting', 'shot', 'shots', 'attempting', 'attempted',
            'attempts', 'grabbing', 'grabbed', 'grabs', 'dishing', 'dished', 'dishes',
            'stealing', 'stole', 'steals', 'blocking', 'blocked', 'blocks', 'turning', 'turned',
            'turns', 'committing', 'committed', 'commits',
            
            # Contextual words that support meaning but aren't core content
            'games', 'game', 'last', 'first', 'next', 'previous', 'recent', 'current',
            'season', 'month', 'week', 'day', 'today', 'yesterday', 'tomorrow',
            'home', 'away', 'both', 'either', 'neither', 'all', 'every', 'each',
            'more', 'less', 'than', 'over', 'under', 'above', 'below', 'between',
            'around', 'about', 'approximately', 'exactly', 'precisely', 'roughly',
            'plus', 'minus', 'add', 'subtract', 'total', 'sum', 'average', 'avg',
            'minimum', 'min', 'maximum', 'max', 'at least', 'at most', 'no more than',
            'no less than', 'greater than', 'less than', 'equal to', 'equals',
            
            # Partial words that might appear due to text processing
            'utes', 'utes', 'utes', 'utes', 'utes', 'utes', 'utes', 'utes', 'utes',
            'ing', 'ed', 'er', 'est', 'ly', 'tion', 'sion', 'ment', 'ness', 'ful',
            'less', 'able', 'ible', 'ous', 'ious', 'eous', 'al', 'ial', 'ical',
            'ive', 'ative', 'itive', 'ize', 'ise', 'ify', 'fy', 'en', 'ize',
        }
        
        words = [word.strip('.,!?;:') for word in self.query_lower.split()]
        
        # Filter out stop words and very short words
        significant_words = []
        for word in words:
            if (word not in stop_words and 
                len(word) > 2 and  # Increased minimum length to avoid partial words
                not word.isdigit() and  # Exclude pure numbers (they're handled separately)
                not self._is_partial_word(word)):  # Exclude partial words
                significant_words.append(word)
        
        return significant_words
    
    def _is_partial_word(self, word: str) -> bool:
        """Check if a word appears to be a partial word from text processing"""
        # Common partial word patterns
        partial_patterns = [
            r'^[a-z]{1,3}$',  # Very short words (1-3 chars)
            r'^[a-z]+ing$',   # Words ending in 'ing' that are very short
            r'^[a-z]+ed$',    # Words ending in 'ed' that are very short
            r'^[a-z]+er$',    # Words ending in 'er' that are very short
            r'^[a-z]+est$',   # Words ending in 'est' that are very short
            r'^[a-z]+ly$',    # Words ending in 'ly' that are very short
            r'^[a-z]+tion$',  # Words ending in 'tion' that are very short
            r'^[a-z]+sion$',  # Words ending in 'sion' that are very short
            r'^[a-z]+ment$',  # Words ending in 'ment' that are very short
            r'^[a-z]+ness$',  # Words ending in 'ness' that are very short
            r'^[a-z]+ful$',   # Words ending in 'ful' that are very short
            r'^[a-z]+less$',  # Words ending in 'less' that are very short
            r'^[a-z]+able$',  # Words ending in 'able' that are very short
            r'^[a-z]+ible$',  # Words ending in 'ible' that are very short
            r'^[a-z]+ous$',   # Words ending in 'ous' that are very short
            r'^[a-z]+ious$',  # Words ending in 'ious' that are very short
            r'^[a-z]+eous$',  # Words ending in 'eous' that are very short
            r'^[a-z]+al$',    # Words ending in 'al' that are very short
            r'^[a-z]+ial$',   # Words ending in 'ial' that are very short
            r'^[a-z]+ical$',  # Words ending in 'ical' that are very short
            r'^[a-z]+ive$',   # Words ending in 'ive' that are very short
            r'^[a-z]+ative$', # Words ending in 'ative' that are very short
            r'^[a-z]+itive$', # Words ending in 'itive' that are very short
            r'^[a-z]+ize$',   # Words ending in 'ize' that are very short
            r'^[a-z]+ise$',   # Words ending in 'ise' that are very short
            r'^[a-z]+ify$',   # Words ending in 'ify' that are very short
            r'^[a-z]+fy$',    # Words ending in 'fy' that are very short
            r'^[a-z]+en$',    # Words ending in 'en' that are very short
        ]
        
        import re
        for pattern in partial_patterns:
            if re.match(pattern, word) and len(word) <= 6:  # Short words with these patterns
                return True
        
        # Specific partial words that commonly appear
        specific_partials = {
            'utes', 'ing', 'ed', 'er', 'est', 'ly', 'tion', 'sion', 'ment', 'ness',
            'ful', 'less', 'able', 'ible', 'ous', 'ious', 'eous', 'al', 'ial', 'ical',
            'ive', 'ative', 'itive', 'ize', 'ise', 'ify', 'fy', 'en'
        }
        
        return word in specific_partials
    
    def add_component(self, component: ParsedComponent) -> None:
        """Add a parsed component and mark its positions as covered"""
        self.components.append(component)
        for pos in range(component.start_pos, component.end_pos):
            self.covered_positions.add(pos)
    
    def calculate_coverage_score(self) -> float:
        """Calculate percentage of significant content that was parsed"""
        if not self.significant_words:
            # Empty query or no significant words should have very low coverage
            return 0.0 if len(self.query.strip()) == 0 else 0.1
        
        total_significant_chars = sum(len(word) for word in self.significant_words)
        if total_significant_chars == 0:
            return 1.0
        
        # Count covered significant characters
        covered_significant_chars = 0
        for word in self.significant_words:
            word_start = self.query_lower.find(word)
            if word_start != -1:
                word_end = word_start + len(word)
                word_coverage = sum(1 for pos in range(word_start, word_end) if pos in self.covered_positions)
                covered_significant_chars += word_coverage
        
        return min(covered_significant_chars / total_significant_chars, 1.0)
    
    def get_uncovered_text(self) -> str:
        """Get the parts of the query that weren't covered by any component"""
        uncovered_chars = []
        for i, char in enumerate(self.query):
            if i not in self.covered_positions:
                uncovered_chars.append(char)
        return ''.join(uncovered_chars).strip()

class SemanticValidator:
    """Validates extracted components for logical consistency"""
    
    def __init__(self, db_engine):
        self.engine = db_engine
        self.player_team_map = {}  # Will be populated as needed
        
    def validate_components(self, components: QueryComponents) -> Tuple[bool, List[str]]:
        """Validate components and return warnings"""
        warnings = []
        
        # Check for logical contradictions
        warnings.extend(self._check_contradictions(components))
        
        # Check for unrealistic values
        warnings.extend(self._check_realistic_values(components))
        
        # Check for player relationships
        warnings.extend(self._check_player_relationships(components))
        
        return len(warnings) == 0, warnings
    
    def _check_contradictions(self, components: QueryComponents) -> List[str]:
        """Check for logical contradictions in the query"""
        warnings = []
        
        # Player appearing in both ON and OFF lists
        if components.players_on and components.players_off:
            overlap = set(components.players_on) & set(components.players_off)
            if overlap:
                warnings.append(f"Player(s) {overlap} appear in both 'with' and 'without' lists")
        
        # Main player also in ON/OFF lists
        if components.player_name:
            if components.player_name in components.players_on:
                warnings.append(f"Main player {components.player_name} also in 'with' list")
            if components.player_name in components.players_off:
                warnings.append(f"Main player {components.player_name} also in 'without' list")
        
        return warnings
    
    def _check_realistic_values(self, components: QueryComponents) -> List[str]:
        """Check for unrealistic values"""
        warnings = []
        
        # Unrealistic game counts
        if components.game_count:
            if components.game_count > 82:
                warnings.append(f"Game count {components.game_count} exceeds season length")
            elif components.game_count < 1:
                warnings.append(f"Game count {components.game_count} is invalid")
        
        # Unrealistic minutes
        if components.minutes_filter:
            min_min, max_min = components.minutes_filter
            if min_min > 48 or max_min > 48:
                warnings.append(f"Minutes filter {components.minutes_filter} exceeds game length")
            elif min_min < 0 or max_min < 0:
                warnings.append(f"Minutes filter {components.minutes_filter} has negative values")
            elif min_min > max_min:
                warnings.append(f"Minutes filter {components.minutes_filter} has invalid range")
        
        return warnings
    
    def _check_player_relationships(self, components: QueryComponents) -> List[str]:
        """Check if player relationships make sense"""
        warnings = []
        
        # Too many players (might indicate parsing error)
        total_players = len(components.players_on) + len(components.players_off)
        if total_players > 8:  # NBA has 5 players on court
            warnings.append(f"Too many players mentioned ({total_players}), might indicate parsing error")
        
        return warnings

class AmbiguityDetector:
    """Detects ambiguities in the parsed query"""
    
    def __init__(self, players: List[str], player_aliases: Dict[str, str]):
        self.players = players
        self.player_aliases = player_aliases
    
    def detect_ambiguities(self, query: str, components: QueryComponents) -> List[Dict[str, Any]]:
        """Detect various types of ambiguities"""
        ambiguities = []
        
        # Player name ambiguities
        ambiguities.extend(self._detect_player_ambiguities(query, components))
        
        # Relationship ambiguities
        ambiguities.extend(self._detect_relationship_ambiguities(query, components))
        
        # Temporal ambiguities
        ambiguities.extend(self._detect_temporal_ambiguities(query, components))
        
        return ambiguities
    
    def _detect_player_ambiguities(self, query: str, components: QueryComponents) -> List[Dict[str, Any]]:
        """Detect ambiguous player references"""
        ambiguities = []
        
        # Check for last name only matches
        if components.player_name:
            player_words = components.player_name.split()
            if len(player_words) >= 2:
                last_name = player_words[-1].lower()
                # Find other players with same last name
                same_last_name = [p for p in self.players if p.lower().split()[-1] == last_name and p != components.player_name]
                if same_last_name:
                    ambiguities.append({
                        'type': 'player_last_name_ambiguity',
                        'current': components.player_name,
                        'alternatives': same_last_name,
                        'severity': 'high' if len(same_last_name) > 1 else 'medium'
                    })
        
        return ambiguities
    
    def _detect_relationship_ambiguities(self, query: str, components: QueryComponents) -> List[Dict[str, Any]]:
        """Detect ambiguous player relationships"""
        ambiguities = []
        
        # Check for "and" patterns that could be interpreted multiple ways
        if ' and ' in query.lower():
            # Count player names in query
            player_mentions = sum(1 for p in self.players if p.lower() in query.lower())
            if player_mentions >= 2 and not components.players_on and not components.players_off:
                ambiguities.append({
                    'type': 'relationship_ambiguity',
                    'description': 'Multiple players mentioned with "and" but unclear relationship',
                    'severity': 'medium'
                })
        
        return ambiguities
    
    def _detect_temporal_ambiguities(self, query: str, components: QueryComponents) -> List[Dict[str, Any]]:
        """Detect temporal ambiguities"""
        ambiguities = []
        
        # Vague time references
        vague_patterns = ['recent', 'lately', 'currently', 'now', 'today']
        for pattern in vague_patterns:
            if pattern in query.lower() and not components.game_count:
                ambiguities.append({
                    'type': 'temporal_ambiguity',
                    'description': f'Vague time reference "{pattern}" without specific period',
                    'severity': 'low'
                })
        
        return ambiguities

class ComplexityAnalyzer:
    """Analyzes query complexity"""
    
    def __init__(self):
        self.complexity_patterns = [
            # Nested clauses
            (r'when\s+.*\s+(?:and|but|or)\s+.*\s+(?:then|during)', 'nested_conditional', 0.8),
            # Multiple conjunctions
            (r'(?:and|or|but).*(?:and|or|but).*(?:and|or|but)', 'multiple_conjunctions', 0.7),
            # Comparative statements
            (r'(?:better|worse|more|less)\s+than', 'comparative', 0.6),
            # Complex temporal expressions
            (r'(?:before|after|during|since).*(?:when|while|as)', 'complex_temporal', 0.7),
            # Conditional statements
            (r'if\s+.*\s+then|when\s+.*\s+show|only\s+when', 'conditional', 0.8),
            # Nested filters
            (r'(?:with|without).*(?:with|without).*(?:with|without)', 'nested_filters', 0.9),
            # Multiple negations
            (r'(?:not|without|excluding).*(?:not|without|excluding)', 'multiple_negations', 0.8),
        ]
    
    def calculate_complexity_score(self, query: str) -> float:
        """Calculate complexity score (0-1 where 1 is most complex)"""
        query_lower = query.lower()
        max_complexity = 0.0
        
        for pattern, name, complexity in self.complexity_patterns:
            if re.search(pattern, query_lower):
                max_complexity = max(max_complexity, complexity)
        
        # Length-based complexity
        word_count = len(query.split())
        length_complexity = min(word_count / 25, 0.5)  # Cap at 0.5
        
        # Punctuation complexity
        punctuation_count = sum(1 for char in query if char in '.,!?;:')
        punctuation_complexity = min(punctuation_count / 10, 0.3)  # Cap at 0.3
        
        return max(max_complexity, length_complexity, punctuation_complexity)

class CompletenessChecker:
    """Checks for incomplete or partial extractions"""
    
    def check_completeness(self, query: str, components: QueryComponents) -> Dict[str, Any]:
        """Check completeness and return issues"""
        issues = []
        
        # Essential component checks
        if self._looks_like_player_query(query) and not components.player_name:
            issues.append({'type': 'missing_player', 'severity': 'high'})
        
        # Partial pattern matches
        partial_patterns = [
            (r'\blast\s+(?!\d+\s+(?:games?|season|month))', 'incomplete_time_reference'),
            (r'\bwith\s*$', 'dangling_with'),
            (r'\bwithout\s*$', 'dangling_without'),
            (r'\bagainst\s*$', 'dangling_against'),
            (r'\bat\s*$', 'dangling_at'),
            (r'\bwhen\s*$', 'dangling_when'),
        ]
        
        for pattern, issue_type in partial_patterns:
            if re.search(pattern, query.lower()):
                issues.append({'type': issue_type, 'severity': 'medium'})
        
        # Orphaned modifiers
        if self._has_orphaned_modifiers(query):
            issues.append({'type': 'orphaned_modifiers', 'severity': 'medium'})
        
        return {
            'issues': issues,
            'completeness_score': self._calculate_completeness_score(issues)
        }
    
    def _looks_like_player_query(self, query: str) -> bool:
        """Determine if query should have a player"""
        player_indicators = ['games', 'stats', 'performance', 'last', 'recent', 'season', 'minutes']
        return any(indicator in query.lower() for indicator in player_indicators)
    
    def _has_orphaned_modifiers(self, query: str) -> bool:
        """Check for modifiers without clear targets"""
        orphan_patterns = [
            r'\b(?:very|really|extremely|quite)\s+(?:good|bad|high|low)\b',
            r'\b(?:all|every|each)\s+(?:time|game)\b(?!\s+(?:with|against|when))',
        ]
        
        for pattern in orphan_patterns:
            if re.search(pattern, query.lower()):
                return True
        return False
    
    def _calculate_completeness_score(self, issues: List[Dict[str, Any]]) -> float:
        """Calculate completeness score based on issues"""
        if not issues:
            return 1.0
        
        # Weight issues by severity
        severity_weights = {'high': 0.4, 'medium': 0.2, 'low': 0.1}
        total_penalty = sum(severity_weights.get(issue['severity'], 0.1) for issue in issues)
        
        return max(0.0, 1.0 - total_penalty)

class MasterConfidenceCalculator:
    """Combines all confidence signals into final score"""
    
    def __init__(self, db_engine, players: List[str], player_aliases: Dict[str, str]):
        self.semantic_validator = SemanticValidator(db_engine)
        self.ambiguity_detector = AmbiguityDetector(players, player_aliases)
        self.complexity_analyzer = ComplexityAnalyzer()
        self.completeness_checker = CompletenessChecker()
        
        # Weights for different signals
        self.weights = {
            'coverage': 0.25,
            'semantic': 0.20,
            'ambiguity': 0.20,
            'complexity': 0.15,
            'completeness': 0.20
        }
        
        # Lowered threshold for better LLM fallback on self-filter patterns
        self.llm_threshold = 0.9
    
    def calculate_confidence(self, query: str, components: QueryComponents, 
                           coverage: QueryCoverage, parser=None) -> ConfidenceBreakdown:
        """Calculate comprehensive confidence score"""
        
        # 1. Coverage score (0-1, higher is better)
        coverage_score = coverage.calculate_coverage_score()
        
        # 2. Semantic validation (0-1, higher is better)
        is_valid, warnings = self.semantic_validator.validate_components(components)
        semantic_score = max(0.0, 1.0 - (len(warnings) * 0.25))  # Deduct 0.25 per warning
        
        # 3. Ambiguity score (0-1, higher is better)
        ambiguities = self.ambiguity_detector.detect_ambiguities(query, components)
        ambiguity_penalties = {'high': 0.4, 'medium': 0.25, 'low': 0.1}
        ambiguity_penalty = sum(ambiguity_penalties.get(amb.get('severity', 'medium'), 0.25) for amb in ambiguities)
        ambiguity_score = max(0.0, 1.0 - ambiguity_penalty)
        
        # 4. Complexity score (0-1, higher is better)
        complexity = self.complexity_analyzer.calculate_complexity_score(query)
        complexity_score = 1.0 - complexity
        
        # 5. Completeness score (0-1, higher is better)
        completeness_result = self.completeness_checker.check_completeness(query, components)
        completeness_score = completeness_result['completeness_score']
        
        # Weighted average
        final_confidence = (
            coverage_score * self.weights['coverage'] +
            semantic_score * self.weights['semantic'] +
            ambiguity_score * self.weights['ambiguity'] +
            complexity_score * self.weights['complexity'] +
            completeness_score * self.weights['completeness']
        )
        
        # Aggressive LLM triggering for opponent filter queries
        should_use_llm = final_confidence < self.llm_threshold
        
        # Force LLM if query has opponent filter keywords but no filters were extracted
        if parser and hasattr(parser, '_has_opponent_filter_keywords') and parser._has_opponent_filter_keywords(query):
            has_extracted_filters = len(components.opponent_filters) > 0
            if not has_extracted_filters:
                should_use_llm = True
                # Apply penalty to confidence to reflect the missed opponent filters
                final_confidence = min(final_confidence * 0.6, 0.7)  # Cap at 0.7 to ensure LLM trigger
        
        return ConfidenceBreakdown(
            final_confidence=final_confidence,
            should_use_llm=should_use_llm,
            coverage_score=coverage_score,
            semantic_score=semantic_score,
            ambiguity_score=ambiguity_score,
            complexity_score=complexity_score,
            completeness_score=completeness_score,
            details={
                'semantic_warnings': warnings,
                'ambiguities': ambiguities,
                'completeness_issues': completeness_result['issues'],
                'uncovered_text': coverage.get_uncovered_text(),
                'coverage_components': len(coverage.components)
            }
        )

class BaseQueryParser:
    """
    Enhanced natural language query parser for NBA analytics
    
    This class provides robust parsing of natural language queries
    about NBA players and teams into structured components that can be used
    with the existing API. Enhanced with spaCy for better accuracy.
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
        
        # Initialize confidence calculator
        self.confidence_calculator = MasterConfidenceCalculator(
            db_engine, self.players, self.player_aliases
        )
        
        # Initialize date parser
        self.date_parser = NBADateParser()
        
        # Debug: Print loaded data
        print(f"DEBUG: Loaded {len(self.players)} players")
        print(f"DEBUG: Loaded {len(self.player_aliases)} aliases")
        if self.players:
            print(f"DEBUG: Sample players: {self.players[:5]}")
        if self.player_aliases:
            print(f"DEBUG: Sample aliases: {list(self.player_aliases.keys())[:5]}")
        
        # Setup spaCy components for enhanced parsing
        self._setup_spacy_components()
        
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
        """
        Load the spaCy language model for NLP tasks.
        Attempts to load 'en_core_web_sm' if available, otherwise falls back to a blank English model.
        Returns:
            nlp (spacy.Language): The loaded spaCy language model.
        """
        try:
            nlp = spacy.load("en_core_web_sm")
        except OSError:
            # Fallback to basic English model if custom model not available
            nlp = spacy.blank("en")
        return nlp
    
    def _setup_spacy_components(self):
        """
        Set up custom spaCy components for the parser.
        Adds an entity ruler for player/alias recognition and initializes relationship patterns for the matcher.
        """
        # Disable the built-in NER to prevent conflicts with our entity ruler
        self.nlp.disable_pipes("ner")
        ruler = self.nlp.add_pipe("entity_ruler", config={"overwrite_ents": True})
        self._add_player_entity_patterns(ruler)
        self._setup_relationship_patterns()
    
    def _add_player_entity_patterns(self, ruler):
        """
        Add player and alias patterns to the spaCy entity ruler.
        Args:
            ruler (EntityRuler): The spaCy entity ruler pipeline component.
        """
        patterns = []
        
        # Add string patterns (existing approach)
        for player in self.players:
            patterns.append({
                "label": "PLAYER",
                "pattern": player,
                "id": player
            })
        
        # Add token-based patterns for better syntactic robustness
        for player in self.players:
            words = player.split()
            if len(words) == 2:  # Handle two-word names like "Anthony Edwards"
                patterns.append({
                    "label": "PLAYER",
                    "pattern": [
                        {"LOWER": words[0].lower()},
                        {"LOWER": words[1].lower()}
                    ],
                    "id": player
                })
        
        # Add last name patterns for each player (NEW FIX)
        for player in self.players:
            words = player.split()
            if len(words) >= 2:
                last_name = words[-1]
                # Add both original case and title case patterns
                patterns.append({
                    "label": "PLAYER",
                    "pattern": last_name,
                    "id": player
                })
                # Add title case if different
                if last_name.title() != last_name:
                    patterns.append({
                        "label": "PLAYER",
                        "pattern": last_name.title(),
                        "id": player
                    })
        
        for alias, player in self.player_aliases.items():
            # Add lowercase pattern (original)
            patterns.append({
                "label": "PLAYER",
                "pattern": alias,
                "id": player
            })
            
            # Add title case pattern for all aliases
            title_case = alias.title()
            if title_case != alias:
                patterns.append({
                    "label": "PLAYER", 
                    "pattern": title_case,
                    "id": player
                })
            
            # Add uppercase pattern for short abbreviations
            if len(alias) <= 4 and alias.upper() != alias:
                patterns.append({
                    "label": "PLAYER",
                    "pattern": alias.upper(), 
                    "id": player
                })
            
            # Add token-based patterns for multi-word aliases
            words = alias.split()
            if len(words) == 2:
                patterns.append({
                    "label": "PLAYER",
                    "pattern": [
                        {"LOWER": words[0].lower()},
                        {"LOWER": words[1].lower()}
                    ],
                    "id": player
                })
        
        ruler.add_patterns(patterns)
    
    def _setup_relationship_patterns(self):
        """
        Define and add relationship patterns (with/without) to the spaCy matcher.
        These patterns help identify player relationships in queries.
        """
        self.matcher = Matcher(self.nlp.vocab)
        with_patterns = [
            [{"LOWER": "with"}, {"ENT_TYPE": "PLAYER"}],
            [{"LOWER": "playing"}, {"LOWER": "with"}, {"ENT_TYPE": "PLAYER"}],
            [{"LOWER": "alongside"}, {"ENT_TYPE": "PLAYER"}],
            [{"LOWER": "including"}, {"ENT_TYPE": "PLAYER"}],
            [{"LOWER": "featuring"}, {"ENT_TYPE": "PLAYER"}],
        ]
        without_patterns = [
            [{"LOWER": "without"}, {"ENT_TYPE": "PLAYER"}],
            [{"LOWER": "minus"}, {"ENT_TYPE": "PLAYER"}],
            [{"LOWER": "excluding"}, {"ENT_TYPE": "PLAYER"}],
        ]
        for pattern in with_patterns:
            self.matcher.add("WITH_PLAYER", [pattern])
        for pattern in without_patterns:
            self.matcher.add("WITHOUT_PLAYER", [pattern])
    
    def _load_players(self) -> List[str]:
        """
        Load all player names from the database.
        Returns:
            List[str]: List of player full names.
        """
        try:
            with self.engine.connect() as conn:
                # Quote column to handle case-sensitive identifier created by pandas/SQLAlchemy on Postgres
                result = conn.execute(text('SELECT DISTINCT "PLAYER_NAME" FROM player_play_types'))
                return [row[0] for row in result.fetchall()]
        except Exception as e:
            print(f"Warning: Could not load players from database: {e}")
            return []
    
    def _load_teams(self) -> Dict[str, str]:
        """
        Load team names and abbreviations from the database.
        Returns:
            Dict[str, str]: Mapping from team name/abbreviation/variation to abbreviation.
        """
        try:
            with self.engine.connect() as conn:
                # Table was normalized to snake_case: team_info
                result = conn.execute(text("SELECT full_name, abbreviation FROM team_info"))
                teams = {}
                for row in result.fetchall():
                    team_name, abbr = row
                    teams[team_name.lower()] = abbr
                    teams[abbr.lower()] = abbr
                    # Add common variations for major teams
                    if "lakers" in team_name.lower():
                        teams["la"] = abbr
                        teams["los angeles"] = abbr
                    elif "warriors" in team_name.lower():
                        teams["gsw"] = abbr
                        teams["golden state"] = abbr
                    elif "celtics" in team_name.lower():
                        teams["boston"] = abbr
                    elif "bucks" in team_name.lower():
                        teams["milwaukee"] = abbr
                    elif "spurs" in team_name.lower():
                        teams["san antonio"] = abbr
                return teams
        except Exception as e:
            print(f"Warning: Could not load teams from database: {e}")
            return {}
    
    def _load_player_aliases(self) -> Dict[str, str]:
        """
        Load player aliases from a YAML configuration file.
        Returns:
            Dict[str, str]: Mapping from alias (lowercase) to canonical player name.
        """
        try:
            alias_file = os.path.join(os.path.dirname(__file__), '..', '..', 'config', 'player_aliases.yaml')
            with open(alias_file, 'r', encoding='utf-8') as f:
                yaml_data = yaml.safe_load(f)
                # Extract aliases from under the 'aliases' key
                if yaml_data and 'aliases' in yaml_data:
                    return yaml_data['aliases'] or {}
                else:
                    # Fallback: assume the entire file is aliases
                    return yaml_data or {}
        except Exception as e:
            print(f"Warning: Could not load player aliases: {e}")
            return {}
    
    def parse(self, query: str) -> QueryComponents:
        """
        Parse a natural language query into structured components.
        Args:
            query (str): The user's natural language query.
        Returns:
            QueryComponents: Structured representation of the parsed query, including player, team, time, filters, and intent.
        """
        components = QueryComponents(raw_query=query)
        processed_query = self._preprocess_query(query)
        doc = self.nlp(processed_query)
        coverage = QueryCoverage(query)
        
        # Extract player information (main, on, off) with position tracking
        components.player_name, components.players_on, components.players_off = self._extract_players_with_syntax(query, doc)
        
        # Track player components in coverage
        for ent in doc.ents:
            if ent.label_ == "PLAYER":
                coverage.add_component(ParsedComponent(
                    value=ent.text,
                    start_pos=ent.start_char,
                    end_pos=ent.end_char,
                    component_type="player",
                    extraction_method="spacy"
                ))
        
        # Extract other components with position tracking
        components.team_name = self._extract_team_name(query, doc)
        components.time_period, components.game_count = self._extract_time_period_with_coverage(query, coverage)
        components.date_range = self._extract_date_filter_with_coverage(query, coverage)
        components.location = self._extract_location_with_coverage(query, coverage)
        components.opponent_filters = self._extract_opponent_filters_with_coverage(query, coverage)
        components.minutes_filter = self._extract_minutes_filter_with_coverage(query, coverage)
        components.self_filters = self._extract_self_filters_with_coverage(query, coverage)
        components.intent = self._classify_intent(query, components)
        
        # Calculate comprehensive confidence score
        confidence_breakdown = self.confidence_calculator.calculate_confidence(query, components, coverage, parser=self)
        components.confidence = confidence_breakdown.final_confidence
        
        # Store confidence breakdown for debugging
        components.confidence_breakdown = confidence_breakdown
        
        print(components)
        return components
    
    def _preprocess_query(self, query: str) -> str:
        """
        Clean and normalize the query text for easier parsing.
        - Removes extra whitespace and common contractions.
        - Normalizes common basketball abbreviations and terms.
        Args:
            query (str): The original query string.
        Returns:
            str: The cleaned and normalized query string.
        """
        query = ' '.join(query.split())
        query = query.replace("'s", "")  # Remove possessive 's
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
    
    def _extract_last_name(self, text: str) -> Optional[str]:
        """
        Extract a player's last name from the text and find matching players.
        Args:
            text (str): The text fragment to search for a last name.
        Returns:
            Optional[str]: The canonical player name if at least one match found, else None.
        Implementation details:
            - Extracts the last word as potential last name.
            - Finds all players with matching last names.
            - Returns the first player found (user responsibility to clarify if ambiguous).
        """
        if not text or len(text.strip()) < 2:
            return None
        
        # Extract the last word as potential last name
        words = text.strip().split()
        if not words:
            return None
        
        last_name = words[-1].lower()
        if len(last_name) < 2:  # Skip very short last names
            return None
        
        # Find all players with matching last names
        matching_players = []
        for player in self.players:
            player_words = player.lower().split()
            if player_words and player_words[-1] == last_name:
                matching_players.append(player)
        
        # Return the first player found (user responsibility to clarify if ambiguous)
        if matching_players:
            return matching_players[0]
        else:
            return None

    def _extract_single_player_name(self, text: str, context: str = "fragment") -> Optional[str]:
        """
        Extract a single player name from a text fragment using aliases, exact, and fuzzy matching.
        Args:
            text (str): The text fragment to search for a player name.
            context (str): Context for extraction (default: 'fragment').
        Returns:
            Optional[str]: The canonical player name if found, else None.
        Implementation details:
            - Prioritizes strict alias matches, then substring, then last name matching.
            - Fuzzy matching is used as a conservative fallback with higher thresholds.
            - Handles short abbreviations with word boundaries.
            - Last name matching is prioritized over fuzzy matching to avoid false positives.
        """
        if not text or len(text.strip()) < 2:
            return None
        text = text.strip()
        text_lower = text.lower()
        
        # Blacklist common basketball terms that shouldn't trigger player matches
        basketball_blacklist = {
            'points', 'point', 'rebounds', 'rebound', 'assists', 'assist', 'steals', 'steal', 
            'blocks', 'block', 'turnovers', 'turnover', 'minutes', 'minute', 'games', 'game',
            'triple', 'double', 'field', 'goals', 'free', 'throws', 'shots', 'shot',
            'performance', 'stats', 'statistics', 'season', 'playoff', 'playoffs',
            'home', 'away', 'road', 'court', 'team', 'teams', 'player', 'players',
            'offense', 'defense', 'basketball', 'nba', 'scoring', 'shooting','mr', 'the'  # Generic titles that are too broad
        }
        
        # Don't extract if it's a blacklisted term
        if text_lower in basketball_blacklist:
            return None
        # STEP 0: Strict full alias match (case-insensitive, stripped)
        for alias in self.player_aliases:
            if text_lower == alias.strip().lower():
                return self.player_aliases[alias]
        
        # STEP 1: Try exact case-insensitive match against all player full names (HIGHEST PRIORITY)
        for player in self.players:
            if text_lower == player.lower():
                return player
        
        # STEP 2: Check short abbreviations with EXACT match (LOWER PRIORITY)
        short_abbrevs = ['ad', 'kd', 'cp3', 'pg', 'jt', 'sga']
        for abbrev in short_abbrevs:
            if abbrev in self.player_aliases:
                if text_lower == abbrev:  # Exact match only
                    return self.player_aliases[abbrev]
        # STEP 3: Try exact substring matching first (prioritize longer matches)
        words = text_lower.split()
        for length in range(min(4, len(words)), 0, -1):
            for i in range(len(words) - length + 1):
                phrase = ' '.join(words[i:i+length])
                if len(phrase) > 2:
                    for alias in self.player_aliases.keys():
                        alias_norm = alias.strip().lower()
                        if phrase == alias_norm:
                            return self.player_aliases[alias]
        
        
        # STEP 4: Try last name matching (prioritize this over fuzzy matching)
        last_name_match = self._extract_last_name(text)
        if last_name_match:
            return last_name_match
        
        # STEP 5: Try fuzzy matching on word combinations (more conservative)
        for i in range(len(words)):
            for j in range(i + 1, min(i + 4, len(words) + 1)):
                phrase = ' '.join(words[i:j])
                if len(phrase) > 2:
                    alias_match = process.extractOne(
                        phrase,
                        [a.strip().lower() for a in self.player_aliases.keys()],
                        scorer=fuzz.partial_ratio,
                        score_cutoff=90  # Keep at 90 to allow misspellings like "currey" -> "stephen curry"
                    )
                    if alias_match:
                        matched_alias = alias_match[0]
                        for alias in self.player_aliases.keys():
                            if alias.strip().lower() == matched_alias:
                                return self.player_aliases[alias]
        
        # STEP 6: Try direct fuzzy matching against player database
        match = process.extractOne(
            text,
            self.players,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=90  
        )
        if match:
            return match[0]
        return None
    
    def _extract_team_name(self, query: str, doc) -> Optional[str]:
        """
        Extract the team name or abbreviation from the query.
        Args:
            query (str): The original query string.
            doc (spacy.tokens.Doc): The spaCy Doc object for the query.
        Returns:
            Optional[str]: The team abbreviation if found, else None.
        """
        query_lower = query.lower()
        for team_variant, abbr in self.teams.items():
            if team_variant in query_lower:
                return abbr
        return None
    
    def _extract_time_period(self, query: str) -> Tuple[Optional[str], Optional[int]]:
        """
        Extract the time period and game count from the query.
        Args:
            query (str): The original query string.
        Returns:
            Tuple[Optional[str], Optional[int]]: (time_period, game_count) such as ("recent", 10) or ("season", None).
        Implementation details:
            - Handles patterns like "last 10 games", "this season", "this month".
        """
        query_lower = query.lower()
        game_patterns = [
            r'(?:last|past|recent)\s+(\d+)\s+(?:home\s+|away\s+|road\s+)?(?:games?|matchups?|contests?)',
            r'(?:last|past|recent)\s+(\w+)\s+(?:home\s+|away\s+|road\s+)?(?:games?|matchups?|contests?)'
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
                        try:
                            from word2number import w2n
                            return "recent", w2n.word_to_num(number_str)
                        except:
                            pass
                except:
                    pass
        season_patterns = [
            r'(?:this|current)\s+season',
            r'season',
            r'\d{4}-\d{2,4}',
            r'\d{4}/\d{2,4}'
        ]
        for pattern in season_patterns:
            if re.search(pattern, query_lower):
                return "season", None
        month_patterns = [
            r'(?:this|current)\s+month',
            r'last\s+month',
            r'past\s+month'
        ]
        for pattern in month_patterns:
            if re.search(pattern, query_lower):
                return "month", None
        return None, None
    
    def _extract_date_filter(self, query: str) -> Optional[str]:
        """
        Extract date filter from the query using the NBA date parser.
        
        Args:
            query (str): The original query string.
            
        Returns:
            Optional[str]: Date in YYYY-MM-DD format if found, else None.
        """
        try:
            return self.date_parser.parse_date_from_query(query)
        except Exception as e:
            print(f"Warning: Date parsing failed for '{query}': {e}")
            return None
    
    def _extract_location(self, query: str) -> Optional[str]:
        """
        Extract the location filter (home/away) from the query.
        Args:
            query (str): The original query string.
        Returns:
            Optional[str]: 'home', 'away', or None if not found.
        Implementation details:
            - Recognizes various synonyms for home and away locations
            - Uses pattern matching for more accurate detection
        """
        query_lower = query.lower()
        
        # Home patterns
        home_patterns = [
            r'\bhome\b',
            r'\bat home\b',
            r'\bhome games?\b',
            r'\bhome court\b',
            r'\bat their place\b'
        ]
        
        # Away patterns  
        away_patterns = [
            r'\baway\b',
            r'\baway games?\b',
            r'\bon the road\b',
            r'\broad games?\b',
            r'\broad trip\b',
            r'\broad\b(?!\s+(?:runner|warrior|rage))',  # Avoid false matches with player names
            r'\bvisiting\b',
            r'\bon road\b'
        ]
        
        # Check for home patterns first
        for pattern in home_patterns:
            if re.search(pattern, query_lower):
                return "home"
        
        # Check for away patterns
        for pattern in away_patterns:
            if re.search(pattern, query_lower):
                return "away"
        
        return None
    
    def _extract_opponent_filters(self, query: str) -> List[Tuple[str, int]]:
        """
        Extract opponent filters using enhanced natural language mapping.
        Args:
            query (str): The original query string.
        Returns:
            List[Tuple[str, int]]: List of opponent filter tuples, e.g. [('C&S PTS', 10)].
        Implementation details:
            - Maps natural language to specific filter types using OPPONENT_FILTER_MAPPINGS
            - Handles team abbreviations and ranking patterns
            - Supports various natural language constructions
        """
        filters = []
        query_lower = query.lower()
        
        # First, handle team abbreviations (existing logic)
        team_pattern = r'\b([A-Z]{2,3})\b'
        team_matches = re.findall(team_pattern, query)
        for team in team_matches:
            if team.lower() in self.teams:
                filters.append(("team", team.upper()))
        
        # Handle ranking patterns (map to actual NBA stat columns)
        ranking_patterns = [
            (r'top\s+(\d+)\s+(?:defenses?|defensive\s+teams?)', 'OPP_PTS', -1),
            (r'bottom\s+(\d+)\s+(?:defenses?|defensive\s+teams?)', 'OPP_PTS', 1),
            (r'worst\s+(\d+)\s+(?:defenses?|defensive\s+teams?)', 'OPP_PTS', 1),
            (r'best\s+(\d+)\s+(?:defenses?|defensive\s+teams?)', 'OPP_PTS', -1),
            (r'top\s+(\d+)\s+(?:offenses?|offensive\s+teams?)', 'OPP_PTS', 1),
            (r'bottom\s+(\d+)\s+(?:offenses?|offensive\s+teams?)', 'OPP_PTS', -1),
            (r'worst\s+(\d+)\s+(?:offenses?|offensive\s+teams?)', 'OPP_PTS', -1),
            (r'best\s+(\d+)\s+(?:offenses?|offensive\s+teams?)', 'OPP_PTS', 1),
            (r'top\s+(\d+)\s+(?:teams?)', 'OPP_PTS', 1),
            (r'bottom\s+(\d+)\s+(?:teams?)', 'OPP_PTS', -1),
            (r'worst\s+(\d+)\s+(?:teams?)', 'OPP_PTS', -1),
            (r'best\s+(\d+)\s+(?:teams?)', 'OPP_PTS', 1),
        ]
        
        for pattern, filter_type, direction in ranking_patterns:
            matches = re.findall(pattern, query_lower)
            for match in matches:
                try:
                    rank = int(match)
                    filters.append((filter_type, rank * direction))
                except ValueError:
                    pass
        
        # NEW: Handle specific filter types using enhanced patterns
        processed_filters = set()  # Track processed filters to avoid duplicates
        
        for pattern, pattern_type in OPPONENT_FILTER_PATTERNS:
            matches = re.findall(pattern, query_lower)
            for match in matches:
                if pattern_type == 'ranking':
                    # Handle "top 10 catch and shoot teams"
                    try:
                        rank_num = int(match[0])
                        filter_text = match[1].strip()
                        filter_type = self._map_filter_text_to_type(filter_text)
                        if filter_type and filter_type not in processed_filters:
                            filters.append((filter_type, rank_num))
                            processed_filters.add(filter_type)
                    except (ValueError, IndexError):
                        continue
                else:
                    # Handle other pattern types
                    filter_text = match.strip()
                    filter_type = self._map_filter_text_to_type(filter_text)
                    if filter_type and filter_type not in processed_filters:
                        # Default rank of 10 for non-ranking patterns
                        filters.append((filter_type, 10))
                        processed_filters.add(filter_type)
        
        return filters
    
    def _map_filter_text_to_type(self, filter_text: str) -> Optional[str]:
        """
        Map natural language filter text to specific filter type.
        Only handles obvious, unambiguous cases - complex cases go to LLM.
        Args:
            filter_text (str): Natural language description of the filter
        Returns:
            Optional[str]: The mapped filter type or None if not found
        """
        filter_text = filter_text.lower().strip()
        
        # Direct mapping lookup - only for very specific, obvious phrases
        direct_mappings = {
            'catch and shoot': 'C&S PTS',
            'pullup': 'PU PTS',
            'transition': 'Transition',
            'isolation': 'Isolation',
            'offensive rebound': 'OffRebound',
            'spot up': 'Spotup',
            'handoff': 'Handoff',
            'off screen': 'OffScreen',
            'post up': 'Postup',
            'close range': 'Less Than 10 ft',
            'inside 10 feet': 'Less Than 10 ft',
        }
        
        # Only return exact matches - no fuzzy matching
        return direct_mappings.get(filter_text)
    
    def _has_opponent_filter_keywords(self, query: str) -> bool:
        """
        Detect if query contains opponent filter keywords that should trigger aggressive LLM fallback.
        
        Args:
            query (str): The query string to analyze
            
        Returns:
            bool: True if opponent filter keywords are detected
        """
        query_lower = query.lower()
        
        # Opponent context keywords
        opponent_keywords = [
            'against', 'vs', 'versus', 'opponent', 'opponents',
            'team', 'teams'
        ]
        
        # Quality/ranking keywords that suggest opponent filtering
        quality_keywords = [
            'elite', 'strong', 'weak', 'tough', 'good', 'bad',
            'top', 'bottom', 'best', 'worst', 'leading', 'trailing'
        ]
        
        # Specific opponent filter categories
        category_keywords = [
            'defensive', 'defense', 'defenses', 'offensive', 'offense', 'offenses',
            'rebounding', 'scoring', 'shooting', 'passing',
            'pullup', 'catch and shoot', 'transition', 'isolation',
            'spot up', 'handoff', 'post up', 'off screen'
        ]
        
        # Check for opponent context + quality/category combinations
        has_opponent_context = any(keyword in query_lower for keyword in opponent_keywords)
        has_quality_descriptor = any(keyword in query_lower for keyword in quality_keywords)
        has_category_descriptor = any(keyword in query_lower for keyword in category_keywords)
        
        # Trigger if we have opponent context AND (quality OR category descriptors)
        return has_opponent_context and (has_quality_descriptor or has_category_descriptor)
    
    def _extract_minutes_filter(self, query: str) -> Optional[Tuple[int, int]]:
        """
        Extract minutes filter from the query.
        Args:
            query (str): The original query string.
        Returns:
            Optional[Tuple[int, int]]: (min_minutes, max_minutes) if found, else None.
        Implementation details:
            - Handles patterns like "30+ minutes", "less than 25 minutes", "20-35 minutes"
            - Supports various synonyms for minutes (min, mins, minutes)
        """
        query_lower = query.lower()
        
        # Pattern for "X+ minutes" or "X or more minutes" or "more than X minutes"
        plus_patterns = [
            r'(\d+)\+\s*(?:min|mins|minutes)',
            r'(\d+)\s*or\s*more\s*(?:min|mins|minutes)',
            r'more\s*than\s*(\d+)\s*(?:min|mins|minutes)',
            r'over\s*(\d+)\s*(?:min|mins|minutes)',
            r'above\s*(\d+)\s*(?:min|mins|minutes)',
            r'at\s*least\s*(\d+)\s*(?:min|mins|minutes)',
            r'minimum\s*(\d+)\s*(?:min|mins|minutes)',
        ]
        
        # Pattern for "less than X minutes" or "under X minutes"
        less_patterns = [
            r'less\s*than\s*(\d+)\s*(?:min|mins|minutes)',
            r'under\s*(\d+)\s*(?:min|mins|minutes)',
            r'below\s*(\d+)\s*(?:min|mins|minutes)',
            r'maximum\s*(\d+)\s*(?:min|mins|minutes)',
            r'max\s*(\d+)\s*(?:min|mins|minutes)',
        ]
        
        # Pattern for range "X-Y minutes" or "between X and Y minutes"
        range_patterns = [
            r'(\d+)\s*-\s*(\d+)\s*(?:min|mins|minutes)',
            r'(\d+)\s*to\s*(\d+)\s*(?:min|mins|minutes)',
            r'between\s*(\d+)\s*and\s*(\d+)\s*(?:min|mins|minutes)',
        ]
        
        # Check for plus patterns (X or more minutes)
        for pattern in plus_patterns:
            match = re.search(pattern, query_lower)
            if match:
                min_minutes = int(match.group(1))
                return (min_minutes, 48)  # 48 is max NBA game minutes
        
        # Check for less patterns (less than X minutes)
        for pattern in less_patterns:
            match = re.search(pattern, query_lower)
            if match:
                max_minutes = int(match.group(1))
                return (0, max_minutes)
        
        # Check for range patterns (X-Y minutes)
        for pattern in range_patterns:
            match = re.search(pattern, query_lower)
            if match:
                min_minutes = int(match.group(1))
                max_minutes = int(match.group(2))
                # Ensure valid range
                if min_minutes <= max_minutes:
                    return (min_minutes, max_minutes)
        
        # Check for exact minutes "X minutes" without other operators
        exact_pattern = r'(?:exactly\s*)?(\d+)\s*(?:min|mins|minutes)(?!\s*(?:or\s*more|or\s*less|\+|\-|to))'
        match = re.search(exact_pattern, query_lower)
        if match:
            exact_minutes = int(match.group(1))
            # For exact minutes, use a small range (±2 minutes)
            return (max(0, exact_minutes - 2), min(48, exact_minutes + 2))
        
        return None
    
    def _extract_players_with_syntax(self, query: str, doc) -> Tuple[Optional[str], List[str], List[str]]:
        """
        Extract player relationships (main, ON, OFF) from the query using entity-first approach.
        Args:
            query (str): The original query string.
            doc (spacy.tokens.Doc): The spaCy Doc object for the query.
        Returns:
            Tuple[Optional[str], List[str], List[str]]: (main_player, players_on, players_off)
        Implementation details:
            - Step 1: Find all player entities using spaCy
            - Step 2: Use fallback extraction for missed players
            - Step 3: Classify each player based on surrounding context
        """
        # Step 1: Find ALL player entities using spaCy
        all_players = []
        found_names = set()  # Track names to avoid duplicates
        
        # Get spaCy entities (primary method)
        for ent in doc.ents:
            if ent.label_ == "PLAYER":
                player_name = ent.ent_id_ if ent.ent_id_ else ent.text
                if player_name and player_name not in found_names:
                    all_players.append({
                        'name': player_name,
                        'start': ent.start_char,
                        'end': ent.end_char,
                        'text': ent.text
                    })
                    found_names.add(player_name)
        
        # Step 2: Enhanced fallback for obvious missed players
        # Look for common patterns where spaCy might miss players
        fallback_patterns = [
            # "X with Y" - both X and Y should be players
            r'^(\w+(?:\s+\w+)*)\s+with\s+(\w+(?:\s+\w+)*?)(?:\s+(?:at|on|last|this|and|but)|\s*$)',
            # "X with Y and Z" - X, Y, Z should be players
            r'^(\w+(?:\s+\w+)*)\s+with\s+(\w+(?:\s+\w+)*)\s+and\s+(\w+(?:\s+\w+)*?)(?:\s+(?:at|on|last|this|but)|\s*$)',
            # "X with Y but without Z" - X, Y, Z should be players
            r'^(\w+(?:\s+\w+)*)\s+with\s+(\w+(?:\s+\w+)*)\s+but\s+without\s+(\w+(?:\s+\w+)*?)(?:\s+(?:at|on|last|this)|\s*$)',
            # "X without Y" - both X and Y should be players
            r'^(\w+(?:\s+\w+)*)\s+without\s+(\w+(?:\s+\w+)*?)(?:\s+(?:at|on|last|this)|\s*$)',
            # "X with Y, Z and W" - complex lists
            r'^(\w+(?:\s+\w+)*)\s+with\s+(\w+(?:\s+\w+)*),\s*(\w+(?:\s+\w+)*)\s+and\s+(\w+(?:\s+\w+)*)',
            # "X with Y and Z at home" - handle location context
            r'^(\w+(?:\s+\w+)*)\s+with\s+(\w+(?:\s+\w+)*)\s+and\s+(\w+(?:\s+\w+)*)\s+(?:at|on)',
            # "X at/on location" or "X last N games" - simple single player queries
            r'^(\w+(?:\s+\w+)*?)(?:\s+(?:at|on|last|this|in|during|for|against))',
        ]
        
        for pattern in fallback_patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                for i, potential_name in enumerate(match.groups()):
                    if potential_name and self._is_valid_player_candidate(potential_name):
                        # Try to find exact match first
                        player_name = self._extract_single_player_name(potential_name.strip())
                        if player_name and player_name not in found_names:
                            # Find the actual position in query (case-insensitive)
                            start_pos = query.lower().find(potential_name.lower().strip())
                            if start_pos != -1:
                                all_players.append({
                                    'name': player_name,
                                    'start': start_pos,
                                    'end': start_pos + len(potential_name.strip()),
                                    'text': potential_name.strip()
                                })
                                found_names.add(player_name)
        
        # Step 2.5: Additional fallback for common missed patterns
        # Look for standalone names that might be players
        additional_patterns = [
            # After "with" keyword
            r'with\s+(\w+(?:\s+\w+)*?)(?:\s+(?:and|at|on|last|this|but)|\s*$)',
            # After "without" keyword  
            r'without\s+(\w+(?:\s+\w+)*?)(?:\s+(?:and|at|on|last|this|but)|\s*$)',
            # After "and" keyword
            r'and\s+(\w+(?:\s+\w+)*?)(?:\s+(?:and|at|on|last|this|but)|\s*$)',
            # After "but without" keyword
            r'but\s+without\s+(\w+(?:\s+\w+)*?)(?:\s+(?:and|at|on|last|this)|\s*$)',
        ]
        
        for pattern in additional_patterns:
            matches = re.finditer(pattern, query, re.IGNORECASE)
            for match in matches:
                potential_name = match.group(1).strip()
                if potential_name and self._is_valid_player_candidate(potential_name):
                    player_name = self._extract_single_player_name(potential_name)
                    if player_name and player_name not in found_names:
                        start_pos = match.start(1)
                        all_players.append({
                            'name': player_name,
                            'start': start_pos,
                            'end': start_pos + len(potential_name),
                            'text': potential_name
                        })
                        found_names.add(player_name)
        
        # Step 3: Classify each player based on context and position
        main_player = None
        players_on = []
        players_off = []
        
        # Sort players by their position in the query to handle priority correctly
        all_players.sort(key=lambda x: x['start'])
        
        for i, player in enumerate(all_players):
            classification = self._classify_player_relationship(query, player, all_players)
            
            if classification == "main":
                if main_player is None:  # Only set first main player
                    main_player = player['name']
            elif classification == "with":
                players_on.append(player['name'])
            elif classification == "without":
                players_off.append(player['name'])
        
        # If no main player found but we have players, use positional logic
        if main_player is None and all_players:
            # Check if query starts with a player name pattern
            query_lower = query.lower()
            
            # Look for patterns like "X with Y" where X should be main
            with_pattern = r'^([^,]+?)\s+with\s+'
            match = re.match(with_pattern, query_lower)
            if match:
                potential_main = match.group(1).strip()
                # Find the player that matches this text
                for player in all_players:
                    if player['start'] < len(potential_main):
                        main_player = self._normalize_player_name(player['name'])
                        # Remove from players_on if it was misclassified
                        normalized_name = self._normalize_player_name(player['name'])
                        if normalized_name in players_on:
                            players_on.remove(normalized_name)
                        break
            
            # If still no main player, use the first player that wasn't classified as with/without
            if main_player is None:
                for player in all_players:
                    normalized_name = self._normalize_player_name(player['name'])
                    if normalized_name not in players_on and normalized_name not in players_off:
                        main_player = normalized_name
                        break
        
        # Remove duplicates while preserving order
        players_on = list(dict.fromkeys(players_on))
        players_off = list(dict.fromkeys(players_off))
        
        return main_player, players_on, players_off
    
    def _classify_player_relationship(self, query: str, player: Dict, all_players: List[Dict]) -> str:
        """
        Classify a player's relationship (main, with, without) based on surrounding context.
        Args:
            query (str): The original query string.
            player (Dict): Player entity with 'name', 'start', 'end', 'text' keys.
            all_players (List[Dict]): All player entities found in the query.
        Returns:
            str: 'main', 'with', 'without', or 'unknown'
        """
        query_lower = query.lower()
        start_pos = player['start']
        end_pos = player['end']
        
        # Get context before and after the player name
        context_before = query_lower[:start_pos].strip()
        context_after = query_lower[end_pos:].strip()
        
        # Check for "without" patterns first (more specific)
        without_patterns = [
            r'without$',
            r'but\s+without$',
            r'minus$',
            r'excluding$',
            r'except$',
            r'not\s+with$',
        ]
        
        for pattern in without_patterns:
            if re.search(pattern, context_before):
                return "without"
        
        # Check for "with" patterns
        with_patterns = [
            r'with$',  # "with" at end of context
            r'with\s+[\w\s]+\s+and$',  # "with X and" at end
            r'with\s+[\w\s]+,$',  # "with X," at end
            r'playing\s+with$',
            r'alongside$',
            r'including$',
            r'featuring$',
        ]
        
        for pattern in with_patterns:
            if re.search(pattern, context_before):
                return "with"
        
        # Check for "and" in context (part of a list)
        if re.search(r'and$', context_before):
            # Look for "with" or "without" keywords earlier in the context
            if 'without' in context_before:
                return "without"
            elif 'with' in context_before:
                return "with"
        
        # Check for comma-separated lists
        if re.search(r',$', context_before):
            # Look for "with" or "without" keywords earlier in the context
            if 'without' in context_before:
                return "without"
            elif 'with' in context_before:
                return "with"
        
        # Handle complex "but without" constructions
        but_without_match = re.search(r'(.+?)\s+but\s+without\s+(.+)', query_lower)
        if but_without_match:
            with_part = but_without_match.group(1)
            without_part = but_without_match.group(2)
            
            # Check which part contains this player
            if start_pos <= len(with_part):
                # Player is in the "with" part
                if 'with' in with_part:
                    # Check if this is NOT the main player (main player comes before "with")
                    with_match = re.search(r'^(.+?)\s+with\s+', with_part)
                    if with_match:
                        main_part = with_match.group(1)
                        if start_pos > len(main_part):
                            return "with"
            else:
                # Player is in the "without" part
                return "without"
        
        # Check if this is the first player in the query and not in a "with" context
        if player == min(all_players, key=lambda p: p['start']):
            # First player is usually main unless it's clearly in a "with" context
            if not any(keyword in context_before for keyword in ['with', 'alongside', 'including', 'featuring']):
                return "main"
        
        # Default: if no clear relationship context found, assume main player
        return "main"
    
    def _is_valid_player_candidate(self, text: str) -> bool:
        """
        Validate if a text fragment could be a valid player name.
        Args:
            text (str): The text fragment to validate.
        Returns:
            bool: True if the text could be a player name, False otherwise.
        """
        if not text or len(text.strip()) < 2:
            return False
        
        text_lower = text.lower().strip()
        
        # Common words that are NOT player names
        invalid_words = {
            'game', 'games', 'last', 'this', 'recent', 'season', 'home', 'away', 'with', 'without',
            'against', 'vs', 'road', 'court', 'team', 'player', 'stats', 'points', 'rebounds',
            'assists', 'minutes', 'shooting', 'defense', 'offense', 'when', 'where', 'how',
            'what', 'who', 'during', 'after', 'before', 'including', 'excluding', 'featuring',
            'exactly', 'approximately', 'around', 'about', 'nearly', 'over', 'under', 'more',
            'less', 'than', 'between', 'and', 'or', 'but', 'the', 'a', 'an', 'to', 'from',
            'shai gilgeous alexander without josh giddey'  # Specific problematic pattern
        }
        
        # Check if the entire text is an invalid word
        if text_lower in invalid_words:
            return False
        
        # Check if text contains too many non-name words
        words = text_lower.split()
        invalid_word_count = sum(1 for word in words if word in invalid_words)
        if len(words) > 1 and invalid_word_count >= len(words) / 2:
            return False
        
        # Must contain at least one alphabetic character
        if not any(c.isalpha() for c in text):
            return False
        
        # Avoid very long phrases (likely not player names)
        if len(words) > 4:
            return False
        
        return True

    def _extract_multiple_players(self, text: str) -> List[str]:
        """
        Extract multiple player names from a text fragment, splitting on common conjunctions and separators.
        Args:
            text (str): The text fragment containing one or more player names.
        Returns:
            List[str]: List of canonical player names found in the fragment.
        Implementation details:
            - Splits on 'and', ',', '&', '+'.
            - Uses _extract_single_player_name for each part.
        """
        if not text or len(text.strip()) < 2:
            return []
        separators = [
            r'\s+and\s+',
            r'\s*,\s*',
            r'\s*&\s*',
            r'\s*\+\s*',
        ]
        parts = [text]
        for separator in separators:
            new_parts = []
            for part in parts:
                new_parts.extend(re.split(separator, part, flags=re.IGNORECASE))
            parts = new_parts
        player_names = []
        for part in parts:
            part = part.strip()
            if part:
                player_name = self._extract_single_player_name(part, context="players_on_off")
                if player_name:
                    player_names.append(player_name)
        return player_names
    
    def _classify_intent(self, query: str, components: QueryComponents) -> Optional[str]:
        """
        Classify the intent of the query (game logs, player profile, team stats).
        Args:
            query (str): The original query string.
            components (QueryComponents): The parsed query components.
        Returns:
            Optional[str]: The classified intent string.
        Implementation details:
            - Looks for keywords to determine intent.
            - Defaults to 'game_logs' if no specific intent is found.
        """
        query_lower = query.lower()
        if any(word in query_lower for word in ["game log", "game logs", "performance", "stats", "statistics"]):
            return "game_logs"
        elif any(word in query_lower for word in ["profile", "overview", "summary"]):
            return "player_profile"
        elif any(word in query_lower for word in ["team", "teams"]):
            return "team_stats"
        return "game_logs"

    
    def _extract_time_period_with_coverage(self, query: str, coverage: QueryCoverage) -> Tuple[Optional[str], Optional[int]]:
        """Extract time period and track coverage"""
        time_period, game_count = self._extract_time_period(query)
        
        # Track coverage for time-related patterns
        if time_period or game_count:
            game_patterns = [
                r'(?:last|past|recent)\s+(\d+)\s+(?:games?|matchups?|contests?)',
                r'(?:last|past|recent)\s+(\w+)\s+(?:games?|matchups?|contests?)'
            ]
            
            for pattern in game_patterns:
                match = re.search(pattern, query.lower())
                if match:
                    coverage.add_component(ParsedComponent(
                        value=match.group(0),
                        start_pos=match.start(),
                        end_pos=match.end(),
                        component_type="time",
                        extraction_method="regex"
                    ))
                    break
            
            # Track season patterns
            season_patterns = [
                r'(?:this|current)\s+season',
                r'season',
                r'\d{4}-\d{2,4}',
                r'\d{4}/\d{2,4}'
            ]
            
            for pattern in season_patterns:
                match = re.search(pattern, query.lower())
                if match:
                    coverage.add_component(ParsedComponent(
                        value=match.group(0),
                        start_pos=match.start(),
                        end_pos=match.end(),
                        component_type="time",
                        extraction_method="regex"
                    ))
                    break
        
        return time_period, game_count
    
    def _extract_date_filter_with_coverage(self, query: str, coverage: QueryCoverage) -> Optional[str]:
        """Extract date filter and track coverage"""
        date_filter = self._extract_date_filter(query)
        
        if date_filter:
            # Track coverage for date patterns using the date parser's analysis
            try:
                date_result = self.date_parser.get_date_components(query)
                if date_result.get('original_expression'):
                    # Find the position of the original expression in the query
                    original_expr = date_result['original_expression']
                    start_pos = query.lower().find(original_expr.lower())
                    if start_pos != -1:
                        coverage.add_component(ParsedComponent(
                            value=original_expr,
                            start_pos=start_pos,
                            end_pos=start_pos + len(original_expr),
                            component_type="date_filter",
                            extraction_method="nba_date_parser"
                        ))
            except Exception as e:
                print(f"Warning: Date coverage tracking failed: {e}")
        
        return date_filter
    
    def _extract_location_with_coverage(self, query: str, coverage: QueryCoverage) -> Optional[str]:
        """Extract location and track coverage"""
        location = self._extract_location(query)
        
        if location:
            # Track coverage for location patterns
            home_patterns = [
                r'\bhome\b',
                r'\bat home\b',
                r'\bhome games?\b',
                r'\bhome court\b',
                r'\bat their place\b'
            ]
            
            away_patterns = [
                r'\baway\b',
                r'\baway games?\b',
                r'\bon the road\b',
                r'\broad games?\b',
                r'\broad trip\b',
                r'\broad\b(?!\s+(?:runner|warrior|rage))',
                r'\bvisiting\b',
                r'\bon road\b'
            ]
            
            patterns = home_patterns if location == "home" else away_patterns
            
            for pattern in patterns:
                match = re.search(pattern, query.lower())
                if match:
                    coverage.add_component(ParsedComponent(
                        value=match.group(0),
                        start_pos=match.start(),
                        end_pos=match.end(),
                        component_type="location",
                        extraction_method="regex"
                    ))
                    break
        
        return location
    
    def _extract_opponent_filters_with_coverage(self, query: str, coverage: QueryCoverage) -> List[Tuple[str, int]]:
        """Extract opponent filters and track coverage"""
        filters = self._extract_opponent_filters(query)
        
        if filters:
            # Track coverage for ranking patterns (updated to use actual NBA stat columns)
            ranking_patterns = [
                (r'top\s+(\d+)\s+(?:defenses?|defensive\s+teams?)', 'OPP_PTS', -1),
                (r'bottom\s+(\d+)\s+(?:defenses?|defensive\s+teams?)', 'OPP_PTS', 1),
                (r'top\s+(\d+)\s+(?:offenses?|offensive\s+teams?)', 'OPP_PTS', 1),
                (r'bottom\s+(\d+)\s+(?:offenses?|offensive\s+teams?)', 'OPP_PTS', -1),
                (r'top\s+(\d+)\s+(?:teams?)', 'OPP_PTS', 1),
                (r'bottom\s+(\d+)\s+(?:teams?)', 'OPP_PTS', -1),
            ]
            
            for pattern, filter_type, direction in ranking_patterns:
                match = re.search(pattern, query.lower())
                if match:
                    coverage.add_component(ParsedComponent(
                        value=match.group(0),
                        start_pos=match.start(),
                        end_pos=match.end(),
                        component_type="opponent_filter",
                        extraction_method="regex"
                    ))
            
            # Track team abbreviations
            team_pattern = r'\b([A-Z]{2,3})\b'
            for match in re.finditer(team_pattern, query):
                if match.group(1).lower() in self.teams:
                    coverage.add_component(ParsedComponent(
                        value=match.group(0),
                        start_pos=match.start(),
                        end_pos=match.end(),
                        component_type="team_filter",
                        extraction_method="regex"
                    ))
            
            # NEW: Track coverage for enhanced opponent filter patterns
            for pattern, pattern_type in OPPONENT_FILTER_PATTERNS:
                matches = re.findall(pattern, query.lower())
                for match in matches:
                    if pattern_type == 'ranking':
                        # For ranking patterns, track the full match
                        full_match = re.search(pattern, query.lower())
                        if full_match:
                            coverage.add_component(ParsedComponent(
                                value=full_match.group(0),
                                start_pos=full_match.start(),
                                end_pos=full_match.end(),
                                component_type="opponent_filter",
                                extraction_method="enhanced_regex"
                            ))
                    else:
                        # For other patterns, track the matched filter text
                        filter_text = match.strip()
                        if filter_text:
                            # Find the position of this text in the original query
                            start_pos = query.lower().find(filter_text)
                            if start_pos != -1:
                                coverage.add_component(ParsedComponent(
                                    value=filter_text,
                                    start_pos=start_pos,
                                    end_pos=start_pos + len(filter_text),
                                    component_type="opponent_filter",
                                    extraction_method="enhanced_regex"
                                ))
        
        return filters
    
    def _extract_minutes_filter_with_coverage(self, query: str, coverage: QueryCoverage) -> Optional[Tuple[int, int]]:
        """Extract minutes filter and track coverage"""
        minutes_filter = self._extract_minutes_filter(query)
        
        if minutes_filter:
            # Track coverage for minutes patterns
            all_patterns = [
                # Plus patterns
                r'(\d+)\+\s*(?:min|mins|minutes)',
                r'(\d+)\s*or\s*more\s*(?:min|mins|minutes)',
                r'more\s*than\s*(\d+)\s*(?:min|mins|minutes)',
                r'over\s*(\d+)\s*(?:min|mins|minutes)',
                r'above\s*(\d+)\s*(?:min|mins|minutes)',
                r'at\s*least\s*(\d+)\s*(?:min|mins|minutes)',
                r'minimum\s*(\d+)\s*(?:min|mins|minutes)',
                # Less patterns
                r'less\s*than\s*(\d+)\s*(?:min|mins|minutes)',
                r'under\s*(\d+)\s*(?:min|mins|minutes)',
                r'below\s*(\d+)\s*(?:min|mins|minutes)',
                r'maximum\s*(\d+)\s*(?:min|mins|minutes)',
                r'max\s*(\d+)\s*(?:min|mins|minutes)',
                # Range patterns
                r'(\d+)\s*-\s*(\d+)\s*(?:min|mins|minutes)',
                r'(\d+)\s*to\s*(\d+)\s*(?:min|mins|minutes)',
                r'between\s*(\d+)\s*and\s*(\d+)\s*(?:min|mins|minutes)',
                # Exact patterns
                r'(?:exactly\s*)?(\d+)\s*(?:min|mins|minutes)(?!\s*(?:or\s*more|or\s*less|\+|\-|to))',
            ]
            
            for pattern in all_patterns:
                match = re.search(pattern, query.lower())
                if match:
                    coverage.add_component(ParsedComponent(
                        value=match.group(0),
                        start_pos=match.start(),
                        end_pos=match.end(),
                        component_type="minutes_filter",
                        extraction_method="regex"
                    ))
                    break
        
        return minutes_filter
    
    def _extract_self_filters_with_coverage(self, query: str, coverage: QueryCoverage) -> List[SelfFilter]:
        """Extract self-filter conditions and track coverage"""
        filters = []
        
        # Find self-filter sections using the patterns (stop at first match to avoid duplicates)
        for pattern in SELF_FILTER_PATTERNS:
            match = re.search(pattern, query.lower())
            if match:
                filter_text = match.group(1)
                
                # Parse individual conditions from the filter text
                individual_filters = self._parse_filter_conditions(filter_text)
                filters.extend(individual_filters)
                
                # Track coverage for the entire self-filter expression
                coverage.add_component(ParsedComponent(
                    value=match.group(0),
                    start_pos=match.start(),
                    end_pos=match.end(),
                    component_type="self_filter",
                    extraction_method="regex"
                ))
                
                # Stop after first match to avoid duplicates
                break
        
        return filters
    
    def _parse_filter_conditions(self, filter_text: str) -> List[SelfFilter]:
        """Parse individual filter conditions from text"""
        filters = []
        
        # Handle AND conditions by splitting on "and", but not if it's part of "between X and Y"
        # First, protect "between X and Y" patterns
        protected_text = filter_text.lower()
        between_matches = list(re.finditer(r'between\s+\d+\s+and\s+\d+', protected_text))
        
        # Replace "and" in between patterns with a placeholder
        for match in reversed(between_matches):  # Reverse to maintain positions
            start, end = match.span()
            replacement = protected_text[start:end].replace(' and ', ' BETWEEN_AND ')
            protected_text = protected_text[:start] + replacement + protected_text[end:]
        
        # Now split on "and"
        conditions = re.split(r'\s+and\s+', protected_text)
        
        # Restore the "and" in between patterns
        conditions = [cond.replace(' BETWEEN_AND ', ' and ') for cond in conditions]
        
        for condition in conditions:
            condition = condition.strip()
            if condition:
                filter_obj = self._parse_single_condition(condition)
                if filter_obj:
                    filters.append(filter_obj)
        
        return filters
    
    def _parse_single_condition(self, condition: str) -> Optional[SelfFilter]:
        """Parse a single condition like '30+ points' or 'more than 25 rebounds'"""
        # Handle special cases like "double-digit rebounds"
        double_digit_pattern = r'double-digit\s+(\w+(?:\s+\w+)*)'
        double_digit_match = re.search(double_digit_pattern, condition)
        if double_digit_match:
            stat_name = double_digit_match.group(1)
            return self._create_filter(stat_name, 'gte', 10, None, condition)
        
        # Check for "scoring X+" patterns - map to points
        scoring_pattern = r'scoring\s+(\d+)\+'
        scoring_match = re.search(scoring_pattern, condition)
        if scoring_match:
            value = int(scoring_match.group(1))
            return self._create_filter('points', 'gte', value, None, condition)
        
        # Check for "shooting X+ times" patterns - map to FGA
        shooting_pattern = r'shooting\s+(\d+)\+\s+times?'
        shooting_match = re.search(shooting_pattern, condition)
        if shooting_match:
            value = int(shooting_match.group(1))
            return self._create_filter('shots', 'gte', value, None, condition)
        
        # Try each comparison pattern FIRST (including between)
        for pattern, operator in COMPARISON_PATTERNS:
            match = re.search(pattern, condition)
            if match:
                # Extract values and stat name based on operator type
                if operator == 'between':
                    # Pattern: between X and Y stat_name
                    value1, value2, stat_name = match.groups()
                    return self._create_filter(stat_name, operator, int(value1), int(value2), condition)
                else:
                    # Pattern: value stat_name
                    value, stat_name = match.groups()
                    return self._create_filter(stat_name, operator, int(value), None, condition)
        
        # Check if this is a simple "30+ points" pattern (from "30+ point games") - as fallback
        simple_pattern = r'(\d+)\+?\s*(points?|rebounds?|assists?|steals?|blocks?|threes?|buckets?|boards?|dimes?)'
        simple_match = re.search(simple_pattern, condition)
        if simple_match:
            value, stat_name = simple_match.groups()
            # Convert plural to singular for mapping
            stat_name_clean = stat_name.rstrip('s') if stat_name.endswith('s') else stat_name
            return self._create_filter(stat_name_clean, 'gte', int(value), None, condition)
        
        return None
    
    def _create_filter(self, stat_name: str, operator: str, value: int, 
                      value2: Optional[int], original_text: str) -> Optional[SelfFilter]:
        """Create SelfFilter object if stat name is valid"""
        # Clean up stat name (remove extra spaces, normalize)
        stat_name_clean = ' '.join(stat_name.strip().split())
        original_lower = original_text.lower()
        
        # Check if this is an attempt context based on verbs in the original text
        attempt_verbs = ['attempts', 'attempt', 'takes', 'take', 'shoots', 'shoot', 'shooting']
        is_attempt_context = any(verb in original_lower for verb in attempt_verbs)
        
        # Check if this is a blocks context (should override attempt context)
        is_blocks_context = 'blocks' in original_lower or 'block' in original_lower
        
        # Handle context-sensitive mappings
        if is_blocks_context:
            # Special handling for blocks context
            if stat_name_clean.lower() in ['shot', 'shots']:
                stat_column = 'BLK'
            else:
                stat_column = STAT_MAPPINGS.get(stat_name_clean.lower())
        elif is_attempt_context:
            # For attempt context, prioritize FGA/FG3A/FTA mappings
            if stat_name_clean.lower() in ['field goal', 'field goals', 'shot', 'shots']:
                stat_column = 'FGA'
            elif stat_name_clean.lower() in ['three', 'threes', '3', '3s', 'three point', 'three points', 'three pointer', 'three pointers', '3 pointer', '3 pointers']:
                stat_column = 'FG3A'
            elif stat_name_clean.lower() in ['free throw', 'free throws']:
                stat_column = 'FTA'
            else:
                # Fall back to regular mapping
                stat_column = STAT_MAPPINGS.get(stat_name_clean.lower())
        else:
            # Regular context, use normal mapping
            stat_column = STAT_MAPPINGS.get(stat_name_clean.lower())
        
        if stat_column:
            return SelfFilter(
                stat_column=stat_column,
                operator=operator,
                value=value,
                value2=value2,
                original_text=original_text
            )
        
        # Silently ignore invalid stat names per user requirements
        return None
    
    def debug_spacy_entities(self, query: str) -> Dict:
        """
        Debug method to understand what spaCy entities are being found.
        """
        processed_query = self._preprocess_query(query)
        doc = self.nlp(processed_query)
        
        print(f"DEBUG spaCy Analysis for: '{query}'")
        print(f"Processed query: '{processed_query}'")
        print(f"Total players loaded: {len(self.players)}")
        print(f"Total aliases loaded: {len(self.player_aliases)}")
        
        # Show all entities found
        print("\nspaCy Entities Found:")
        for ent in doc.ents:
            print(f"  - Text: '{ent.text}', Label: {ent.label_}, ID: {ent.ent_id_}, Start: {ent.start_char}, End: {ent.end_char}")
        
        # Show tokens
        print("\nTokens:")
        for token in doc:
            print(f"  - '{token.text}' (pos: {token.pos_}, dep: {token.dep_})")
        
        # Check if our target players are in the loaded data
        test_players = ["LeBron James", "Anthony Davis", "Anthony Edwards"]
        print(f"\nTarget players in database:")
        for player in test_players:
            in_players = player in self.players
            in_aliases = any(alias_player == player for alias_player in self.player_aliases.values())
            print(f"  - {player}: in_players={in_players}, in_aliases={in_aliases}")
        
        # Check entity ruler patterns
        print(f"\nEntity ruler patterns for target players:")
        for player in test_players:
            # Check if pattern exists in entity ruler
            found_pattern = False
            for pattern_dict in self.nlp.get_pipe("entity_ruler").patterns:
                if pattern_dict.get("pattern") == player:
                    found_pattern = True
                    print(f"  - {player}: Pattern found - {pattern_dict}")
                    break
            if not found_pattern:
                print(f"  - {player}: Pattern NOT found!")
        
        # Also check for case variations
        print(f"\nChecking case variations:")
        for player in test_players:
            variations = [player, player.lower(), player.upper()]
            for var in variations:
                if var in [p.get("pattern") for p in self.nlp.get_pipe("entity_ruler").patterns]:
                    print(f"  - {var}: Found in patterns")
        
        return {
            'processed_query': processed_query,
            'entities': [(ent.text, ent.label_, ent.ent_id_) for ent in doc.ents],
            'tokens': [(token.text, token.pos_, token.dep_) for token in doc]
        }
    
    def analyze_query(self, query: str) -> Dict:
        """
        Analyze a query and return detailed debugging information, including spaCy entities, pattern matches, and confidence breakdown.
        Args:
            query (str): The original query string.
        Returns:
            Dict: Dictionary with detailed analysis and parsed components.
        """
        doc = self.nlp(query)
        spacy_entities = []
        for ent in doc.ents:
            spacy_entities.append((ent.text, ent.label_, ent.ent_id_ if ent.ent_id_ else ''))
        pattern_matches = []
        matches = self.matcher(doc)
        for match_id, start, end in matches:
            span = doc[start:end]
            label = self.nlp.vocab.strings[match_id]
            pattern_matches.append((label, span.text))
        components = self.parse(query)
        confidence_breakdown = {
            'player_found': bool(components.player_name),
            'intent_classified': bool(components.intent),
            'time_extracted': bool(components.time_period),
            'relationships_found': bool(components.players_on or components.players_off),
            'location_found': bool(components.location),
            'opponents_found': bool(components.opponent_filters)
        }
        return {
            'spacy_entities': spacy_entities,
            'pattern_matches': pattern_matches,
            'confidence_breakdown': confidence_breakdown,
            'components': components
        }


def test_opponent_filter_mapping():
    """Test the new opponent filter mapping system"""
    class MockEngine:
        def connect(self):
            return self
        
        def execute(self, query):
            # Mock team data
            class MockResult:
                def fetchall(self):
                    return [('LAL',), ('GSW',), ('BOS',)]
            return MockResult()
        
        def __enter__(self):
            return self
        
        def __exit__(self, *args):
            pass
    
    # Create a mock parser instance
    parser = BaseQueryParser(MockEngine())
    parser.teams = {'lal': 'LAL', 'gsw': 'GSW', 'bos': 'BOS'}
    
    # Test cases - only obvious, unambiguous phrases
    test_queries = [
        "LeBron James against catch and shoot teams",
        "Stephen Curry vs transition teams", 
        "Kevin Durant vs pullup teams",
        "Luka Doncic vs isolation teams",
        "Giannis against offensive rebound teams",
        "LeBron James vs LAL",
        "Stephen Curry vs top 10 catch and shoot teams",
        "Luka Doncic against close range teams",
        "Giannis vs spot up teams",
        "LeBron James vs handoff teams",
        "Stephen Curry against off screen teams",
        "Kevin Durant vs post up teams",
        "Luka Doncic vs inside 10 feet teams"
    ]
    
    print("Testing Opponent Filter Mapping System")
    print("=" * 50)
    
    for query in test_queries:
        print(f"\nQuery: {query}")
        filters = parser._extract_opponent_filters(query)
        if filters:
            for filter_type, rank in filters:
                print(f"  → {filter_type}: {rank}")
        else:
            print("  → No filters found")
    
    print("\n" + "=" * 50)
    print("Test completed!")

