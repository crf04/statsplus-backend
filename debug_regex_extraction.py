#!/usr/bin/env python3
"""
Debug script to test regex extraction issues.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.nl_query.parser import BaseQueryParser
import re

class MockEngine:
    def connect(self):
        return MockConnection()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass

class MockConnection:
    def execute(self, query):
        return MockResult()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass

class MockResult:
    def fetchall(self):
        return [
            ("LeBron James",), ("Anthony Davis",), ("Stephen Curry",), ("Kevin Durant",),
            ("Jayson Tatum",), ("Jaylen Brown",), ("Damian Lillard",), ("CJ McCollum",),
            ("Devin Booker",), ("Chris Paul",), ("Russell Westbrook",), ("OG Anunoby",),
            ("Fred VanVleet",), ("Devin Vassell",), ("Scottie Barnes",), ("Marcus Smart",),
        ]

def test_regex_extraction():
    print("🔍 DEBUG: Testing Regex Extraction Issues")
    print("=" * 80)
    
    # Initialize parser
    mock_engine = MockEngine()
    parser = BaseQueryParser(mock_engine)
    
    # Test the problematic text extractions
    test_cases = [
        {
            "query": "Tatum with Brown at home when they're both healthy",
            "expected_extraction": "Brown",
            "description": "Simple with pattern"
        },
        {
            "query": "Lillard without McCollum road games 25-40 minutes",
            "expected_extraction": "McCollum",
            "description": "Simple without pattern"
        },
        {
            "query": "Scottie Barnes without OG Anunoby on road games 20-30 minutes",
            "expected_extraction": "OG Anunoby",
            "description": "Without pattern with compound name"
        }
    ]
    
    for test_case in test_cases:
        print(f"\n{'='*60}")
        print(f"Test: {test_case['description']}")
        print(f"Query: '{test_case['query']}'")
        print(f"Expected: '{test_case['expected_extraction']}'")
        
        query = test_case['query']
        
        # Test the regex patterns used in _extract_players_with_syntax
        with_without_patterns = [
            (r'with\s+(.+?)(?:\s+(?:last|this|recent|home|away|against)|\s*$)', 'with'),
            (r'without\s+(.+?)(?:\s+(?:last|this|recent|home|away|against)|\s*$)', 'without'),
        ]
        
        for pattern, context_type in with_without_patterns:
            matches = re.finditer(pattern, query, re.IGNORECASE)
            for match in matches:
                players_text = match.group(1).strip()
                print(f"\n🔍 Regex Pattern: {pattern}")
                print(f"   Context: {context_type}")
                print(f"   Extracted text: '{players_text}'")
                print(f"   Expected text: '{test_case['expected_extraction']}'")
                
                # Test what _extract_multiple_players returns
                multiple_players = parser._extract_multiple_players(players_text)
                print(f"   _extract_multiple_players result: {multiple_players}")
                
                # Test what _extract_single_player_name returns for the raw text
                single_result = parser._extract_single_player_name(players_text)
                print(f"   _extract_single_player_name result: {single_result}")
                
                # Also test the expected extraction
                expected_result = parser._extract_single_player_name(test_case['expected_extraction'])
                print(f"   Expected '{test_case['expected_extraction']}' → {expected_result}")

if __name__ == "__main__":
    test_regex_extraction() 