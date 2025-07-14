#!/usr/bin/env python3

"""
Debug script to investigate Chris Paul extraction in Luka Doncic queries.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.nl_query.parser import BaseQueryParser
import re

class MockEngine:
    def connect(self):
        return self
    
    def execute(self, query):
        # Mock player data including Chris Paul and Luka Doncic
        class MockResult:
            def fetchall(self):
                return [
                    ("Chris Paul",), 
                    ("Luka Doncic",),
                    ("LeBron James",), 
                    ("Anthony Davis",), 
                    ("Stephen Curry",),
                    ("Paul George",)
                ]
        return MockResult()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        pass

def debug_luka_cp3_extraction():
    """Debug Chris Paul extraction in Luka Doncic queries"""
    
    print("=== Chris Paul in Luka Doncic Query Debug ===")
    
    # Create parser
    parser = BaseQueryParser(MockEngine())
    
    # Test various Luka Doncic related queries
    test_queries = [
        "Luka Doncic last 10 games",
        "Luka last 10 games", 
        "Doncic last 10 games",
        "luka doncic last 10 games",
        "Luka Doncic with cp3 last 10 games",
        "Luka Doncic triple double games",
        "Luka Doncic 30+ point games",
        "Luka Doncic assists",
        "Luka Doncic performance"
    ]
    
    print(f"Testing {len(test_queries)} queries...")
    
    for i, query in enumerate(test_queries, 1):
        print(f"\n{i}. Testing: '{query}'")
        print("-" * 50)
        
        # Parse the query
        result = parser.parse(query)
        
        print(f"Main player: '{result.player_name}'")
        print(f"Players ON: {result.players_on}")
        print(f"Players OFF: {result.players_off}")
        print(f"Confidence: {result.confidence:.3f}")
        
        # Check if Chris Paul appears anywhere
        all_players = [result.player_name] + result.players_on + result.players_off
        if "Chris Paul" in all_players:
            print("🚨 ALERT: Chris Paul found in results!")
            
            # Debug the spaCy analysis
            print("\nspaCy Analysis:")
            debug_info = parser.debug_spacy_entities(query)
            print(f"Entities: {debug_info['entities']}")
            
            # Check for "cp" in the query text
            if "cp" in query.lower():
                print("Found 'cp' in query text - this might be the issue!")
            
            # Manual step-by-step extraction
            print("\nManual extraction test:")
            words = query.lower().split()
            for word in words:
                extracted = parser._extract_single_player_name(word)
                if extracted:
                    print(f"  '{word}' -> '{extracted}'")
    
    # Test specific problematic patterns
    print("\n" + "="*60)
    print("Testing specific potentially problematic patterns:")
    
    problem_patterns = [
        "cp assist",
        "cp points", 
        "cp game",
        "cp performance",
        "cp triple double"
    ]
    
    for pattern in problem_patterns:
        print(f"\nTesting fragment: '{pattern}'")
        extracted = parser._extract_single_player_name(pattern)
        print(f"  -> '{extracted}'")
    
    # Check if there are regex patterns that might extract "cp" from larger words
    print("\n" + "="*60)
    print("Checking for 'cp' extraction from words:")
    
    test_words = ["doncic", "doncic's", "performance", "triple", "double", "points", "assists"]
    for word in test_words:
        if "cp" in word:
            print(f"'{word}' contains 'cp' at position {word.find('cp')}")
            
            # Test if the parser tries to extract from substrings
            for i in range(len(word)-1):
                substring = word[i:i+2]
                if substring == "cp":
                    print(f"  Found 'cp' substring: '{substring}'")
                    extracted = parser._extract_single_player_name(substring)
                    if extracted:
                        print(f"    -> This extracts to: '{extracted}'")

if __name__ == "__main__":
    debug_luka_cp3_extraction() 