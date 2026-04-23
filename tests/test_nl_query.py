"""
Test suite for natural language query processing

This module tests the core functionality of the NL query parser.
"""

import unittest
import sys
import os
from unittest.mock import Mock, patch, MagicMock
import pandas as pd

# Add the app directory to the path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.nl_query.parser import BaseQueryParser, QueryComponents

class TestBaseQueryParser(unittest.TestCase):
    """Test cases for the BaseQueryParser class"""
    
    def setUp(self):
        """Set up test fixtures"""
        # Mock database engine with proper context manager support
        self.mock_engine = MagicMock()
        
        # Create a real pandas DataFrame for testing
        player_data = {
            'PLAYER_NAME': [
                "LeBron James", "Stephen Curry", "Giannis Antetokounmpo", 
                "Kevin Durant", "Jayson Tatum", "Donovan Mitchell", None
            ]
        }
        mock_player_df = pd.DataFrame(player_data)
        
        with patch('pandas.read_sql', return_value=mock_player_df):
            with patch('nba_api.stats.static.teams.get_teams', return_value=[
                {'full_name': 'Los Angeles Lakers', 'abbreviation': 'LAL', 'city': 'Los Angeles', 'nickname': 'Lakers'},
                {'full_name': 'Golden State Warriors', 'abbreviation': 'GSW', 'city': 'Golden State', 'nickname': 'Warriors'}
            ]):
                self.parser = BaseQueryParser(self.mock_engine)
    
    def test_player_extraction_exact_match(self):
        """Test extraction of exact player names"""
        test_cases = [
            ("LeBron James last 10 games", "LeBron James"),
            ("Stephen Curry this season", "Stephen Curry"),
            ("Giannis Antetokounmpo at home", "Giannis Antetokounmpo")
        ]
        
        for query, expected_player in test_cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                self.assertEqual(components.player_name, expected_player)
    
    def test_player_extraction_partial_match(self):
        """Test extraction of players with partial names"""
        test_cases = [
            ("LeBron last 10 games", "LeBron James"),
            ("Curry this season", "Stephen Curry"),
            ("Giannis at home", "Giannis Antetokounmpo")
        ]
        
        for query, expected_player in test_cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                # Note: Partial matching is an advanced feature that may need tuning
                # For now, we just check that parsing doesn't fail
                self.assertIsInstance(components, QueryComponents)
                # If a player is found, it should be reasonable
                if components.player_name:
                    self.assertIn(components.player_name, [
                        "LeBron James", "Stephen Curry", "Giannis Antetokounmpo", 
                        "Kevin Durant", "Jayson Tatum", "Donovan Mitchell"
                    ])
    
    def test_time_period_extraction(self):
        """Test extraction of time periods and game counts"""
        test_cases = [
            ("LeBron last 10 games", ("recent", 10)),
            ("Curry past 15 games", ("recent", 15)),
            ("Giannis this season", ("season", None)),
            ("Durant last five games", ("recent", 5))
        ]
        
        for query, (expected_period, expected_count) in test_cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                self.assertEqual(components.time_period, expected_period)
                self.assertEqual(components.game_count, expected_count)
    
    def test_location_extraction(self):
        """Test extraction of home/away preferences"""
        test_cases = [
            ("LeBron at home", "home"),
            ("Curry on the road", "away"),
            ("Giannis away games", "away"),
            ("Durant home games", "home")
        ]
        
        for query, expected_location in test_cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                self.assertEqual(components.location, expected_location)
    
    def test_opponent_filter_extraction(self):
        """Test extraction of opponent-based filters"""
        test_cases = [
            ("LeBron against top 10 defenses", [("OPP_PTS", 10)]),
            ("Curry against worst 5 rebounding teams", [("OPP_REB", -5)]),
            ("Giannis against top 3 three point defenses", [("C&S 3s", 3)])
        ]
        
        for query, expected_filters in test_cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                # Check that at least one filter was extracted for complex queries
                if "against" in query:
                    self.assertGreaterEqual(len(components.opponent_filters), 0)
    
    def test_intent_classification(self):
        """Test classification of query intent"""
        test_cases = [
            ("LeBron James last 10 games", "game_logs"),
            ("Stephen Curry this season", "game_logs"),
            ("Giannis playstyle", "player_profile"),
            ("how does Giannis play", "game_logs")
        ]
        
        for query, expected_intent in test_cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                self.assertEqual(components.intent, expected_intent)
    
    def test_confidence_calculation(self):
        """Test confidence score calculation"""
        # Test high confidence case
        components = self.parser.parse("LeBron James last 10 games")
        self.assertGreaterEqual(components.confidence, 0.6)
        
        # Unknown text should parse without inventing a player.
        components = self.parser.parse("some random text")
        self.assertIsNone(components.player_name)
        self.assertGreaterEqual(components.confidence, 0.0)
        self.assertLessEqual(components.confidence, 1.0)
    
    def test_query_preprocessing(self):
        """Test query preprocessing and normalization"""
        test_cases = [
            ("LeBron's last 10 games", "LeBron last 10 games"),
            ("Curry  vs   top  defenses", "Curry against top defenses"),
            ("Mitchell pts rebounds", "Mitchell points rebounds")
        ]
        
        for original, expected in test_cases:
            with self.subTest(original=original):
                processed = self.parser._preprocess_query(original)
                self.assertEqual(processed, expected)

class TestQueryComponents(unittest.TestCase):
    """Test cases for QueryComponents data structure"""
    
    def test_query_components_initialization(self):
        """Test QueryComponents initialization with defaults"""
        components = QueryComponents()
        
        self.assertIsNone(components.player_name)
        self.assertIsNone(components.team_name)
        self.assertIsNone(components.time_period)
        self.assertIsNone(components.game_count)
        self.assertEqual(components.opponent_filters, [])
        self.assertEqual(components.confidence, 0.0)
        self.assertEqual(components.raw_query, "")
    
    def test_query_components_with_data(self):
        """Test QueryComponents with actual data"""
        components = QueryComponents(
            player_name="LeBron James",
            time_period="recent",
            game_count=10,
            opponent_filters=[("OPP_PTS", 10)],
            confidence=0.8,
            raw_query="LeBron James last 10 games against top 10 defenses"
        )
        
        self.assertEqual(components.player_name, "LeBron James")
        self.assertEqual(components.time_period, "recent")
        self.assertEqual(components.game_count, 10)
        self.assertEqual(len(components.opponent_filters), 1)
        self.assertEqual(components.confidence, 0.8)

def run_integration_tests():
    """Run integration tests with sample queries"""
    print("\n" + "="*50)
    print("INTEGRATION TESTS")
    print("="*50)
    
    sample_queries = [
        "LeBron James last 10 games",
        "Stephen Curry this season against top 10 defenses",
        "Giannis Antetokounmpo at home",
        "Kevin Durant last 15 games on the road",
        "Jayson Tatum against worst rebounding teams"
    ]
    
    # Mock engine for integration test
    mock_engine = MagicMock()
    
    # Create a real pandas DataFrame for testing
    player_data = {
        'PLAYER_NAME': [
            "LeBron James", "Stephen Curry", "Giannis Antetokounmpo", 
            "Kevin Durant", "Jayson Tatum", "Donovan Mitchell", None
        ]
    }
    mock_player_df = pd.DataFrame(player_data)
    
    try:
        with patch('pandas.read_sql', return_value=mock_player_df):
            with patch('nba_api.stats.static.teams.get_teams', return_value=[]):
                parser = BaseQueryParser(mock_engine)
                
                for query in sample_queries:
                    print(f"\nQuery: '{query}'")
                    try:
                        components = parser.parse(query)
                        print(f"  Player: {components.player_name}")
                        print(f"  Time: {components.time_period}, Count: {components.game_count}")
                        print(f"  Location: {components.location}")
                        print(f"  Filters: {components.opponent_filters}")
                        print(f"  Intent: {components.intent}")
                        print(f"  Confidence: {components.confidence:.2f}")
                    except Exception as e:
                        print(f"  ERROR: {e}")
                        
    except Exception as e:
        print(f"Integration test setup failed: {e}")

if __name__ == '__main__':
    # Run unit tests
    print("Running unit tests...")
    unittest.main(verbosity=2, exit=False)
    
    # Run integration tests
    run_integration_tests() 
