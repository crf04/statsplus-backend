#!/usr/bin/env python3
"""
Debug script to analyze parser extraction
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.nl_query.parser import BaseQueryParser
from sqlalchemy import create_engine
from config import config

def debug_query(query):
    """Debug a specific query"""
    print(f"🔍 Debugging query: '{query}'")
    print("=" * 60)
    
    # Initialize parser
    engine = create_engine(config['default'].SQLALCHEMY_DATABASE_URI)
    parser = BaseQueryParser(engine)
    
    # Print player and alias lists
    print(f"[DEBUG] Players: {parser.players}")
    print(f"[DEBUG] Aliases: {parser.player_aliases}")
    
    # Parse the query
    components = parser.parse(query)
    
    print(f"📋 Parsed Components:")
    print(f"   Main Player: {components.player_name}")
    print(f"   Players ON: {components.players_on}")
    print(f"   Players OFF: {components.players_off}")
    print(f"   Time Period: {components.time_period}")
    print(f"   Game Count: {components.game_count}")
    print(f"   Location: {components.location}")
    print(f"   Opponent Filters: {components.opponent_filters}")
    print(f"   Intent: {components.intent}")
    print(f"   Confidence: {components.confidence:.3f}")
    
    # Get detailed analysis
    analysis = parser.analyze_query(query)
    
    print(f"\n🔍 Detailed Analysis:")
    print(f"   spaCy Entities: {analysis['spacy_entities']}")
    print(f"   Pattern Matches: {analysis['pattern_matches']}")
    print(f"   Confidence Breakdown: {analysis['confidence_breakdown']}")
    
    # Check for contradictions
    on_set = set(components.players_on)
    off_set = set(components.players_off)
    contradictions = on_set.intersection(off_set)
    
    if contradictions:
        print(f"\n❌ CONTRADICTIONS FOUND:")
        print(f"   Players in both ON and OFF lists: {contradictions}")
    else:
        print(f"\n✅ No contradictions found")

def test_multiple_queries():
    """Test multiple queries to verify parser robustness"""
    test_queries = [
        "LeBron James playing with Austin Reaves and without Anthony Davis",
        "Stephen Curry stats against top 10 defenses",
        "Kevin Durant last 10 games",
        "Luka Doncic home games this season",
        "Anthony Davis with LeBron James and without Austin Reaves",
        "Giannis Antetokounmpo performance when playing with Damian Lillard"
    ]
    
    for query in test_queries:
        print(f"\n{'='*80}")
        debug_query(query)
        print(f"{'='*80}")

if __name__ == "__main__":
    # Test the specific query
    debug_query("LeBron James playing with Austin Reaves and without Anthony Davis")
    
    # Test multiple queries
    print(f"\n\n{'='*80}")
    print("TESTING MULTIPLE QUERIES")
    print(f"{'='*80}")
    test_multiple_queries() 