#!/usr/bin/env python3
"""
Debug script to analyze specific failed player names from confidence validation test.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.nl_query.parser import BaseQueryParser
from sqlalchemy import create_engine, text
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
            ("Michael Jordan",),  # This seems to be a problem match
            ("Rudy Gobert",),     # This seems to be a problem match
            ("Ja Morant",),       # This seems to be a problem match
        ]

def analyze_failed_names():
    """Analyze the specific names that failed in confidence validation"""
    
    print("🔍 DEBUG: Analyzing Failed Player Names")
    print("=" * 80)
    
    # Initialize parser
    try:
        mock_engine = MockEngine()
        parser = BaseQueryParser(mock_engine)
        print(f"✅ Parser initialized successfully")
    except Exception as e:
        print(f"❌ Failed to initialize parser: {e}")
        return
    
    # Test cases that failed - focusing on the problematic names
    failed_test_cases = [
        {
            "query": "Jayson Tatum with Brown and Smart away games under 38 minutes",
            "problem_names": ["Brown", "Smart"],
            "expected_names": ["Jaylen Brown", "Marcus Smart"],
            "got_names": ["Anthony Davis"]
        },
        {
            "query": "LeBron without AD and Westbrook home games 30+ minutes",
            "problem_names": ["Westbrook"],
            "expected_names": ["Russell Westbrook"],
            "got_names": ["Devin Booker"]
        },
        {
            "query": "Scottie Barnes without OG Anunoby on road games 20-30 minutes",
            "problem_names": ["OG Anunoby"],
            "expected_names": ["OG Anunoby"],
            "got_names": ["Anthony Davis"]
        },
        {
            "query": "Alperen Şengün with Fred VanVleet home games minimum 28 minutes",
            "problem_names": ["Fred VanVleet"],
            "expected_names": ["Fred VanVleet"],
            "got_names": ["Rudy Gobert"]
        },
        {
            "query": "Tatum with Brown home games 30+ minutes",
            "problem_names": ["Brown"],
            "expected_names": ["Jaylen Brown"],
            "got_names": ["Anthony Davis"]
        },
        {
            "query": "Lillard without McCollum road games 25-40 minutes",
            "problem_names": ["McCollum"],
            "expected_names": ["CJ McCollum"],
            "got_names": ["Anthony Davis"]
        },
        {
            "query": "Show me Curry games with the core lineup at home",
            "problem_names": ["Curry"],
            "expected_names": ["Stephen Curry"],
            "got_names": ["LeBron James"]
        },
        {
            "query": "Victor Wembanyama with Devin Vassell home games exactly 30 minutes",
            "problem_names": ["Devin Vassell"],
            "expected_names": ["Devin Vassell"],
            "got_names": ["Not detected"]
        }
    ]
    
    for i, test_case in enumerate(failed_test_cases, 1):
        print(f"\n{'='*80}")
        print(f"FAILED TEST CASE {i}")
        print(f"Query: '{test_case['query']}'")
        print(f"Problem names: {test_case['problem_names']}")
        print(f"Expected: {test_case['expected_names']}")
        print(f"Got: {test_case['got_names']}")
        print(f"{'='*80}")
        
        # Run debug analysis
        try:
            debug_info = parser.debug_spacy_entities(test_case['query'])
            print(f"\n📊 spaCy Debug Info:")
            print(f"   Entities found: {debug_info['entities']}")
            print(f"   Processed query: '{debug_info['processed_query']}'")
            
            # Test each problem name individually
            for problem_name in test_case['problem_names']:
                print(f"\n🔍 Testing individual name extraction for: '{problem_name}'")
                
                # Test _extract_single_player_name directly
                result = parser._extract_single_player_name(problem_name)
                print(f"   _extract_single_player_name('{problem_name}') → {result}")
                
                # Test _extract_last_name directly
                last_name_result = parser._extract_last_name(problem_name)
                print(f"   _extract_last_name('{problem_name}') → {last_name_result}")
                
                # Check if name is in players list
                in_players = problem_name in parser.players
                print(f"   '{problem_name}' in players list: {in_players}")
                
                # Check if name is in aliases
                in_aliases = problem_name.lower() in [alias.lower() for alias in parser.player_aliases.keys()]
                print(f"   '{problem_name}' in aliases: {in_aliases}")
                
                # Check aliases that might match
                matching_aliases = [alias for alias, player in parser.player_aliases.items() 
                                  if problem_name.lower() in alias.lower()]
                print(f"   Matching aliases: {matching_aliases}")
                
                # Show fuzzy matches
                try:
                    from rapidfuzz import process, fuzz
                    fuzzy_matches = process.extract(
                        problem_name, 
                        parser.players, 
                        scorer=fuzz.token_sort_ratio, 
                        limit=5
                    )
                    print(f"   Top 5 fuzzy matches: {fuzzy_matches}")
                except Exception as e:
                    print(f"   Fuzzy matching error: {e}")
                
                # Test if it's a valid player candidate
                is_valid = parser._is_valid_player_candidate(problem_name)
                print(f"   _is_valid_player_candidate('{problem_name}') → {is_valid}")
        
        except Exception as e:
            print(f"❌ Error analyzing test case: {e}")
            traceback.print_exc()
    
    # Additional analysis - check what players are being incorrectly matched
    print(f"\n{'='*80}")
    print(f"🔍 ADDITIONAL ANALYSIS")
    print(f"{'='*80}")
    
    # Check why "Anthony Davis" keeps showing up
    print(f"\n📊 Why 'Anthony Davis' keeps appearing:")
    print(f"   In players list: {'Anthony Davis' in parser.players}")
    print(f"   Aliases pointing to Anthony Davis: {[alias for alias, player in parser.player_aliases.items() if player == 'Anthony Davis']}")
    
    # Check player aliases file
    print(f"\n📊 Player aliases analysis:")
    print(f"   Total aliases: {len(parser.player_aliases)}")
    print(f"   Sample aliases: {list(parser.player_aliases.items())[:10]}")
    
    # Check for problematic patterns
    problematic_patterns = ["Brown", "Westbrook", "McCollum", "Curry", "OG", "VanVleet", "Vassell"]
    for pattern in problematic_patterns:
        matches = [alias for alias in parser.player_aliases.keys() if pattern.lower() in alias.lower()]
        print(f"   Aliases containing '{pattern}': {matches}")

if __name__ == "__main__":
    analyze_failed_names() 