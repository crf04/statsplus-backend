"""
Custom Prompt Testing for NBA Natural Language Query Parser

This module provides easy ways to add and test custom prompts
with the NBA query parser system.
"""

import unittest
import sys
import os
from unittest.mock import Mock, patch, MagicMock
import pandas as pd

# Add the app directory to the path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.nl_query.parser import BaseQueryParser, QueryComponents

class CustomPromptTestCase:
    """Helper class to define custom test cases"""
    
    def __init__(self, query, expected_player=None, expected_intent=None, 
                 expected_time_period=None, expected_game_count=None,
                 expected_location=None, min_confidence=0.0, description=""):
        self.query = query
        self.expected_player = expected_player
        self.expected_intent = expected_intent
        self.expected_time_period = expected_time_period
        self.expected_game_count = expected_game_count
        self.expected_location = expected_location
        self.min_confidence = min_confidence
        self.description = description or query

class TestCustomPrompts(unittest.TestCase):
    """Test cases for custom prompts - easily extendable"""
    
    def setUp(self):
        """Set up test fixtures with enhanced player/team data"""
        self.mock_engine = MagicMock()
        
        # Extended player list for more comprehensive testing
        player_data = {
            'PLAYER_NAME': [
                "LeBron James", "Stephen Curry", "Giannis Antetokounmpo", 
                "Kevin Durant", "Jayson Tatum", "Donovan Mitchell",
                "Luka Doncic", "Nikola Jokic", "Joel Embiid", "Kawhi Leonard",
                "Jimmy Butler", "Damian Lillard", "Russell Westbrook",
                "Paul George", "Anthony Davis", "Chris Paul", "James Harden",
                "Devin Booker", "Trae Young", "Ja Morant", "Zion Williamson",
                "Kyle Lowry", "Fred VanVleet", "Pascal Siakam", "Scottie Barnes",
                None
            ]
        }
        mock_player_df = pd.DataFrame(player_data)
        
        with patch('pandas.read_sql', return_value=mock_player_df):
            with patch('nba_api.stats.static.teams.get_teams', return_value=[
                {'full_name': 'Los Angeles Lakers', 'abbreviation': 'LAL', 'city': 'Los Angeles', 'nickname': 'Lakers'},
                {'full_name': 'Golden State Warriors', 'abbreviation': 'GSW', 'city': 'Golden State', 'nickname': 'Warriors'},
                {'full_name': 'Boston Celtics', 'abbreviation': 'BOS', 'city': 'Boston', 'nickname': 'Celtics'},
                {'full_name': 'Milwaukee Bucks', 'abbreviation': 'MIL', 'city': 'Milwaukee', 'nickname': 'Bucks'},
                {'full_name': 'Toronto Raptors', 'abbreviation': 'TOR', 'city': 'Toronto', 'nickname': 'Raptors'}
            ]):
                self.parser = BaseQueryParser(self.mock_engine)
    
    def run_custom_test_cases(self, test_cases):
        """Run a list of custom test cases"""
        results = []
        
        for i, test_case in enumerate(test_cases, 1):
            components = self.parser.parse(test_case.query)
            
            # Check expectations
            test_result = {
                'query': test_case.query,
                'description': test_case.description,
                'passed': True,
                'components': components,
                'failures': []
            }
            
            # Validate player name
            if test_case.expected_player:
                if components.player_name != test_case.expected_player:
                    test_result['passed'] = False
                    test_result['failures'].append(
                        f"Expected player '{test_case.expected_player}', got '{components.player_name}'"
                    )
            
            # Validate intent
            if test_case.expected_intent:
                if components.intent != test_case.expected_intent:
                    test_result['passed'] = False
                    test_result['failures'].append(
                        f"Expected intent '{test_case.expected_intent}', got '{components.intent}'"
                    )
            
            # Validate time period
            if test_case.expected_time_period:
                if components.time_period != test_case.expected_time_period:
                    test_result['passed'] = False
                    test_result['failures'].append(
                        f"Expected time period '{test_case.expected_time_period}', got '{components.time_period}'"
                    )
            
            # Validate game count
            if test_case.expected_game_count:
                if components.game_count != test_case.expected_game_count:
                    test_result['passed'] = False
                    test_result['failures'].append(
                        f"Expected game count {test_case.expected_game_count}, got {components.game_count}"
                    )
            
            # Validate location
            if test_case.expected_location:
                if components.location != test_case.expected_location:
                    test_result['passed'] = False
                    test_result['failures'].append(
                        f"Expected location '{test_case.expected_location}', got '{components.location}'"
                    )
            
            # Validate minimum confidence
            if components.confidence < test_case.min_confidence:
                test_result['passed'] = False
                test_result['failures'].append(
                    f"Expected min confidence {test_case.min_confidence}, got {components.confidence:.2f}"
                )
            
            results.append(test_result)
        
        return results

    def test_basic_player_queries(self):
        """Test basic player performance queries"""
        test_cases = [
            CustomPromptTestCase(
                "LeBron James last 10 games",
                expected_player="LeBron James",
                expected_intent="game_logs",
                expected_time_period="recent",
                expected_game_count=10,
                min_confidence=0.6,
                description="Basic player + time + count query"
            ),
            CustomPromptTestCase(
                "Stephen Curry this season",
                expected_player="Stephen Curry",
                expected_intent="game_logs",
                expected_time_period="season",
                min_confidence=0.6,
                description="Player + season query"
            ),
            CustomPromptTestCase(
                "Giannis Antetokounmpo recent stats",
                expected_player="Giannis Antetokounmpo",
                expected_intent="game_logs",
                min_confidence=0.5,
                description="Player + recent modifier"
            )
        ]
        
        results = self.run_custom_test_cases(test_cases)
        self.print_test_results("Basic Player Queries", results)
        
        # Assert at least some tests passed
        passed = sum(1 for r in results if r['passed'])
        self.assertGreater(passed, 0, "At least one basic query should pass")

    def test_location_based_queries(self):
        """Test location-specific queries"""
        test_cases = [
            CustomPromptTestCase(
                "Jimmy Butler at home",
                expected_player="Jimmy Butler",
                expected_location="home",
                expected_intent="game_logs",
                min_confidence=0.5,
                description="Player + home location"
            ),
            CustomPromptTestCase(
                "Damian Lillard on the road",
                expected_player="Damian Lillard",
                expected_location="away",
                expected_intent="game_logs",
                min_confidence=0.5,
                description="Player + away location"
            )
        ]
        
        results = self.run_custom_test_cases(test_cases)
        self.print_test_results("Location-based Queries", results)

    def test_player_profile_queries(self):
        """Test player profile and analysis queries"""
        test_cases = [
            CustomPromptTestCase(
                "How does Nikola Jokic play?",
                expected_player="Nikola Jokic",
                expected_intent="player_profile",
                min_confidence=0.4,
                description="Conversational player profile query"
            ),
            CustomPromptTestCase(
                "Joel Embiid playing style",
                expected_player="Joel Embiid",
                expected_intent="player_profile",
                min_confidence=0.4,
                description="Player + playing style"
            )
        ]
        
        results = self.run_custom_test_cases(test_cases)
        self.print_test_results("Player Profile Queries", results)

    def print_test_results(self, category, results):
        """Print detailed results for a test category"""
        print(f"\n{'='*60}")
        print(f"📊 {category} Results")
        print(f"{'='*60}")
        
        passed = sum(1 for r in results if r['passed'])
        total = len(results)
        
        print(f"✅ Passed: {passed}/{total} ({passed/total*100:.1f}%)")
        
        for result in results:
            status = "✅" if result['passed'] else "❌"
            confidence = result['components'].confidence
            print(f"{status} '{result['query']}' (confidence: {confidence:.2f})")
            
            if not result['passed']:
                for failure in result['failures']:
                    print(f"   • {failure}")

if __name__ == '__main__':
    # Run the tests with extra verbosity
    unittest.main(verbosity=2) 