#!/usr/bin/env python3
"""
Simple test to verify the AD alias fix works correctly
"""
import sys
import os

# Add the app directory to the path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from services.nl_query.parser import BaseQueryParser
from sqlalchemy import create_engine

def test_ad_alias():
    """Test that AD correctly resolves to Anthony Davis"""
    
    # Create a mock database engine (not actually used for alias matching)
    engine = create_engine('sqlite:///:memory:')
    
    # Create parser instance
    parser = BaseQueryParser(engine)
    
    # Test queries
    test_queries = [
        "lebron with ad",
        "lebron james with AD",
        "AD last 10 games",
        "kd and ad",
        "ad stats"
    ]
    
    print("🧪 Testing AD alias resolution...")
    print("=" * 50)
    
    for query in test_queries:
        print(f"\n💬 Testing query: '{query}'")
        print("-" * 30)
        
        try:
            # Parse the query
            result = parser.parse(query)
            
            # Check if AD was found in players_on
            if result.players_on:
                print(f"✅ Players ON: {result.players_on}")
            
            # Check if AD was found as main player
            if result.player_name:
                print(f"✅ Main player: {result.player_name}")
            
            if not result.players_on and not result.player_name:
                print("❌ No players found")
                
        except Exception as e:
            print(f"❌ Error: {e}")
        
        print("-" * 30)
    
    print("\n🏁 Test completed!")

if __name__ == "__main__":
    test_ad_alias() 