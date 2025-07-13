#!/usr/bin/env python3
"""
Debug script to analyze fuzzy matching issues in _extract_single_player_name.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.nl_query.parser import BaseQueryParser
from rapidfuzz import process, fuzz
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

def debug_fuzzy_matching_step_by_step():
    """Debug the fuzzy matching step by step for failing names"""
    
    print("🔍 DEBUG: Fuzzy Matching Step-by-Step Analysis")
    print("=" * 80)
    
    # Initialize parser
    try:
        mock_engine = MockEngine()
        parser = BaseQueryParser(mock_engine)
        print(f"✅ Parser initialized successfully")
    except Exception as e:
        print(f"❌ Failed to initialize parser: {e}")
        return
    
    # Test problematic names
    problematic_names = [
        "Brown",
        "Westbrook", 
        "McCollum",
        "Curry",
        "Smart"
    ]
    
    for name in problematic_names:
        print(f"\n{'='*80}")
        print(f"🔍 ANALYZING: '{name}'")
        print(f"{'='*80}")
        
        text = name.strip()
        text_lower = text.lower()
        
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
        substring_match = None
        for length in range(min(4, len(words)), 0, -1):
            for i in range(len(words) - length + 1):
                phrase = ' '.join(words[i:i+length])
                if len(phrase) > 2:
                    for alias in parser.player_aliases.keys():
                        alias_norm = alias.strip().lower()
                        if phrase == alias_norm:
                            substring_match = parser.player_aliases[alias]
                            break
                    if substring_match:
                        break
            if substring_match:
                break
        print(f"   Result: {substring_match}")
        
        # STEP 4: Fuzzy matching on aliases
        print(f"\n📋 STEP 4: Fuzzy matching on aliases")
        alias_fuzzy_match = None
        for i in range(len(words)):
            for j in range(i + 1, min(i + 4, len(words) + 1)):
                phrase = ' '.join(words[i:j])
                if len(phrase) > 2:
                    alias_match = process.extractOne(
                        phrase,
                        [a.strip().lower() for a in parser.player_aliases.keys()],
                        scorer=fuzz.partial_ratio,
                        score_cutoff=85
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
        
        # STEP 5: Direct fuzzy matching against player database
        print(f"\n📋 STEP 5: Direct fuzzy matching against player database")
        player_fuzzy_match = process.extractOne(
            text,
            parser.players,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=75
        )
        print(f"   Input: '{text}'")
        print(f"   Match: {player_fuzzy_match}")
        
        # Show top 10 fuzzy matches regardless of cutoff
        all_matches = process.extract(
            text,
            parser.players,
            scorer=fuzz.token_sort_ratio,
            limit=10
        )
        print(f"   Top 10 matches (regardless of cutoff):")
        for i, (player, score, idx) in enumerate(all_matches):
            marker = "✅" if score >= 75 else "❌"
            print(f"      {i+1}. {marker} {player} (score: {score})")
        
        # STEP 6: Last name matching
        print(f"\n📋 STEP 6: Last name matching")
        last_name_match = parser._extract_last_name(text)
        print(f"   Result: {last_name_match}")
        
        # Final result
        print(f"\n🎯 FINAL RESULT:")
        final_result = parser._extract_single_player_name(text)
        print(f"   _extract_single_player_name('{text}') → {final_result}")
        
        # Show which step won
        if final_result:
            if alias_match == final_result:
                print(f"   ✅ Winner: STEP 0 (Strict alias match)")
            elif exact_match == final_result:
                print(f"   ✅ Winner: STEP 1 (Exact match)")
            elif abbrev_match == final_result:
                print(f"   ✅ Winner: STEP 2 (Short abbreviation)")
            elif substring_match == final_result:
                print(f"   ✅ Winner: STEP 3 (Substring match)")
            elif alias_fuzzy_match == final_result:
                print(f"   ✅ Winner: STEP 4 (Fuzzy alias match)")
            elif player_fuzzy_match and player_fuzzy_match[0] == final_result:
                print(f"   ✅ Winner: STEP 5 (Direct fuzzy match)")
            elif last_name_match == final_result:
                print(f"   ✅ Winner: STEP 6 (Last name match)")
            else:
                print(f"   ❓ Winner: UNKNOWN")
        else:
            print(f"   ❌ No match found")

if __name__ == "__main__":
    debug_fuzzy_matching_step_by_step() 