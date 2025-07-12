#!/usr/bin/env python3
"""
🏀 NBA Natural Language Query Interactive Tester

Test your own natural language queries and see REAL NBA data results!

Usage:
    python nba_query_tester.py                 # Interactive mode
    python nba_query_tester.py --examples      # Show example queries
"""

import sys
import os
import argparse
import json
from sqlalchemy import create_engine

# Add the app directory to the path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

from app.services.nl_query.parser import BaseQueryParser
from app.services.nl_query.parameter_mapper import ParameterMapper
from app.services.nl_query.executor import QueryExecutor

class NBAQueryTester:
    """Interactive NBA query testing system"""
    
    def __init__(self):
        """Initialize the real NBA query system"""
        print("🏀 Initializing NBA Query System...")
        
        # Use the real database (same as app.py)
        self.engine = create_engine('sqlite:///nba_play_types.db')
        
        # Initialize the complete pipeline
        self.parser = BaseQueryParser(self.engine)
        self.mapper = ParameterMapper()
        self.executor = QueryExecutor(self.engine)
        
        print("✅ NBA Query System ready!")
    
    def interactive_mode(self):
        """Interactive testing mode with real NBA data execution"""
        print("\n" + "=" * 80)
        print("🏀 NBA NATURAL LANGUAGE QUERY - INTERACTIVE TESTER")
        print("=" * 80)
        print("Enter natural language queries and see REAL NBA data!")
        print()
        print("Commands:")
        print("  • Type any NBA query (e.g., 'LeBron James last 10 games')")
        print("  • 'examples' - Show sample queries")
        print("  • 'debug-players' - Show loaded players and check specific names")
        print("  • 'help' - Show this help")
        print("  • 'quit', 'exit', 'q' - Exit")
        print("=" * 80)
        
        while True:
            try:
                query = input("\n🎯 NBA Query: ").strip()
                
                if query.lower() in ['quit', 'exit', 'q']:
                    print("👋 Thanks for using NBA Query Tester!")
                    break
                
                if query.lower() == 'examples':
                    self.show_examples()
                    continue
                
                if query.lower() == 'help':
                    self.show_help()
                    continue
                
                if query.lower() == 'debug-players':
                    self.debug_players()
                    continue
                
                if not query:
                    print("❌ Please enter a query!")
                    continue
                
                # Execute the complete pipeline
                self.execute_and_display_query(query)
                
            except KeyboardInterrupt:
                print("\n👋 Thanks for using NBA Query Tester!")
                break
            except Exception as e:
                print(f"❌ Error: {e}")
                print("💡 Try a different query or type 'examples' for sample queries.")
    
    def execute_and_display_query(self, query):
        """Execute a complete natural language query and display results"""
        print(f"\n📋 Processing: '{query}'")
        print("-" * 60)
        
        try:
            # Step 1: Parse the natural language
            components = self.parser.parse(query)
            
            print(f"✅ Understood: {components.player_name or 'NBA query'}")
            if components.game_count:
                print(f"✅ Time period: Last {components.game_count} games")
            elif components.time_period == "season":
                print(f"✅ Time period: This season")
            
            if components.opponent_filters:
                filter_desc = []
                for stat, rank in components.opponent_filters:
                    filter_desc.append(f"top {rank}" if rank > 0 else f"worst {abs(rank)}")
                print(f"✅ Filters: Against {', '.join(filter_desc)} teams")
            
            if components.players_on:
                count = len(components.players_on)
                plural = "player" if count == 1 else "players"
                print(f"✅ Playing with ({count} {plural}): {', '.join(components.players_on)}")
            
            if components.players_off:
                count = len(components.players_off)
                plural = "player" if count == 1 else "players"
                print(f"✅ Playing without ({count} {plural}): {', '.join(components.players_off)}")
            
            print(f"✅ Confidence: {components.confidence:.0%}")
            
            if components.confidence < 0.6:
                print("⚠️  Low confidence - results may not be accurate")
            
            # Step 2: Map to API parameters
            api_info = self.mapper.map_to_api_params(components)
            
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
            
            # Step 3: Execute real API call
            print(f"\n🚀 Executing real NBA API call...")
            results = self.executor.execute_query(api_info)
            
            if "error" in results:
                print(f"❌ Execution Error: {results['error']}")
                return
            
            # Step 4: Display real results
            print(f"✅ Success! ({results['execution_time']})")
            self.display_nba_results(results)
            
        except Exception as e:
            print(f"❌ Error processing query: {e}")
            print("💡 Try rephrasing your query or type 'examples' for samples.")
    
    def debug_players(self):
        """Debug loaded players and check for specific names"""
        print(f"\n🔍 PLAYER DATABASE DEBUG")
        print("=" * 50)
        
        # Show basic stats
        total_players = len(self.parser.players)
        total_aliases = len(self.parser.player_aliases)
        print(f"📊 Total players loaded: {total_players}")
        print(f"📊 Total aliases loaded: {total_aliases}")
        
        # Check for Austin Reaves specifically
        austin_variants = [
            "Austin Reaves",
            "Austin Tyler Reaves", 
            "A. Reaves",
            "Reaves, Austin"
        ]
        
        print(f"\n🔍 Checking for Austin Reaves variants:")
        found_austin = False
        for variant in austin_variants:
            if variant in self.parser.players:
                print(f"  ✅ Found: '{variant}'")
                found_austin = True
            else:
                print(f"  ❌ Not found: '{variant}'")
        
        # Search for any player with "Reaves"
        reaves_players = [p for p in self.parser.players if "reaves" in p.lower()]
        if reaves_players:
            print(f"\n📝 Players with 'Reaves' in name:")
            for player in reaves_players:
                print(f"  • {player}")
        else:
            print(f"\n❌ No players found with 'Reaves' in name")
        
        # Show a sample of loaded players
        print(f"\n📋 Sample of loaded players:")
        sample_players = self.parser.players[:10]
        for i, player in enumerate(sample_players, 1):
            print(f"  {i}. {player}")
        
        if total_players > 10:
            print(f"  ... and {total_players - 10} more players")
        
        # Check aliases for Austin
        austin_aliases = {k: v for k, v in self.parser.player_aliases.items() 
                         if "austin" in k.lower() or "austin" in v.lower()}
        if austin_aliases:
            print(f"\n🏷️  Austin-related aliases:")
            for alias, player in austin_aliases.items():
                print(f"  • '{alias}' → {player}")
        else:
            print(f"\n❌ No Austin-related aliases found")
    
    def display_nba_results(self, results):
        """Display real NBA data in a user-friendly format"""
        print(f"\n📊 NBA DATA RESULTS")
        print("=" * 40)
        
        if results['summary']:
            summary = results['summary']
            
            # Show main statistics
            if 'stats' in summary:
                stats = summary['stats']
                print(f"🏀 Games Analyzed: {stats['games_analyzed']}")
                print(f"📈 Average Performance:")
                print(f"   • Points: {stats['avg_points']}")
                print(f"   • Rebounds: {stats['avg_rebounds']}")
                print(f"   • Assists: {stats['avg_assists']}")
                
                # Show comparison with season if available
                if 'season_averages' in summary:
                    season_avg = summary['season_averages']
                    print(f"\n📊 Season Comparison:")
                    print(f"   • Season PPG: {season_avg.get('PTS', 'N/A')}")
                    print(f"   • Season RPG: {season_avg.get('REB', 'N/A')}")
                    print(f"   • Season APG: {season_avg.get('AST', 'N/A')}")
                    
                    # Performance analysis
                    if season_avg.get('PTS'):
                        diff = stats['avg_points'] - season_avg.get('PTS', 0)
                        if abs(diff) > 1:
                            if diff > 0:
                                print(f"   💪 {diff:.1f} points above season average!")
                            else:
                                print(f"   📉 {abs(diff):.1f} points below season average")
        
        # Show sample games
        if results['data'] and 'game_logs' in results['data']:
            game_logs = json.loads(results['data']['game_logs'])
            if game_logs:
                print(f"\n🎮 Recent Games:")
                for i, game in enumerate(game_logs[:5], 1):
                    matchup = game.get('MATCHUP', 'N/A')
                    pts = game.get('PTS', 0)
                    reb = game.get('REB', 0)
                    ast = game.get('AST', 0)
                    date = game.get('GAME_DATE', 'N/A')
                    
                    print(f"   {i}. {date}: {pts}P/{reb}R/{ast}A vs {matchup}")
                
                if len(game_logs) > 5:
                    print(f"   ... and {len(game_logs) - 5} more games")
        
        print("\n" + "=" * 40)
    
    def show_examples(self):
        """Show example queries users can try"""
        examples = {
            "🏀 Player Stats": [
                "LeBron James last 10 games",
                "Stephen Curry this season",
                "Giannis past 5 games",
                "Kevin Durant recent performance"
            ],
            "📍 Location-Based": [
                "Jimmy Butler at home",
                "Damian Lillard on the road",
                "Paul George away games"
            ],
            "🛡️ Advanced Filtering": [
                "James Harden against top 5 defenses",
                "Devin Booker vs elite teams",
                "Trae Young against worst rebounding teams",
                "Luka Doncic last 15 games against top 10 teams"
            ],
            "👤 Player Profiles": [
                "How does Nikola Jokic play?",
                "Joel Embiid playing style",
                "Kawhi Leonard strengths"
            ]
        }
        
        print("\n📚 EXAMPLE QUERIES YOU CAN TRY:")
        print("=" * 50)
        
        for category, queries in examples.items():
            print(f"\n{category}:")
            for query in queries:
                print(f"   📝 {query}")
        
        print(f"\n💡 Just type any of these exactly, or create your own!")
    
    def show_help(self):
        """Show help information"""
        print("\n📖 NBA QUERY HELP")
        print("=" * 30)
        print("You can ask natural language questions about NBA players!")
        print()
        print("🎯 Query Patterns:")
        print("   • '[Player] last [X] games'")
        print("   • '[Player] this season'")
        print("   • '[Player] at home/on the road'")
        print("   • '[Player] against top [X] defenses'")
        print("   • 'How does [Player] play?'")
        print()
        print("📊 What you'll get:")
        print("   • Real NBA statistics and averages")
        print("   • Game-by-game performance data")
        print("   • Comparisons with season averages")
        print("   • Advanced opponent-based filtering")
        print()
        print("💡 Tips:")
        print("   • Use real player names (e.g., 'LeBron James', 'Stephen Curry')")
        print("   • Numbers can be written as digits (10) or words (ten)")
        print("   • Be specific for better results")

def main():
    """Main function"""
    parser = argparse.ArgumentParser(description="NBA Natural Language Query Interactive Tester")
    parser.add_argument("--examples", action="store_true", help="Show example queries and exit")
    
    args = parser.parse_args()
    
    try:
        tester = NBAQueryTester()
        
        if args.examples:
            tester.show_examples()
        else:
            tester.interactive_mode()
            
    except Exception as e:
        print(f"❌ Error initializing NBA Query System: {e}")
        print("💡 Make sure the NBA database is available and try again.")
        sys.exit(1)

if __name__ == "__main__":
    main() 