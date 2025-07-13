#!/usr/bin/env python3
"""
Debug why "Brown at" returns "Anthony Davis".
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.nl_query.parser import BaseQueryParser
from rapidfuzz import process, fuzz

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

def debug_brown_at():
    print("🔍 DEBUG: Why 'Brown at' returns 'Anthony Davis'")
    print("=" * 80)
    
    # Initialize parser
    mock_engine = MockEngine()
    parser = BaseQueryParser(mock_engine)
    
    # Test the problematic text
    test_text = "Brown at"
    print(f"Testing: '{test_text}'")
    
    # Step through the _extract_single_player_name logic manually
    text = test_text.strip()
    text_lower = text.lower()
    
    print(f"\nStep-by-step analysis:")
    print(f"  Input: '{text}'")
    print(f"  Lower: '{text_lower}'")
    
    # STEP 0: Strict full alias match
    print(f"\n📋 STEP 0: Strict full alias match")
    alias_match = None
    for alias in parser.player_aliases:
        if text_lower == alias.strip().lower():
            alias_match = parser.player_aliases[alias]
            break
    print(f"   Result: {alias_match}")
    
    # STEP 1: Exact case-insensitive match
    print(f"\n📋 STEP 1: Exact case-insensitive match")
    exact_match = None
    for player in parser.players:
        if text_lower == player.lower():
            exact_match = player
            break
    print(f"   Result: {exact_match}")
    
    # STEP 2: Short abbreviations
    print(f"\n📋 STEP 2: Short abbreviations")
    short_abbrevs = ['ad', 'kd', 'cp3', 'pg', 'jt', 'sga']
    abbrev_match = None
    for abbrev in short_abbrevs:
        if abbrev in parser.player_aliases:
            if text_lower == abbrev:
                abbrev_match = parser.player_aliases[abbrev]
                break
    print(f"   Result: {abbrev_match}")
    
    # STEP 3: Exact substring matching
    print(f"\n📋 STEP 3: Exact substring matching")
    words = text_lower.split()
    print(f"   Words: {words}")
    substring_match = None
    for length in range(min(4, len(words)), 0, -1):
        for i in range(len(words) - length + 1):
            phrase = ' '.join(words[i:i+length])
            print(f"   Checking phrase: '{phrase}'")
            if len(phrase) > 2:
                for alias in parser.player_aliases.keys():
                    alias_norm = alias.strip().lower()
                    if phrase == alias_norm:
                        substring_match = parser.player_aliases[alias]
                        print(f"   Found match: '{phrase}' → '{substring_match}'")
                        break
                if substring_match:
                    break
        if substring_match:
            break
    print(f"   Result: {substring_match}")
    
    # STEP 4: Last name matching (our fix)
    print(f"\n📋 STEP 4: Last name matching")
    last_name_match = parser._extract_last_name(text)
    print(f"   Result: {last_name_match}")
    
    # STEP 5: Fuzzy matching on aliases
    print(f"\n📋 STEP 5: Fuzzy matching on aliases")
    alias_fuzzy_match = None
    for i in range(len(words)):
        for j in range(i + 1, min(i + 4, len(words) + 1)):
            phrase = ' '.join(words[i:j])
            print(f"   Checking phrase: '{phrase}'")
            if len(phrase) > 2:
                alias_match = process.extractOne(
                    phrase,
                    [a.strip().lower() for a in parser.player_aliases.keys()],
                    scorer=fuzz.partial_ratio,
                    score_cutoff=90  # Our increased threshold
                )
                if alias_match:
                    print(f"   Found fuzzy alias match: '{phrase}' → '{alias_match[0]}' (score: {alias_match[1]})")
                    matched_alias = alias_match[0]
                    for alias in parser.player_aliases.keys():
                        if alias.strip().lower() == matched_alias:
                            alias_fuzzy_match = parser.player_aliases[alias]
                            break
                    break
        if alias_fuzzy_match:
            break
    print(f"   Result: {alias_fuzzy_match}")
    
    # STEP 6: Direct fuzzy matching
    print(f"\n📋 STEP 6: Direct fuzzy matching")
    direct_fuzzy_match = process.extractOne(
        text,
        parser.players,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=75
    )
    print(f"   Result: {direct_fuzzy_match}")
    
    # Final result
    final_result = parser._extract_single_player_name(text)
    print(f"\n🎯 FINAL RESULT: {final_result}")

if __name__ == "__main__":
    debug_brown_at() 