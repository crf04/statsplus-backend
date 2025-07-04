#!/usr/bin/env python3
"""
🚀 Quick NBA Query Test - Test a single query instantly

Usage:
    python quick_test.py "LeBron James last 10 games"
    python quick_test.py "Stephen Curry this season"
"""

import sys
import os
import json
from sqlalchemy import create_engine

# Add the app directory to the path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

from app.services.nl_query.parser import BaseQueryParser
from app.services.nl_query.parameter_mapper import ParameterMapper
from app.services.nl_query.executor import QueryExecutor

def quick_test(query):
    """Test a single NBA query quickly"""
    print(f"🏀 Quick NBA Query Test")
    print(f"Query: '{query}'")
    print("=" * 60)
    
    try:
        # Initialize system
        print("⚙️  Initializing NBA system...")
        engine = create_engine('sqlite:///nba_play_types.db')
        parser = BaseQueryParser(engine)
        mapper = ParameterMapper()
        executor = QueryExecutor(engine)
        
        # Step 1: Parse
        print("🔍 Parsing query...")
        components = parser.parse(query)
        
        # Step 2: Map to API
        print("🗺️  Mapping to API parameters...")
        api_info = mapper.map_to_api_params(components)
        
        # Debug: Show API call details
        print(f"\n🔧 DEBUG - API Call Details:")
        print(f"   📍 Endpoint: {api_info['endpoint']}")
        print(f"   📝 Description: {api_info.get('description', 'N/A')}")
        print(f"   📋 Raw Parameters:")
        for key, value in api_info['parameters'].items():
            print(f"      • {key}: {value}")
        
        if api_info.get('api_calls'):
            print(f"   🔗 Actual API Calls ({len(api_info['api_calls'])}):")
            for i, call in enumerate(api_info['api_calls'], 1):
                print(f"      {i}. {call['method']} → {call['service']}")
                print(f"         Purpose: {call.get('purpose', 'N/A')}")
                if call.get('parameters'):
                    print(f"         Final Parameters:")
                    for param_key, param_val in call['parameters'].items():
                        print(f"           ◦ {param_key}: {param_val}")
        
        # Step 3: Execute
        print("🚀 Executing NBA API...")
        results = executor.execute_query(api_info)
        
        # Display results
        if "error" in results:
            print(f"❌ Error: {results['error']}")
            return
        
        print(f"✅ Success! ({results['execution_time']})")
        print("\n📊 RESULTS:")
        print("=" * 30)
        
        if results['summary'] and 'stats' in results['summary']:
            stats = results['summary']['stats']
            print(f"🎯 Player: {components.player_name}")
            print(f"🏀 Games: {stats['games_analyzed']}")
            print(f"📈 PPG: {stats['avg_points']}")
            print(f"📈 RPG: {stats['avg_rebounds']}")
            print(f"📈 APG: {stats['avg_assists']}")
            
            # Show recent games
            if results['data'] and 'game_logs' in results['data']:
                game_logs = json.loads(results['data']['game_logs'])
                if game_logs:
                    print(f"\n🎮 Recent Games:")
                    for i, game in enumerate(game_logs[:3], 1):
                        pts = game.get('PTS', 0)
                        reb = game.get('REB', 0)
                        ast = game.get('AST', 0)
                        matchup = game.get('MATCHUP', 'N/A')
                        date = game.get('GAME_DATE', 'N/A')[-5:]  # Last 5 chars (MM-DD)
                        print(f"   {i}. {date}: {pts}P/{reb}R/{ast}A vs {matchup}")
        
        print("\n" + "=" * 30)
        print("✅ Test completed!")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        print("💡 Make sure the NBA database is available.")

def main():
    """Main function"""
    if len(sys.argv) != 2:
        print("Usage: python quick_test.py \"Your NBA query here\"")
        print("\nExamples:")
        print("  python quick_test.py \"LeBron James last 10 games\"")
        print("  python quick_test.py \"Stephen Curry this season\"")
        print("  python quick_test.py \"Giannis against top 5 defenses\"")
        sys.exit(1)
    
    query = sys.argv[1]
    quick_test(query)

if __name__ == "__main__":
    main() 