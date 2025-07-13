#!/usr/bin/env python3
"""
Debug script to examine the entity database and patterns.
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
        # Mock the actual database data structure
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
    print("🔍 DEBUG: Entity Database & Patterns Analysis")
    print("=" * 80)
    
    # Create parser with mock engine
    parser = BaseQueryParser(MockEngine())
    
    print(f"📊 DATABASE PLAYERS:")
    print(f"Total players loaded: {len(parser.players)}")
    print(f"Players list:")
    for i, player in enumerate(parser.players, 1):
        print(f"  {i:2d}. {player}")
    
    print(f"\n📊 PLAYER ALIASES:")
    print(f"Total aliases loaded: {len(parser.player_aliases)}")
    print(f"Aliases dictionary:")
    
    # Group aliases by player for better readability
    aliases_by_player = {}
    for alias, player in parser.player_aliases.items():
        if player not in aliases_by_player:
            aliases_by_player[player] = []
        aliases_by_player[player].append(alias)
    
    for player in sorted(aliases_by_player.keys()):
        aliases = aliases_by_player[player]
        print(f"  {player}:")
        for alias in aliases:
            print(f"    → '{alias}'")
    
    print(f"\n📊 ENTITY RULER PATTERNS:")
    entity_ruler = parser.nlp.get_pipe("entity_ruler")
    patterns = entity_ruler.patterns
    
    print(f"Total patterns: {len(patterns)}")
    
    # Separate patterns by type
    string_patterns = []
    token_patterns = []
    
    for pattern in patterns:
        if pattern.get("label") == "PLAYER":
            if isinstance(pattern.get("pattern"), str):
                string_patterns.append(pattern)
            elif isinstance(pattern.get("pattern"), list):
                token_patterns.append(pattern)
    
    print(f"\nString patterns ({len(string_patterns)}):")
    for i, pattern in enumerate(string_patterns[:20], 1):  # Show first 20
        print(f"  {i:2d}. '{pattern['pattern']}' → {pattern['id']}")
    if len(string_patterns) > 20:
        print(f"  ... and {len(string_patterns) - 20} more")
    
    print(f"\nToken patterns ({len(token_patterns)}):")
    for i, pattern in enumerate(token_patterns[:10], 1):  # Show first 10
        tokens = [token['LOWER'] for token in pattern['pattern']]
        print(f"  {i:2d}. {tokens} → {pattern['id']}")
    if len(token_patterns) > 10:
        print(f"  ... and {len(token_patterns) - 10} more")
    
    print(f"\n🔍 CHECKING SPECIFIC FAILING PLAYERS:")
    
    failing_players = [
        "Kyrie Irving",
        "Devin Vassell", 
        "OG Anunoby",
        "Fred VanVleet",
        "Devin Booker",  # For "Book" alias
        "Anthony Davis",  # For "AD" alias
        "Draymond Green",  # For "Draymond" alias
        "Stephen Curry",  # For "Steph" alias
        "Kevin Durant",   # For "KD" alias
    ]
    
    for player in failing_players:
        print(f"\n🔍 Analyzing: {player}")
        
        # Check if in player database
        in_database = player in parser.players
        print(f"  In database: {in_database}")
        
        if not in_database:
            print(f"  ❌ MISSING FROM DATABASE!")
            continue
        
        # Check full name pattern
        full_name_pattern = None
        for pattern in string_patterns:
            if pattern['pattern'] == player:
                full_name_pattern = pattern
                break
        
        if full_name_pattern:
            print(f"  Full name pattern: ✅ '{full_name_pattern['pattern']}'")
        else:
            print(f"  Full name pattern: ❌ MISSING")
        
        # Check aliases
        aliases = aliases_by_player.get(player, [])
        print(f"  Aliases ({len(aliases)}): {aliases}")
        
        # Check alias patterns
        alias_patterns = []
        for alias in aliases:
            for pattern in string_patterns:
                if pattern['pattern'] == alias.lower() and pattern['id'] == player:
                    alias_patterns.append(pattern)
        
        print(f"  Alias patterns ({len(alias_patterns)}):")
        for pattern in alias_patterns:
            print(f"    → '{pattern['pattern']}'")
        
        # Test entity recognition
        print(f"  Entity recognition test:")
        
        # Test full name
        doc = parser.nlp(player)
        entities = [(ent.text, ent.label_, ent.ent_id_) for ent in doc.ents if ent.label_ == "PLAYER"]
        if entities:
            print(f"    Full name: ✅ {entities}")
        else:
            print(f"    Full name: ❌ No entity found")
        
        # Test aliases
        for alias in aliases[:3]:  # Test first 3 aliases
            doc = parser.nlp(alias)
            entities = [(ent.text, ent.label_, ent.ent_id_) for ent in doc.ents if ent.label_ == "PLAYER"]
            if entities:
                print(f"    Alias '{alias}': ✅ {entities}")
            else:
                print(f"    Alias '{alias}': ❌ No entity found")
    
    print(f"\n📊 PATTERN STATISTICS:")
    print(f"  Total PLAYER patterns: {len([p for p in patterns if p.get('label') == 'PLAYER'])}")
    print(f"  String patterns: {len(string_patterns)}")
    print(f"  Token patterns: {len(token_patterns)}")
    print(f"  Unique players with patterns: {len(set(p['id'] for p in patterns if p.get('label') == 'PLAYER' and 'id' in p))}")
    
    # Check for duplicates
    pattern_strings = [p['pattern'] for p in string_patterns]
    duplicates = []
    seen = set()
    for pattern in pattern_strings:
        if pattern in seen:
            duplicates.append(pattern)
        seen.add(pattern)
    
    if duplicates:
        print(f"  ⚠️  Duplicate patterns found: {len(duplicates)}")
        for dup in duplicates[:5]:
            print(f"    → '{dup}'")
    else:
        print(f"  ✅ No duplicate patterns found")

if __name__ == "__main__":
    main() 