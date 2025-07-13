#!/usr/bin/env python3
"""
Debug script to check which players are missing from spaCy entity ruler.
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
    print("🔍 DEBUG: Checking Missing Entity Patterns")
    print("=" * 80)
    
    # Create parser with mock engine
    parser = BaseQueryParser(MockEngine())
    
    # Get all entity patterns
    entity_ruler = parser.nlp.get_pipe("entity_ruler")
    patterns = entity_ruler.patterns
    
    # Extract all player patterns
    player_patterns = []
    for pattern in patterns:
        if pattern.get("label") == "PLAYER":
            if isinstance(pattern.get("pattern"), str):
                player_patterns.append(pattern["pattern"])
    
    print(f"Total PLAYER patterns found: {len(player_patterns)}")
    print(f"Sample patterns: {player_patterns[:10]}")
    
    # Check which expected players are missing
    expected_players = [
        "LeBron James", "Anthony Davis", "Stephen Curry", "Kevin Durant",
        "Jayson Tatum", "Jaylen Brown", "Damian Lillard", "CJ McCollum",
        "Giannis Antetokounmpo", "Khris Middleton", "Luka Dončić", "Kyrie Irving",
        "Jimmy Butler", "Devin Booker", "Chris Paul", "Draymond Green",
        "Klay Thompson", "Marcus Smart", "Russell Westbrook", "Paolo Banchero",
        "Franz Wagner", "Wendell Carter Jr.", "Shai Gilgeous-Alexander", "Josh Giddey",
        "Zion Williamson", "Victor Wembanyama", "Devin Vassell", "Scottie Barnes",
        "OG Anunoby", "Alperen Şengün", "Fred VanVleet", "Cade Cunningham",
        "Jalen Green", "Evan Mobley", "Donovan Mitchell", "Anthony Edwards",
        "Jalen Brunson", "Bam Adebayo"
    ]
    
    print(f"\n📊 MISSING PLAYERS FROM ENTITY PATTERNS:")
    missing_players = []
    for player in expected_players:
        if player not in player_patterns:
            missing_players.append(player)
            print(f"  ❌ {player}")
    
    if not missing_players:
        print("  ✅ All expected players found in patterns!")
    else:
        print(f"\n⚠️  {len(missing_players)} players missing from entity patterns")
    
    print(f"\n📊 CHECKING ALIASES:")
    # Check key aliases that might be missing
    key_aliases = ["AD", "Draymond", "Book", "CP3", "KD", "Steph", "Tatum", "Brown"]
    
    alias_patterns = []
    for pattern in patterns:
        if pattern.get("label") == "PLAYER":
            if isinstance(pattern.get("pattern"), str):
                alias_patterns.append(pattern["pattern"].lower())
    
    print(f"Total alias patterns found: {len(alias_patterns)}")
    
    missing_aliases = []
    for alias in key_aliases:
        if alias.lower() not in alias_patterns:
            missing_aliases.append(alias)
            print(f"  ❌ {alias}")
    
    if not missing_aliases:
        print("  ✅ All key aliases found in patterns!")
    else:
        print(f"\n⚠️  {len(missing_aliases)} aliases missing from patterns")
    
    print(f"\n📊 TESTING SPECIFIC FAILING CASES:")
    
    failing_cases = [
        ("Tatum with Brown", ["Tatum", "Brown"]),
        ("AD", ["AD"]),
        ("Draymond", ["Draymond"]),  
        ("Book", ["Book"]),
        ("Steph", ["Steph"]),
        ("KD", ["KD"]),
    ]
    
    for query, expected_entities in failing_cases:
        print(f"\n🔍 Testing: '{query}'")
        doc = parser.nlp(query)
        
        found_entities = []
        for ent in doc.ents:
            if ent.label_ == "PLAYER":
                found_entities.append(ent.text)
        
        print(f"  Expected entities: {expected_entities}")
        print(f"  Found entities: {found_entities}")
        
        for expected in expected_entities:
            if expected not in found_entities:
                print(f"  ❌ Missing: {expected}")
                
                # Check if it's in the patterns
                if expected.lower() in alias_patterns:
                    print(f"     - Pattern exists in alias_patterns")
                else:
                    print(f"     - Pattern NOT in alias_patterns")
        
        if found_entities:
            print(f"  ✅ Found some entities")
        else:
            print(f"  ❌ No entities found")

if __name__ == "__main__":
    main() 