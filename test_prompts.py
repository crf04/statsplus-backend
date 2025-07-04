#!/usr/bin/env python3
"""
Interactive NBA Natural Language Query Tester

This tool allows you to test different natural language prompts
and see how they're parsed by the system.

Usage:
    python test_prompts.py                    # Interactive mode
    python test_prompts.py --batch            # Run predefined test cases
    python test_prompts.py --file queries.txt # Test from file
"""

import sys
import os
import argparse
from unittest.mock import patch, MagicMock
import pandas as pd
from datetime import datetime

# Add the app directory to the path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

from app.services.nl_query.parser import BaseQueryParser, QueryComponents
from app.services.nl_query.parameter_mapper import ParameterMapper

def create_test_parser():
    """Create a parser instance with mock data for testing"""
    # Mock database engine
    mock_engine = MagicMock()
    
    # Create realistic test data
    player_data = {
        'PLAYER_NAME': [
            "LeBron James", "Stephen Curry", "Giannis Antetokounmpo", 
            "Kevin Durant", "Jayson Tatum", "Donovan Mitchell",
            "Luka Doncic", "Nikola Jokic", "Joel Embiid", "Kawhi Leonard",
            "Jimmy Butler", "Damian Lillard", "Russell Westbrook",
            "Paul George", "Anthony Davis", "Chris Paul", "James Harden",
            "Devin Booker", "Trae Young", "Ja Morant", "Zion Williamson",
            None  # Test null handling
        ]
    }
    mock_player_df = pd.DataFrame(player_data)
    
    team_data = [
        {'full_name': 'Los Angeles Lakers', 'abbreviation': 'LAL', 'city': 'Los Angeles', 'nickname': 'Lakers'},
        {'full_name': 'Golden State Warriors', 'abbreviation': 'GSW', 'city': 'Golden State', 'nickname': 'Warriors'},
        {'full_name': 'Boston Celtics', 'abbreviation': 'BOS', 'city': 'Boston', 'nickname': 'Celtics'},
        {'full_name': 'Milwaukee Bucks', 'abbreviation': 'MIL', 'city': 'Milwaukee', 'nickname': 'Bucks'},
        {'full_name': 'Phoenix Suns', 'abbreviation': 'PHX', 'city': 'Phoenix', 'nickname': 'Suns'},
        {'full_name': 'Brooklyn Nets', 'abbreviation': 'BKN', 'city': 'Brooklyn', 'nickname': 'Nets'}
    ]
    
    with patch('pandas.read_sql', return_value=mock_player_df):
        with patch('nba_api.stats.static.teams.get_teams', return_value=team_data):
            parser = BaseQueryParser(mock_engine)
            mapper = ParameterMapper()
            return parser, mapper

def display_results(query, components, api_info=None):
    """Display parsing results and API calls in a formatted way"""
    print("=" * 80)
    print(f"🔍 Query: '{query}'")
    print("=" * 80)
    
    # STEP 1: PARSING RESULTS
    print("📋 STEP 1: NATURAL LANGUAGE PARSING")
    print("-" * 50)
    
    # Core entities
    if components.player_name:
        print(f"👤 Player: {components.player_name}")
    if components.team_name:
        print(f"🏀 Team: {components.team_name}")
    
    # Time filters
    if components.time_period:
        print(f"⏰ Time Period: {components.time_period}")
    if components.game_count:
        print(f"🎯 Game Count: {components.game_count}")
    if components.date_range:
        print(f"📅 Date Range: {components.date_range}")
    
    # Location and filters
    if components.location:
        print(f"🏠 Location: {components.location}")
    if components.opponent_filters:
        print(f"🛡️ Opponent Filters: {components.opponent_filters}")
    
    # Intent and confidence
    print(f"🎯 Intent: {components.intent}")
    print(f"📊 Confidence: {components.confidence:.2f}")
    
    # Confidence interpretation
    if components.confidence >= 0.8:
        conf_level = "🟢 High (Excellent)"
    elif components.confidence >= 0.6:
        conf_level = "🟡 Medium (Good)"
    elif components.confidence >= 0.4:
        conf_level = "🟠 Low (Needs improvement)"
    else:
        conf_level = "🔴 Very Low (Poor understanding)"
    
    print(f"💯 Confidence Level: {conf_level}")
    
    # STEP 2: API MAPPING (if provided)
    if api_info:
        print(f"\n📡 STEP 2: API MAPPING")
        print("-" * 50)
        print(f"🎯 Target Endpoint: {api_info['endpoint']}")
        print(f"📝 Description: {api_info['description']}")
        
        # Show mapped parameters
        print(f"\n🔧 Mapped Parameters:")
        for key, value in api_info['parameters'].items():
            if key == "opponent_filters" and isinstance(value, list):
                print(f"   • {key}: {len(value)} filters")
                for filter_item in value:
                    print(f"     - {filter_item}")
            else:
                print(f"   • {key}: {value}")
        
        # STEP 3: ACTUAL API CALLS
        print(f"\n🚀 STEP 3: GENERATED API CALLS")
        print("-" * 50)
        
        for i, api_call in enumerate(api_info['api_calls'], 1):
            print(f"\n📞 API Call #{i}: {api_call['purpose']}")
            print(f"   Service: {api_call['service']}")
            print(f"   Method: {api_call['method']}")
            print(f"   Parameters:")
            for param_key, param_value in api_call['parameters'].items():
                print(f"     • {param_key}: {param_value}")
        
        # STEP 4: EXPECTED RESULTS
        print(f"\n📊 STEP 4: EXPECTED RESULTS")
        print("-" * 50)
        estimates = api_info['estimated_results']
        print(f"📈 Data Type: {estimates['data_type']}")
        print(f"📋 Expected Records: {estimates['expected_records']}")
        print(f"⏱️ Processing Time: {estimates['processing_time']}")
        
        # Generate example API execution code
        print(f"\n💻 STEP 5: EXAMPLE EXECUTION CODE")
        print("-" * 50)
        print("```python")
        for i, api_call in enumerate(api_info['api_calls'], 1):
            service_parts = api_call['service'].split('.')
            class_name = service_parts[-1]
            
            print(f"# API Call {i}: {api_call['purpose']}")
            print(f"from {api_call['service']} import {class_name}")
            print(f"{class_name.lower()} = {class_name}(")
            
            for param_key, param_value in api_call['parameters'].items():
                if isinstance(param_value, str):
                    print(f"    {param_key}='{param_value}',")
                else:
                    print(f"    {param_key}={param_value},")
            
            print(")")
            print(f"data_{i} = {class_name.lower()}.get_data_frames()[0]")
            print()
        
        print("```")
    
    print("\n" + "=" * 80)
    print()

def interactive_mode():
    """Interactive testing mode with API call display"""
    print("🏀 NBA Natural Language Query Interactive Tester")
    print("=" * 50)
    print("Enter natural language queries to test the parser.")
    print("See the complete pipeline: Query → Parsing → API Calls")
    print("Type 'quit', 'exit', or 'q' to stop.")
    print("Type 'examples' to see sample queries.")
    print("=" * 50)
    
    parser, mapper = create_test_parser()
    
    while True:
        try:
            query = input("\n💬 Enter your query: ").strip()
            
            if query.lower() in ['quit', 'exit', 'q']:
                print("👋 Goodbye!")
                break
            
            if query.lower() == 'examples':
                show_examples()
                continue
            
            if not query:
                print("❌ Please enter a query!")
                continue
            
            # Parse the query
            components = parser.parse(query)
            
            # Map to API parameters
            api_info = mapper.map_to_api_params(components)
            
            # Display complete results
            display_results(query, components, api_info)
            
        except KeyboardInterrupt:
            print("\n👋 Goodbye!")
            break
        except Exception as e:
            print(f"❌ Error processing query: {e}")

def batch_test():
    """Run a batch of predefined test cases with API call display"""
    print("🚀 Running Batch Test Cases with API Call Display")
    print("=" * 60)
    
    test_cases = [
        # Basic player queries
        "LeBron James last 10 games",
        "Stephen Curry this season", 
        "Giannis stats this year",
        
        # Complex time queries
        "Kevin Durant past 15 games",
        "Jayson Tatum last five games",
        "Luka Doncic recent performance",
        
        # Location-based queries
        "Jimmy Butler at home",
        "Damian Lillard on the road",
        "Paul George away games",
        
        # Opponent-based queries
        "James Harden against top 10 defenses",
        "Devin Booker against worst rebounding teams",
        "Trae Young vs elite teams",
        
        # Player profile queries
        "How does Nikola Jokic play?",
        "Joel Embiid playing style",
        "Kawhi Leonard strengths"
    ]
    
    parser, mapper = create_test_parser()
    
    for i, query in enumerate(test_cases, 1):
        print(f"\n📋 Test Case {i}/{len(test_cases)}")
        
        # Parse the query
        components = parser.parse(query)
        
        # Map to API parameters
        api_info = mapper.map_to_api_params(components)
        
        # Display complete results
        display_results(query, components, api_info)
        
        # Brief pause for readability
        if i < len(test_cases):
            input("Press Enter to continue...")

def test_from_file(filename):
    """Test queries from a file with API call display"""
    print(f"📄 Testing queries from: {filename}")
    print("=" * 60)
    
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            queries = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        
        if not queries:
            print("❌ No valid queries found in file!")
            return
        
        parser, mapper = create_test_parser()
        results = []
        
        for i, query in enumerate(queries, 1):
            print(f"\n📋 Query {i}/{len(queries)}")
            
            # Parse the query
            components = parser.parse(query)
            
            # Map to API parameters
            api_info = mapper.map_to_api_params(components)
            
            # Display complete results
            display_results(query, components, api_info)
            
            results.append({
                'query': query,
                'confidence': components.confidence,
                'player': components.player_name,
                'intent': components.intent,
                'api_calls': len(api_info['api_calls']),
                'endpoint': api_info['endpoint']
            })
            
            # Brief pause for readability
            if i < len(queries):
                input("Press Enter to continue...")
        
        # Summary statistics
        print("\n📊 SUMMARY STATISTICS")
        print("=" * 40)
        confidences = [r['confidence'] for r in results]
        avg_confidence = sum(confidences) / len(confidences)
        high_conf = len([c for c in confidences if c >= 0.6])
        
        print(f"Total Queries: {len(results)}")
        print(f"Average Confidence: {avg_confidence:.2f}")
        print(f"High Confidence (≥0.6): {high_conf}/{len(results)} ({high_conf/len(results)*100:.1f}%)")
        
        # API call statistics
        total_api_calls = sum(r['api_calls'] for r in results)
        avg_api_calls = total_api_calls / len(results)
        print(f"Total API Calls Generated: {total_api_calls}")
        print(f"Average API Calls per Query: {avg_api_calls:.1f}")
        
        # Endpoint distribution
        endpoints = {}
        for result in results:
            endpoint = result['endpoint']
            endpoints[endpoint] = endpoints.get(endpoint, 0) + 1
        
        print(f"\nEndpoint Distribution:")
        for endpoint, count in endpoints.items():
            percentage = (count / len(results)) * 100
            print(f"  • {endpoint}: {count} queries ({percentage:.1f}%)")
        
        # Top performers
        best_results = sorted(results, key=lambda x: x['confidence'], reverse=True)[:5]
        print(f"\n🏆 Top 5 Best Parsed Queries:")
        for i, result in enumerate(best_results, 1):
            print(f"{i}. '{result['query']}' (confidence: {result['confidence']:.2f}, endpoint: {result['endpoint']})")
        
    except FileNotFoundError:
        print(f"❌ File not found: {filename}")
    except Exception as e:
        print(f"❌ Error reading file: {e}")

def show_examples():
    """Show example queries"""
    examples = {
        "Player Performance": [
            "LeBron James last 10 games",
            "Stephen Curry this season",
            "Giannis shooting percentage"
        ],
        "Time-based Queries": [
            "Kevin Durant past 15 games",
            "Jayson Tatum recent performance",
            "Luka Doncic this month"
        ],
        "Location Queries": [
            "Jimmy Butler at home",
            "Damian Lillard on the road",
            "Paul George away games"
        ],
        "Opponent Analysis": [
            "James Harden against top 10 defenses",
            "Devin Booker vs elite teams",
            "Trae Young against worst rebounding teams"
        ],
        "Player Profiles": [
            "How does Nikola Jokic play?",
            "Joel Embiid playing style",
            "Kawhi Leonard strengths and weaknesses"
        ]
    }
    
    print("\n📚 EXAMPLE QUERIES")
    print("=" * 30)
    
    for category, queries in examples.items():
        print(f"\n🔸 {category}:")
        for query in queries:
            print(f"   • {query}")

def main():
    parser = argparse.ArgumentParser(description="Test NBA NL Query Parser with different prompts")
    parser.add_argument("--batch", action="store_true", help="Run batch test cases")
    parser.add_argument("--file", type=str, help="Test queries from file")
    
    args = parser.parse_args()
    
    try:
        if args.batch:
            batch_test()
        elif args.file:
            test_from_file(args.file)
        else:
            interactive_mode()
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main() 