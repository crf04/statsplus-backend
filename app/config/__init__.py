"""
Configuration module for natural language query processing
"""

from .query_schemas import ENDPOINT_SCHEMAS, INTENT_PATTERNS
from .filter_mappings import (
    FILTER_MAPPINGS, 
    LOCATION_SYNONYMS, 
    TIME_PERIOD_SYNONYMS, 
    NUMBER_WORDS, 
    RANKING_TERMS
)

__all__ = [
    'ENDPOINT_SCHEMAS', 
    'INTENT_PATTERNS',
    'FILTER_MAPPINGS', 
    'LOCATION_SYNONYMS',
    'TIME_PERIOD_SYNONYMS',
    'NUMBER_WORDS',
    'RANKING_TERMS'
] 