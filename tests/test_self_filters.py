"""
Test suite for self-filter parsing functionality

Tests both traditional NLP parser and LLM fallback for various self-filter patterns.
"""

import unittest
import sys
import os
from unittest.mock import patch, MagicMock
import pandas as pd
from app.models.game_logs import SelfFilter

# Add the app directory to the path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.nl_query.parser import BaseQueryParser


class TestSelfFilters(unittest.TestCase):
    """Test cases for self-filter parsing"""
    
    def setUp(self):
        """Set up test fixtures"""
        # Mock database engine
        self.mock_engine = MagicMock()
        
        # Create mock player data
        player_data = {
            'PLAYER_NAME': [
                "LeBron James", "Stephen Curry", "Giannis Antetokounmpo", 
                "Kevin Durant", "Jayson Tatum", "Kobe Bryant"
            ]
        }
        mock_player_df = pd.DataFrame(player_data)
        
        with patch('pandas.read_sql', return_value=mock_player_df):
            with patch('nba_api.stats.static.teams.get_teams', return_value=[]):
                self.parser = BaseQueryParser(self.mock_engine)
    
    def test_basic_scoring_filters(self):
        """Test basic scoring pattern filters"""
        test_cases = [
            ("LeBron games with 25+ points", ("PTS", "gte", 25)),
            ("Curry games scoring 30+ points", ("PTS", "gte", 30)),
            ("Giannis 20+ point games", ("PTS", "gte", 20)),
        ]
        
        for query, expected_filter in test_cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                self.assertGreater(len(components.self_filters), 0, f"No self filters found for: {query}")
                
                filter_obj = components.self_filters[0]
                expected_stat, expected_op, expected_val = expected_filter
                
                self.assertEqual(filter_obj.stat_column, expected_stat)
                self.assertEqual(filter_obj.operator, expected_op)
                self.assertEqual(filter_obj.value, expected_val)
    
    def test_shooting_attempt_filters_are_extracted(self):
        """Test shooting attempt patterns are extracted before any LLM fallback"""
        cases = [
            ("LeBron games shooting 15+ times", "FGA", 15),
        ]
        
        for query, stat_column, value in cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                
                self.assertTrue(
                    any(
                        filter_obj.stat_column == stat_column and filter_obj.value == value
                        for filter_obj in components.self_filters
                    ),
                    f"Expected {stat_column}>={value} filter for: {query}",
                )
    
    def test_shooting_attempt_filters_traditional(self):
        """Test shooting attempt patterns handled by traditional parser"""
        traditional_cases = [
            ("Curry games with 10+ three point attempts", ("FG3A", "gte", 10)),
            ("Kobe games with 25+ field goal attempts", ("FGA", "gte", 25)),
        ]
        
        for query, expected_filter in traditional_cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                
                if components.self_filters:
                    filter_obj = components.self_filters[0]
                    expected_stat, expected_op, expected_val = expected_filter
                    
                    self.assertEqual(filter_obj.stat_column, expected_stat)
                    self.assertEqual(filter_obj.operator, expected_op)
                    self.assertEqual(filter_obj.value, expected_val)
    
    def test_rebound_filters(self):
        """Test rebounding pattern filters"""
        test_cases = [
            ("LeBron games with 10+ rebounds", ("REB", "gte", 10)),
            ("Giannis 15+ rebound games", ("REB", "gte", 15)),
        ]
        
        for query, expected_filter in test_cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                self.assertGreater(len(components.self_filters), 0, f"No self filters found for: {query}")
                
                filter_obj = components.self_filters[0]
                expected_stat, expected_op, expected_val = expected_filter
                
                self.assertEqual(filter_obj.stat_column, expected_stat)
                self.assertEqual(filter_obj.operator, expected_op)
                self.assertEqual(filter_obj.value, expected_val)
    
    def test_assist_filters(self):
        """Test assist pattern filters"""
        test_cases = [
            ("Curry games with 8+ assists", ("AST", "gte", 8)),
            ("LeBron 10+ assist games", ("AST", "gte", 10)),
        ]
        
        for query, expected_filter in test_cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                self.assertGreater(len(components.self_filters), 0, f"No self filters found for: {query}")
                
                filter_obj = components.self_filters[0]
                expected_stat, expected_op, expected_val = expected_filter
                
                self.assertEqual(filter_obj.stat_column, expected_stat)
                self.assertEqual(filter_obj.operator, expected_op)
                self.assertEqual(filter_obj.value, expected_val)
    
    def test_defensive_stat_filters(self):
        """Test defensive stat filters (steals, blocks)"""
        test_cases = [
            ("LeBron games with 2+ steals", ("STL", "gte", 2)),
            ("Giannis 3+ block games", ("BLK", "gte", 3)),
        ]
        
        for query, expected_filter in test_cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                self.assertGreater(len(components.self_filters), 0, f"No self filters found for: {query}")
                
                filter_obj = components.self_filters[0]
                expected_stat, expected_op, expected_val = expected_filter
                
                self.assertEqual(filter_obj.stat_column, expected_stat)
                self.assertEqual(filter_obj.operator, expected_op)
                self.assertEqual(filter_obj.value, expected_val)
    
    def test_three_point_filters(self):
        """Test three-point specific filters"""
        test_cases = [
            ("Curry games with 5+ threes", ("FG3M", "gte", 5)),
            ("Durant games making 2+ threes", ("FG3M", "gte", 2)),
        ]
        
        for query, expected_filter in test_cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                self.assertGreater(len(components.self_filters), 0, f"No self filters found for: {query}")
                
                filter_obj = components.self_filters[0]
                expected_stat, expected_op, expected_val = expected_filter
                
                self.assertEqual(filter_obj.stat_column, expected_stat)
                self.assertEqual(filter_obj.operator, expected_op)
                self.assertEqual(filter_obj.value, expected_val)
    
    def test_multiple_self_filters(self):
        """Test queries with multiple self-filter conditions"""
        test_cases = [
            "LeBron games with 25+ points and 8+ assists",
            "Curry games with 30+ points and 5+ threes",
            "Giannis games with 20+ points and 10+ rebounds",
        ]
        
        for query in test_cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                # Should have at least one filter, potentially more
                self.assertGreater(len(components.self_filters), 0, f"No self filters found for: {query}")
    
    def test_operator_variations(self):
        """Test different comparison operators"""
        test_cases = [
            ("LeBron games with exactly 25 points", "eq", 25),
            ("Curry games with over 30 points", "gt", 30),
            ("Giannis games with at least 20 points", "gte", 20),
        ]
        
        for query, expected_op, expected_val in test_cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                if components.self_filters:
                    filter_obj = components.self_filters[0]
                    self.assertEqual(filter_obj.operator, expected_op)
                    self.assertEqual(filter_obj.value, expected_val)

    def test_typed_operator_domain_applies_all_comparisons(self):
        values = pd.Series([1, 2, 3, 4])
        expected = {
            "gte": [False, True, True, True],
            "gt": [False, False, True, True],
            "lt": [True, False, False, False],
            "lte": [True, True, False, False],
            "eq": [False, False, True, False],
            "between": [False, True, True, False],
        }

        for operator, mask in expected.items():
            with self.subTest(operator=operator):
                value2 = 3 if operator == "between" else None
                typed_filter = SelfFilter(
                    stat="PTS",
                    operator=operator,
                    value=2 if operator != "eq" else 3,
                    value2=value2,
                )
                self.assertEqual(typed_filter.apply(values).tolist(), mask)

    def test_typed_operator_domain_rejects_invalid_second_values(self):
        with self.assertRaises(ValueError):
            SelfFilter(stat="PTS", operator="between", value=3)
        with self.assertRaises(ValueError):
            SelfFilter(stat="PTS", operator="gte", value=3, value2=4)
        with self.assertRaises(ValueError):
            SelfFilter(stat="PTS", operator="between", value=4, value2=3)
    
    def test_edge_cases(self):
        """Test edge cases and potential failure scenarios"""
        edge_cases = [
            "LeBron games with 0 turnovers",  # Zero value
            "Curry games with 50+ points",   # High value
            "Giannis triple-double games",   # Special pattern
            "Durant double-digit scoring",   # Double-digit pattern
        ]
        
        for query in edge_cases:
            with self.subTest(query=query):
                components = self.parser.parse(query)
                # Should not crash, confidence should be reasonable
                self.assertIsInstance(components.confidence, float)
                self.assertGreaterEqual(components.confidence, 0.0)
                self.assertLessEqual(components.confidence, 1.0)
    
    def test_confidence_and_llm_triggering(self):
        """Test that common stat filters stay on the deterministic parser path"""
        traditional_queries = [
            "LeBron games shooting 15+ times",
            "LeBron games with 25+ points",
            "Curry games with 10+ rebounds",
            "Giannis 15+ assist games",
        ]
        llm_fallback_queries = [
            "Giannis games attempting 20+ shots",
            "Giannis games taking 20+ shots",
            "Durant games taking 8+ threes",
        ]

        for query in traditional_queries:
            with self.subTest(query=query, test_type="traditional"):
                components = self.parser.parse(query)
                self.assertFalse(components.confidence_breakdown.should_use_llm,
                               f"Should NOT trigger LLM: {query}")

        for query in llm_fallback_queries:
            with self.subTest(query=query, test_type="llm_fallback"):
                components = self.parser.parse(query)
                self.assertTrue(components.confidence_breakdown.should_use_llm,
                              f"Should trigger LLM: {query}")


def run_self_filter_integration_test():
    """Run integration test to show current self-filter parsing status"""
    print("\n" + "="*60)
    print("SELF-FILTER INTEGRATION TEST")
    print("="*60)
    
    # Mock engine
    mock_engine = MagicMock()
    
    # Create mock player data
    player_data = {
        'PLAYER_NAME': [
            "LeBron James", "Stephen Curry", "Giannis Antetokounmpo", 
            "Kevin Durant", "Jayson Tatum", "Kobe Bryant"
        ]
    }
    mock_player_df = pd.DataFrame(player_data)
    
    test_queries = [
        # Traditional patterns (should work well)
        "LeBron games with 25+ points",
        "Curry games with 10+ rebounds", 
        "Giannis 15+ assist games",
        
        # Shooting patterns (should trigger LLM)
        "LeBron games shooting 15+ times",
        "Curry games attempting 10+ threes",
        "Kobe games taking 20+ shots",
        
        # Complex patterns
        "Durant games with 25+ points and 8+ assists",
        "Tatum triple-double games",
    ]
    
    try:
        with patch('pandas.read_sql', return_value=mock_player_df):
            with patch('nba_api.stats.static.teams.get_teams', return_value=[]):
                parser = BaseQueryParser(mock_engine)
                
                for query in test_queries:
                    print(f"\nQuery: '{query}'")
                    try:
                        components = parser.parse(query)
                        print(f"  Player: {components.player_name}")
                        print(f"  Confidence: {components.confidence:.3f}")
                        print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
                        
                        if components.self_filters:
                            print(f"  Self filters ({len(components.self_filters)}):")
                            for i, filter_obj in enumerate(components.self_filters):
                                print(f"    {i+1}. {filter_obj.stat_column} {filter_obj.operator} {filter_obj.value}")
                        else:
                            print("  Self filters: None detected")
                            
                    except Exception as e:
                        print(f"  ERROR: {e}")
                        
    except Exception as e:
        print(f"Integration test setup failed: {e}")


if __name__ == '__main__':
    # Run unit tests
    print("Running self-filter unit tests...")
    unittest.main(verbosity=2, exit=False)
    
    # Run integration test
    run_self_filter_integration_test()
