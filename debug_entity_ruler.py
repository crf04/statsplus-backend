#!/usr/bin/env python3
"""
Debug script to test if spaCy entity ruler is working.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.nl_query.parser import BaseQueryParser

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
        ]

def main():
    print("🔍 DEBUG: Testing spaCy Entity Ruler")
    print("=" * 80)
    
    # Create parser with mock engine
    parser = BaseQueryParser(MockEngine())
    
    # Check pipeline components
    print(f"spaCy pipeline components: {parser.nlp.pipe_names}")
    
    # Get entity ruler
    entity_ruler = parser.nlp.get_pipe("entity_ruler")
    print(f"Entity ruler patterns count: {len(entity_ruler.patterns)}")
    
    # Test very simple cases
    test_cases = [
        "LeBron James",
        "Anthony Davis", 
        "LeBron",
        "AD",
        "Tatum",
        "Brown",
        "Stephen Curry",
        "Steph"
    ]
    
    for test_case in test_cases:
        print(f"\n🔍 Testing: '{test_case}'")
        doc = parser.nlp(test_case)
        
        # Show all entities found
        entities = []
        for ent in doc.ents:
            entities.append((ent.text, ent.label_, ent.ent_id_))
        
        print(f"  Entities found: {entities}")
        
        # Show tokens
        tokens = []
        for token in doc:
            tokens.append((token.text, token.pos_, token.ent_type_))
        
        print(f"  Tokens: {tokens}")
        
        # Check if pattern exists
        pattern_exists = False
        for pattern in entity_ruler.patterns:
            if pattern.get("pattern") == test_case or pattern.get("pattern") == test_case.lower():
                pattern_exists = True
                print(f"  Pattern found: {pattern}")
                break
        
        if not pattern_exists:
            print(f"  No exact pattern found for '{test_case}'")
        
        # Check if any entity was found
        if entities:
            print(f"  ✅ SUCCESS: Entity found")
        else:
            print(f"  ❌ FAIL: No entity found")
    
    # Test with a longer query
    print(f"\n🔍 Testing longer query: 'LeBron James with Anthony Davis'")
    doc = parser.nlp("LeBron James with Anthony Davis")
    
    entities = []
    for ent in doc.ents:
        entities.append((ent.text, ent.label_, ent.ent_id_))
    
    print(f"  Entities found: {entities}")
    
    # Check if entity ruler is enabled
    print(f"\n📊 Entity ruler info:")
    print(f"  Enabled: {not entity_ruler.disabled}")
    print(f"  Overwrite entities: {entity_ruler.overwrite}")
    print(f"  Patterns count: {len(entity_ruler.patterns)}")
    
    # Show first few patterns
    print(f"\n📊 First 10 patterns:")
    for i, pattern in enumerate(entity_ruler.patterns[:10]):
        print(f"  {i+1}. {pattern}")

if __name__ == "__main__":
    main() 