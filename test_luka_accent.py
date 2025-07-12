#!/usr/bin/env python3
"""
Test script to check how the parser handles Luka Dončić accent variations
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '.'))

from app.services.nl_query.parser import BaseQueryParser

class MockEngine:
    """Mock database engine for testing"""
    def connect(self):
        return self
    
    def execute(self, query):
        class MockResult:
            def fetchall(self):
                # Return both accent variations to see what's in the database
                return [
                    ("Luka Dončić",),  # With accent
                    ("Luka Doncic",),  # Without accent  
                    ("LeBron James",), ("Anthony Davis",), ("Stephen Curry",),
                    ("Kyrie Irving",), ("Kevin Durant",), ("Draymond Green",),
                ]
        return MockResult()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        pass

def test_luka_accent_variations():
    """Test different ways of writing Luka's name"""
    print("🏀 TESTING LUKA DONČIĆ ACCENT VARIATIONS")
    print("=" * 60)
    
    # Initialize parser
    parser = BaseQueryParser(MockEngine())
    
    test_cases = [
        # Different accent variations
        "Luka Dončić last 10 games",           # With accent  
        "Luka Doncic last 10 games",           # Without accent
        "luka doncic last 10 games",           # Lowercase no accent
        "luka dončić last 10 games",           # Lowercase with accent
        "LUKA DONČIĆ last 10 games",           # Uppercase with accent
        "LUKA DONCIC last 10 games",           # Uppercase without accent
        
        # With other players
        "Luka Dončić with Kyrie Irving",       # With accent
        "Luka Doncic with Kyrie Irving",       # Without accent
        "luka with kyrie",                     # Casual/short form
        
        # Check what's actually in the database
        "Luka",                                # Just first name
    ]
    
    print(f"First, let's see what players are loaded:")
    print(f"Players in database: {parser.players[:10]}...")  # Show first 10
    print(f"Looking for Luka variations...")
    
    luka_players = [p for p in parser.players if 'luka' in p.lower()]
    print(f"Luka players found: {luka_players}")
    
    print(f"\nAlias variations:")
    luka_aliases = {k: v for k, v in parser.player_aliases.items() if 'luka' in k.lower() or 'luka' in v.lower()}
    print(f"Luka aliases: {luka_aliases}")
    
    print(f"\n" + "=" * 60)
    print("TESTING QUERIES:")
    print("=" * 60)
    
    for i, query in enumerate(test_cases, 1):
        print(f"\nTest {i}: '{query}'")
        print("-" * 40)
        
        try:
            # Parse the query
            result = parser.parse(query)
            
            print(f"✅ Main Player: '{result.player_name}'")
            print(f"✅ Players ON: {result.players_on}")
            print(f"✅ Players OFF: {result.players_off}")
            print(f"✅ Confidence: {result.confidence:.1%}")
            
            # Check if we got a result
            if result.player_name:
                print(f"🎯 SUCCESS: Found player '{result.player_name}'")
            else:
                print(f"❌ FAILED: No player found")
                
        except Exception as e:
            print(f"❌ ERROR: {e}")
            import traceback
            traceback.print_exc()

def test_direct_extraction():
    """Test the direct player name extraction methods"""
    print(f"\n" + "=" * 60)
    print("DIRECT EXTRACTION TESTS:")
    print("=" * 60)
    
    parser = BaseQueryParser(MockEngine())
    
    test_names = [
        "Luka Dončić",
        "Luka Doncic", 
        "luka dončić",
        "luka doncic",
        "Luka",
        "luka",
    ]
    
    for name in test_names:
        print(f"\nTesting: '{name}'")
        result = parser._extract_single_player_name(name)
        print(f"  Result: '{result}'")
        
        # Also test fuzzy matching
        if not result:
            print(f"  Trying fuzzy matching...")
            from rapidfuzz import process, fuzz
            match = process.extractOne(name, parser.players, scorer=fuzz.token_sort_ratio, score_cutoff=70)
            print(f"  Fuzzy match: {match}")

if __name__ == "__main__":
    test_luka_accent_variations()
    test_direct_extraction() 