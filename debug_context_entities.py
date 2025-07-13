#!/usr/bin/env python3
"""
Debug script to test entity recognition in context of failing queries.
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
    print("🔍 DEBUG: Entity Recognition in Query Context")
    print("=" * 80)
    
    # Create parser with mock engine
    parser = BaseQueryParser(MockEngine())
    
    # Test the actual failing queries
    failing_queries = [
        "LeBron James with AD at home games with 35+ minutes",
        "Stephen Curry without Draymond on road games with 30+ minutes", 
        "Luka Dončić with Kyrie Irving at home under 40 minutes",
        "CP3 with Book on road games less than 35 minutes",
        "Steph Curry with KD and Klay Thompson home games 32+ minutes",
        "Victor Wembanyama with Devin Vassell home games exactly 30 minutes",
        "Scottie Barnes without OG Anunoby on road games 20-30 minutes",
        "Alperen Şengün with Fred VanVleet home games minimum 28 minutes",
        "Tatum with Brown at home when they're both healthy",
    ]
    
    for query in failing_queries:
        print(f"\n🔍 Testing: '{query}'")
        doc = parser.nlp(query)
        
        # Show all entities found
        entities = []
        for ent in doc.ents:
            entities.append((ent.text, ent.label_, ent.ent_id_, ent.start_char, ent.end_char))
        
        print(f"  All entities found: {len(entities)}")
        for ent in entities:
            print(f"    {ent[0]} ({ent[1]}) → {ent[2]} [{ent[3]}:{ent[4]}]")
        
        # Check specifically for PLAYER entities
        player_entities = [ent for ent in entities if ent[1] == "PLAYER"]
        print(f"  PLAYER entities: {len(player_entities)}")
        for ent in player_entities:
            print(f"    ✅ {ent[0]} → {ent[2]}")
        
        # Check for missing expected players
        expected_players = {
            "LeBron James with AD at home games with 35+ minutes": ["LeBron James", "Anthony Davis"],
            "Stephen Curry without Draymond on road games with 30+ minutes": ["Stephen Curry", "Draymond Green"], 
            "Luka Dončić with Kyrie Irving at home under 40 minutes": ["Luka Dončić", "Kyrie Irving"],
            "CP3 with Book on road games less than 35 minutes": ["Chris Paul", "Devin Booker"],
            "Steph Curry with KD and Klay Thompson home games 32+ minutes": ["Stephen Curry", "Kevin Durant", "Klay Thompson"],
            "Victor Wembanyama with Devin Vassell home games exactly 30 minutes": ["Victor Wembanyama", "Devin Vassell"],
            "Scottie Barnes without OG Anunoby on road games 20-30 minutes": ["Scottie Barnes", "OG Anunoby"],
            "Alperen Şengün with Fred VanVleet home games minimum 28 minutes": ["Alperen Şengün", "Fred VanVleet"],
            "Tatum with Brown at home when they're both healthy": ["Jayson Tatum", "Jaylen Brown"],
        }
        
        expected = expected_players.get(query, [])
        found_player_names = [ent[2] for ent in player_entities if ent[2]]
        
        print(f"  Expected players: {expected}")
        print(f"  Found players: {found_player_names}")
        
        missing = []
        for exp_player in expected:
            if exp_player not in found_player_names:
                missing.append(exp_player)
        
        if missing:
            print(f"  ❌ Missing players: {missing}")
        else:
            print(f"  ✅ All expected players found!")
        
        # Show specific issues
        print(f"  Detailed analysis:")
        
        # Look for problematic tokens
        for token in doc:
            if token.text.lower() in ["ad", "draymond", "kyrie", "book", "steph", "kd", "tatum", "brown"]:
                print(f"    Token '{token.text}': pos={token.pos_}, ent_type_={token.ent_type_}, ent_iob_={token.ent_iob_}")
    
    print(f"\n📊 SUMMARY:")
    print(f"This test will help us understand why entities work individually but fail in context.")

if __name__ == "__main__":
    main() 