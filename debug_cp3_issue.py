#!/usr/bin/env python3

"""
Debug script to investigate the CP3 issue with the NBA query parser.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.nl_query.parser import BaseQueryParser
import yaml

class MockEngine:
    def connect(self):
        return self
    
    def execute(self, query):
        # Mock player data including Chris Paul
        class MockResult:
            def fetchall(self):
                return [
                    ("Chris Paul",), 
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

def debug_cp3_issue():
    """Debug the CP3 recognition issue"""
    
    print("=== CP3 Issue Debug ===")
    
    # Create parser
    parser = BaseQueryParser(MockEngine())
    
    # Load aliases manually to check what's loaded
    print("\n1. Checking loaded aliases...")
    print(f"Total aliases loaded: {len(parser.player_aliases)}")
    
    # Check if cp3 is in aliases
    cp3_variations = ['cp3', 'CP3', 'Cp3', 'cP3']
    for variation in cp3_variations:
        if variation in parser.player_aliases:
            print(f"✅ Found '{variation}' -> '{parser.player_aliases[variation]}'")
        else:
            print(f"❌ '{variation}' not found in aliases")
    
    # Check the actual alias keys
    cp3_related = [k for k in parser.player_aliases.keys() if 'cp3' in k.lower()]
    print(f"CP3-related aliases found: {cp3_related}")
    
    # Test direct extraction
    print("\n2. Testing direct extraction...")
    test_inputs = ['cp3', 'CP3', 'Cp3', 'cP3']
    
    for test_input in test_inputs:
        result = parser._extract_single_player_name(test_input)
        print(f"_extract_single_player_name('{test_input}') -> {result}")
    
    # Test the full parsing
    print("\n3. Testing full query parsing...")
    test_queries = [
        "cp3 last 10 games",
        "CP3 last 10 games", 
        "Cp3 last 10 games",
        "Chris Paul last 10 games"
    ]
    
    for query in test_queries:
        result = parser.parse(query)
        print(f"'{query}' -> player_name: '{result.player_name}', confidence: {result.confidence:.3f}")
    
    # Test the step-by-step process
    print("\n4. Step-by-step analysis for 'cp3'...")
    text = "cp3"
    text_lower = text.lower()
    
    print(f"Input: '{text}' -> lowercase: '{text_lower}'")
    
    # Step 0: Check alias match
    print("\nSTEP 0: Alias matching...")
    for alias in parser.player_aliases:
        if text_lower == alias.strip().lower():
            print(f"✅ STEP 0 MATCH: '{alias}' -> '{parser.player_aliases[alias]}'")
            break
    else:
        print("❌ No alias match in STEP 0")
    
    # Step 2: Check short abbreviations
    print("\nSTEP 2: Short abbreviations...")
    short_abbrevs = ['ad', 'kd', 'cp3', 'pg', 'jt', 'sga']
    for abbrev in short_abbrevs:
        if abbrev in parser.player_aliases:
            print(f"Abbrev '{abbrev}' found in aliases")
            if text_lower == abbrev:
                print(f"✅ STEP 2 MATCH: '{abbrev}' -> '{parser.player_aliases[abbrev]}'")
                break
        else:
            print(f"❌ Abbrev '{abbrev}' NOT found in aliases")
    
    # Check spaCy entity recognition
    print("\n5. Testing spaCy entity recognition...")
    test_query = "cp3 last 10 games"
    debug_info = parser.debug_spacy_entities(test_query)
    print(f"spaCy entities for '{test_query}': {debug_info['entities']}")

if __name__ == "__main__":
    debug_cp3_issue() 