#!/usr/bin/env python3
"""
Debug script to analyze remaining failing test cases after the parser fix.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.nl_query.parser import BaseQueryParser
from typing import Dict, List, Tuple, Any
import traceback

class MockEngine:
    """Mock database engine for testing"""
    
    def connect(self):
        return MockConnection()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        pass

class MockConnection:
    """Mock database connection"""
    
    def execute(self, query):
        return MockResult()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        pass

class MockResult:
    """Mock database result"""
    
    def fetchall(self):
        # Return common NBA players
        return [
            ("LeBron James",),
            ("Anthony Davis",),
            ("Stephen Curry",),
            ("Kevin Durant",),
            ("Giannis Antetokounmpo",),
            ("Luka Dončić",),
            ("Jayson Tatum",),
            ("Jaylen Brown",),
            ("Jimmy Butler",),
            ("Bam Adebayo",),
            ("Damian Lillard",),
            ("CJ McCollum",),
            ("Devin Booker",),
            ("Chris Paul",),
            ("Russell Westbrook",),
            ("Austin Reaves",),
            ("D'Angelo Russell",),
            ("Anthony Edwards",),
            ("Jalen Brunson",),
            ("Donovan Mitchell",),
            ("Shai Gilgeous-Alexander",),
            ("Josh Giddey",),
            ("Paolo Banchero",),
            ("Victor Wembanyama",),
            ("Scottie Barnes",),
            ("Franz Wagner",),
            ("Alperen Şengün",),
            ("Cade Cunningham",),
            ("Jalen Green",),
            ("Evan Mobley",),
            ("Khris Middleton",),
            ("Klay Thompson",),
            ("Marcus Smart",),
            ("Wendell Carter Jr.",),
            ("Kyrie Irving",),
            ("Draymond Green",),
            ("Devin Vassell",),
            ("OG Anunoby",),
            ("Fred VanVleet",),
            ("Zion Williamson",),
            ("Michael Jordan",),
            ("Rudy Gobert",),
            ("Ja Morant",),
        ]

def debug_remaining_failures():
    """Debug the remaining failing test cases"""
    
    print("🔍 DEBUG: Analyzing Remaining Failures")
    print("=" * 80)
    
    # Initialize parser
    try:
        mock_engine = MockEngine()
        parser = BaseQueryParser(mock_engine)
        print(f"✅ Parser initialized successfully")
    except Exception as e:
        print(f"❌ Failed to initialize parser: {e}")
        return
    
    # Test cases that are still failing
    remaining_failures = [
        {
            "query": "Tatum with Brown at home when they're both healthy",
            "expected_players": ["Jayson Tatum", "Jaylen Brown"],
            "issue": "Getting Anthony Davis instead of Jaylen Brown"
        },
        {
            "query": "Scottie Barnes without OG Anunoby on road games 20-30 minutes",
            "expected_players": ["Scottie Barnes", "OG Anunoby"],
            "issue": "Getting Anthony Davis instead of OG Anunoby"
        },
        {
            "query": "Alperen Şengün with Fred VanVleet home games minimum 28 minutes",
            "expected_players": ["Alperen Şengün", "Fred VanVleet"],
            "issue": "Not detecting Fred VanVleet"
        },
        {
            "query": "Lillard without McCollum road games 25-40 minutes",
            "expected_players": ["Damian Lillard", "CJ McCollum"],
            "issue": "Getting Anthony Davis instead of CJ McCollum"
        },
        {
            "query": "Victor Wembanyama with Devin Vassell home games exactly 30 minutes",
            "expected_players": ["Victor Wembanyama", "Devin Vassell"],
            "issue": "Not detecting Devin Vassell"
        }
    ]
    
    for i, test_case in enumerate(remaining_failures, 1):
        print(f"\n{'='*80}")
        print(f"REMAINING FAILURE {i}")
        print(f"Query: '{test_case['query']}'")
        print(f"Expected: {test_case['expected_players']}")
        print(f"Issue: {test_case['issue']}")
        print(f"{'='*80}")
        
        try:
            # Parse query
            components = parser.parse(test_case['query'])
            
            print(f"\n📊 ACTUAL PARSING RESULTS:")
            print(f"   Player: {components.player_name}")
            print(f"   Players ON: {components.players_on}")
            print(f"   Players OFF: {components.players_off}")
            
            # Let's also debug the spaCy entities
            debug_info = parser.debug_spacy_entities(test_case['query'])
            print(f"\n📊 spaCy Debug Info:")
            print(f"   Entities found: {debug_info['entities']}")
            
            # Let's test individual name extraction for each expected player
            for expected_player in test_case['expected_players']:
                # Extract just the name part that might be failing
                parts = expected_player.split()
                if len(parts) >= 2:
                    first_name = parts[0]
                    last_name = parts[-1]
                    full_name = expected_player
                    
                    print(f"\n🔍 Testing name extraction for: '{expected_player}'")
                    print(f"   First name: '{first_name}'")
                    print(f"   Last name: '{last_name}'")
                    print(f"   Full name: '{full_name}'")
                    
                    # Test individual extraction
                    first_result = parser._extract_single_player_name(first_name)
                    last_result = parser._extract_single_player_name(last_name)
                    full_result = parser._extract_single_player_name(full_name)
                    
                    print(f"   _extract_single_player_name('{first_name}') → {first_result}")
                    print(f"   _extract_single_player_name('{last_name}') → {last_result}")
                    print(f"   _extract_single_player_name('{full_name}') → {full_result}")
                    
                    # Check if they're in the database
                    in_players = expected_player in parser.players
                    print(f"   '{expected_player}' in players list: {in_players}")
                    
                    # Check for aliases
                    aliases = [alias for alias, player in parser.player_aliases.items() if player == expected_player]
                    print(f"   Aliases for '{expected_player}': {aliases}")
        
        except Exception as e:
            print(f"❌ Error analyzing test case: {e}")
            traceback.print_exc()

if __name__ == "__main__":
    debug_remaining_failures() 