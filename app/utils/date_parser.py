"""
Natural language date parser for NBA queries.

This module handles parsing of date expressions in natural language
and converts them to the YYYY-MM-DD format required by the API.
"""

import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import dateparser
from dateutil.relativedelta import relativedelta
from app.config.settings import RuntimeSettings, current_nba_season, get_runtime_settings


class NBADateParser:
    """Parse natural language date expressions for NBA contexts."""
    
    def __init__(self, settings: RuntimeSettings | None = None):
        """Initialize the NBA date parser with season-specific knowledge."""
        self.current_year = datetime.now().year
        runtime_settings = settings or get_runtime_settings()
        self.current_season = runtime_settings.nba.current_season
        
        self.nba_dates = {
            "all star break": "2025-02-14",         # All-Star Weekend Feb 14–16, break covers 14–19
            "all-star break": "2025-02-14",
            "allstar break": "2025-02-14",
            "trade deadline": "2025-02-06",         # Deadline 3 p.m. ET 6 Feb :contentReference[oaicite:1]{index=1}
            "christmas": "2024-12-25",              # Always Dec 25
            "christmas day": "2024-12-25",
            "new year": "2025-01-01",               # Jan 1 of next year
            "new years": "2025-01-01",
            "playoffs": "2025-04-19",               # Playoffs begin Apr 19 :contentReference[oaicite:2]{index=2}
            "playoff start": "2025-04-19",
            "season start": "2024-10-22",           # Regular season opener Oct 22 :contentReference[oaicite:3]{index=3}
        }
    
    def _get_current_nba_season(self) -> str:
        """Get current NBA season in YYYY-YY format."""
        return current_nba_season()
    
    def parse_date_from_query(self, query: str) -> Optional[str]:
        """
        Extract and parse date expressions from a natural language query.
        
        Args:
            query (str): The natural language query
            
        Returns:
            Optional[str]: Date in YYYY-MM-DD format, or None if no date found
        """
        query_lower = query.lower()
        
        # Step 1: Check for NBA-specific dates
        nba_date = self._parse_nba_specific_dates(query_lower)
        if nba_date:
            return nba_date
        
        # Step 2: Check for relative date expressions
        relative_date = self._parse_relative_dates(query_lower)
        if relative_date:
            return relative_date
        
        # Step 3: Check for explicit date patterns
        explicit_date = self._parse_explicit_dates(query)
        if explicit_date:
            return explicit_date
        
        # Step 4: Use dateparser library for general parsing
        general_date = self._parse_with_dateparser(query)
        if general_date:
            return general_date
        
        return None
    
    def _parse_nba_specific_dates(self, query: str) -> Optional[str]:
        """Parse NBA-specific date expressions."""
        for phrase, date in self.nba_dates.items():
            if phrase in query:
                # Handle "since", "after", "before" prefixes
                if any(prefix in query for prefix in ["since", "after", "from"]):
                    return date
                elif "before" in query:
                    # Return day before for "before" queries
                    date_obj = datetime.strptime(date, "%Y-%m-%d")
                    return (date_obj - timedelta(days=1)).strftime("%Y-%m-%d")
                else:
                    return date
        return None
    
    def _parse_relative_dates(self, query: str) -> Optional[str]:
        """Parse relative date expressions like 'last month', 'since January'."""
        now = datetime.now()
        
        # Pattern: "last X days/weeks/months"
        last_pattern = r'(?:last|past)\s+(\d+)\s+(days?|weeks?|months?)'
        match = re.search(last_pattern, query)
        if match:
            amount = int(match.group(1))
            unit = match.group(2).rstrip('s')  # Remove plural
            
            if unit == 'day':
                date_obj = now - timedelta(days=amount)
            elif unit == 'week':
                date_obj = now - timedelta(weeks=amount)
            elif unit == 'month':
                date_obj = now - relativedelta(months=amount)
            else:
                return None
            
            return date_obj.strftime("%Y-%m-%d")
        
        # Pattern: "since/after [month]"
        month_patterns = [
            r'(?:since|after|from)\s+(january|february|march|april|may|june|july|august|september|october|november|december)',
            r'(?:since|after|from)\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)'
        ]
        
        for pattern in month_patterns:
            match = re.search(pattern, query)
            if match:
                month_name = match.group(1)
                # Use dateparser to get the date
                date_str = f"{month_name} 1, {self.current_year}"
                parsed = dateparser.parse(date_str)
                if parsed:
                    return parsed.strftime("%Y-%m-%d")
        
        # Pattern: "last month", "this month"
        if "last month" in query:
            date_obj = now - relativedelta(months=1)
            return date_obj.replace(day=1).strftime("%Y-%m-%d")
        elif "this month" in query:
            return now.replace(day=1).strftime("%Y-%m-%d")
        
        return None
    
    def _parse_explicit_dates(self, query: str) -> Optional[str]:
        """Parse explicit date formats in the query."""
        # Common date patterns
        date_patterns = [
            # YYYY-MM-DD format
            r'(\d{4}-\d{1,2}-\d{1,2})',
            # MM/DD/YYYY format
            r'(\d{1,2}/\d{1,2}/\d{4})',
            # Month DD, YYYY format
            r'((?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},?\s+\d{4})',
            # DD Month YYYY format
            r'(\d{1,2}\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{4})',
        ]
        
        for pattern in date_patterns:
            match = re.search(pattern, query.lower())
            if match:
                date_str = match.group(1)
                parsed = dateparser.parse(date_str)
                if parsed:
                    return parsed.strftime("%Y-%m-%d")
        
        return None
    
    def _parse_with_dateparser(self, query: str) -> Optional[str]:
        """Use dateparser library for general date parsing."""
        # Extract potential date phrases
        date_phrases = self._extract_date_phrases(query)
        
        for phrase in date_phrases:
            try:
                parsed = dateparser.parse(
                    phrase,
                    settings={
                        'PREFER_DAY_OF_MONTH': 'first',
                        'PREFER_DATES_FROM': 'past',
                        'RETURN_AS_TIMEZONE_AWARE': False
                    }
                )
                if parsed:
                    return parsed.strftime("%Y-%m-%d")
            except Exception:
                continue
        
        return None
    
    def _extract_date_phrases(self, query: str) -> list[str]:
        """Extract potential date phrases from the query."""
        # Look for common date-related keywords and extract surrounding context
        date_keywords = [
            'since', 'after', 'before', 'from', 'until', 'during',
            'january', 'february', 'march', 'april', 'may', 'june',
            'july', 'august', 'september', 'october', 'november', 'december',
            'last', 'this', 'next', 'past'
        ]
        
        phrases = []
        words = query.lower().split()
        
        for i, word in enumerate(words):
            if any(keyword in word for keyword in date_keywords):
                # Extract 1-4 words around the keyword
                start = max(0, i - 1)
                end = min(len(words), i + 4)
                phrase = ' '.join(words[start:end])
                phrases.append(phrase)
        
        return phrases
    
    def get_date_components(self, query: str) -> Dict[str, Any]:
        """
        Get comprehensive date parsing results.
        
        Args:
            query (str): The natural language query
            
        Returns:
            Dict[str, Any]: Dictionary with date components and metadata
        """
        result = {
            'date_filter': None,
            'date_type': None,
            'original_expression': None,
            'confidence': 0.0
        }
        
        query_lower = query.lower()
        
        # Find the original date expression
        date_expressions = [
            r'(?:since|after|from|before|until)\s+[^,\.!?]+',
            r'(?:last|past|this|next)\s+\d+\s+(?:days?|weeks?|months?)',
            r'(?:january|february|march|april|may|june|july|august|september|october|november|december)[^,\.!?]*',
            r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}',
            r'\d{4}-\d{1,2}-\d{1,2}'
        ]
        
        for pattern in date_expressions:
            match = re.search(pattern, query_lower)
            if match:
                result['original_expression'] = match.group(0).strip()
                break
        
        # Parse the date
        parsed_date = self.parse_date_from_query(query)
        if parsed_date:
            result['date_filter'] = parsed_date
            result['confidence'] = 0.9
            
            # Determine date type
            if any(word in query_lower for word in ['since', 'after', 'from']):
                result['date_type'] = 'start_date'
            elif any(word in query_lower for word in ['before', 'until']):
                result['date_type'] = 'end_date'
            else:
                result['date_type'] = 'start_date'  # Default
        
        return result
