#!/usr/bin/env python3
"""
Debug script to test problematic short aliases.
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
            ("Giannis Antetokounmpo",), ("Khris Middleton",), ("Luka Dončić",), ("Kyrie Irving",),
            ("Jimmy Butler",), ("Devin Booker",), ("Chris Paul",), ("Draymond Green",),
            ("Klay Thompson",), ("Marcus Smart",), ("Russell Westbrook",), ("Paolo Banchero",),
            ("Franz Wagner",), ("Wendell Carter Jr.",), ("Shai Gilgeous-Alexander",), ("Josh Giddey",),
            ("Zion Williamson",), ("Victor Wembanyama",), ("Devin Vassell",), ("Scottie Barnes",),
            ("OG Anunoby",), ("Alperen Şengün",), ("Fred VanVleet",), ("Cade Cunningham",),
            ("Jalen Green",), ("Evan Mobley",), ("Donovan Mitchell",), ("Anthony Edwards",),
            ("Jalen Brunson",), ("Bam Adebayo",)
        ]

def main():
    print("🔍 DEBUG: Problematic Short Aliases")
    print("=" * 80)
    
    # Create parser with mock engine
    parser = BaseQueryParser(MockEngine())
    
    # Test problematic aliases
    problematic_aliases = [
        ("AD", "Anthony Davis"),
        ("Draymond", "Draymond Green"), 
        ("CP3", "Chris Paul"),
        ("Book", "Devin Booker"),
        ("Steph", "Stephen Curry"),
        ("KD", "Kevin Durant"),
        ("Tatum", "Jayson Tatum"),
        ("Brown", "Jaylen Brown"),
    ]
    
    print("📊 INDIVIDUAL ALIAS TESTS:")
    for alias, expected_player in problematic_aliases:
        print(f"\n🔍 Testing: '{alias}'")
        doc = parser.nlp(alias)
        
        entities = [(ent.text, ent.label_, ent.ent_id_) for ent in doc.ents]
        print(f"  Entities: {entities}")
        
        # Check if pattern exists
        entity_ruler = parser.nlp.get_pipe("entity_ruler")
        pattern_exists = any(
            p.get("pattern") == alias.lower() and p.get("id") == expected_player 
            for p in entity_ruler.patterns
        )
        print(f"  Pattern exists: {pattern_exists}")
        
        # Test case variations
        variations = [alias, alias.lower(), alias.upper()]
        for var in variations:
            doc_var = parser.nlp(var)
            entities_var = [(ent.text, ent.label_, ent.ent_id_) for ent in doc_var.ents if ent.label_ == "PLAYER"]
            if entities_var:
                print(f"  '{var}' → ✅ {entities_var}")
            else:
                print(f"  '{var}' → ❌ No entity")
    
    print(f"\n📊 MINIMAL CONTEXT TESTS:")
    minimal_contexts = [
        "with AD",
        "with Draymond", 
        "CP3 games",
        "Book games",
        "Steph games",
        "KD games",
        "Tatum games",
        "Brown games",
    ]
    
    for context in minimal_contexts:
        print(f"\n🔍 Testing: '{context}'")
        doc = parser.nlp(context)
        
        entities = [(ent.text, ent.label_, ent.ent_id_) for ent in doc.ents if ent.label_ == "PLAYER"]
        if entities:
            print(f"  ✅ {entities}")
        else:
            print(f"  ❌ No entities found")
            
            # Show all tokens
            for token in doc:
                if token.text.lower() in ["ad", "draymond", "cp3", "book", "steph", "kd", "tatum", "brown"]:
                    print(f"    Token '{token.text}': pos={token.pos_}, lemma='{token.lemma_}', shape='{token.shape_}'")
    
    print(f"\n📊 PATTERN INSPECTION:")
    entity_ruler = parser.nlp.get_pipe("entity_ruler")
    
    for alias, expected_player in problematic_aliases:
        print(f"\n🔍 Patterns for '{alias}' → {expected_player}:")
        
        # Find all patterns for this alias/player
        matching_patterns = []
        for pattern in entity_ruler.patterns:
            if pattern.get("id") == expected_player and isinstance(pattern.get("pattern"), str):
                if pattern["pattern"] == alias.lower():
                    matching_patterns.append(pattern)
        
        if matching_patterns:
            for pattern in matching_patterns:
                print(f"  ✅ Found: {pattern}")
        else:
            print(f"  ❌ No patterns found for '{alias.lower()}' → {expected_player}")
            
            # Show what patterns DO exist for this player
            player_patterns = [
                p for p in entity_ruler.patterns 
                if p.get("id") == expected_player and isinstance(p.get("pattern"), str)
            ][:5]  # Show first 5
            
            print(f"    Available patterns for {expected_player}:")
            for pattern in player_patterns:
                print(f"      → '{pattern['pattern']}'")

if __name__ == "__main__":
    main() 