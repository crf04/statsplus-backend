#!/usr/bin/env python3

"""
Debug script to investigate CP fragment extraction issues.
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
        # Mock player data
        class MockResult:
            def fetchall(self):
                return [
                    ("Chris Paul",), 
                    ("Luka Doncic",),
                    ("Russell Westbrook",),
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

def debug_cp_fragments():
    """Debug CP fragment extraction issues"""
    
    print("=== CP Fragment Extraction Debug ===")
    
    # Create parser
    parser = BaseQueryParser(MockEngine())
    
    # Test problematic fragments
    test_fragments = [
        "cp",
        "cp points",
        "cp triple double",
        "cp assist",
        "cp game",
        "cp performance",
        "cp rebounds",
        "cp3",
        "points",
        "triple",
        "double",
        "triple double"
    ]
    
    print("Testing fragment extraction...")
    
    for fragment in test_fragments:
        print(f"\nTesting: '{fragment}'")
        result = parser._extract_single_player_name(fragment)
        print(f"  -> '{result}'")
        
        # If it extracts something, let's see the step-by-step process
        if result:
            print(f"  Step-by-step analysis:")
            text_lower = fragment.lower()
            
            # Check alias match
            for alias in parser.player_aliases:
                if text_lower == alias.strip().lower():
                    print(f"    ✅ STEP 0 - Alias match: '{alias}' -> '{parser.player_aliases[alias]}'")
                    break
            
            # Check fuzzy matching
            from rapidfuzz import process, fuzz
            
            # Step 3: substring matching
            words = text_lower.split()
            for length in range(min(4, len(words)), 0, -1):
                for i in range(len(words) - length + 1):
                    phrase = ' '.join(words[i:i+length])
                    if len(phrase) > 2:
                        for alias in parser.player_aliases.keys():
                            alias_norm = alias.strip().lower()
                            if phrase == alias_norm:
                                print(f"    ✅ STEP 3 - Substring match: '{phrase}' matches alias '{alias_norm}'")
                                break
            
            # Step 5: Fuzzy matching
            alias_match = process.extractOne(
                fragment,
                [a.strip().lower() for a in parser.player_aliases.keys()],
                scorer=fuzz.partial_ratio,
                score_cutoff=90
            )
            if alias_match:
                print(f"    ✅ STEP 5 - Fuzzy alias match: '{fragment}' -> '{alias_match[0]}' (score: {alias_match[1]})")
            
            # Step 6: Direct fuzzy matching
            match = process.extractOne(
                fragment,
                parser.players,
                scorer=fuzz.token_sort_ratio,
                score_cutoff=85
            )
            if match:
                print(f"    ✅ STEP 6 - Direct fuzzy match: '{fragment}' -> '{match[0]}' (score: {match[1]})")
    
    # Test specific aliases that might be causing issues
    print("\n" + "="*60)
    print("Checking specific aliases that might match 'cp':")
    
    cp_related_aliases = []
    for alias in parser.player_aliases.keys():
        if "cp" in alias.lower() or len(alias) <= 3:
            cp_related_aliases.append(alias)
    
    print(f"Found {len(cp_related_aliases)} potentially problematic aliases:")
    for alias in cp_related_aliases:
        print(f"  '{alias}' -> '{parser.player_aliases[alias]}'")
    
    # Test fuzzy matching scores
    print("\n" + "="*60)
    print("Testing fuzzy matching scores for 'cp triple double':")
    
    from rapidfuzz import process, fuzz
    
    test_phrase = "cp triple double"
    all_aliases = [a.strip().lower() for a in parser.player_aliases.keys()]
    
    matches = process.extract(
        test_phrase,
        all_aliases,
        scorer=fuzz.partial_ratio,
        limit=10
    )
    
    print(f"Top 10 fuzzy matches for '{test_phrase}':")
    for match_result in matches:
        match, score = match_result[0], match_result[1]
        if score >= 70:  # Show scores above 70
            # Find the original alias
            for alias in parser.player_aliases.keys():
                if alias.strip().lower() == match:
                    print(f"  '{match}' -> '{parser.player_aliases[alias]}' (score: {score})")
                    break

if __name__ == "__main__":
    debug_cp_fragments() 