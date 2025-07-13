#!/usr/bin/env python3
"""
Test suite for integrated date parsing functionality.

This test validates that the NBA date parser works correctly when integrated
into the main BaseQueryParser and handles various date expressions properly.
"""

from typing import List, Dict, Any, Optional
from unittest.mock import Mock, MagicMock
import sys
import os

# Add the project root to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.nl_query.parser import BaseQueryParser, QueryComponents


class MockEngine:
    """Mock database engine for testing."""
    
    def connect(self):
        """Mock database connection."""
        return self
    
    def execute(self, query: str):
        """Mock query execution."""
        class MockResult:
            def fetchall(self):
                return [
                    ("LeBron James",), ("Anthony Davis",), ("Stephen Curry",),
                    ("Luka Doncic",), ("Giannis Antetokounmpo",), ("Jimmy Butler",),
                    ("Paolo Banchero",), ("Victor Wembanyama",), ("Jayson Tatum",),
                    ("Nikola Jokic",), ("Shai Gilgeous-Alexander",), ("Anthony Edwards",)
                ]
        return MockResult()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        pass


class TestDateParsingIntegration:
    """Test suite for date parsing integration."""
    
    def __init__(self, parser: BaseQueryParser):
        """Initialize test suite with parser."""
        self.parser = parser
        self.passed = 0
        self.failed = 0
        
    def assert_condition(self, condition: bool, message: str) -> None:
        """Assert a condition and track results."""
        if condition:
            self.passed += 1
        else:
            self.failed += 1
            print(f"  ❌ FAILED: {message}")
            raise AssertionError(message)
    
    def test_nba_specific_dates(self) -> None:
        """Test NBA-specific date expressions."""
        queries = [
            "LeBron James since the All-Star break",
            "Stephen Curry after trade deadline", 
            "Giannis before Christmas",
            "Anthony Davis since playoffs started",
            "Jimmy Butler after season start",
        ]
        
        print("\n=== NBA-SPECIFIC DATES ===")
        for query in queries:
            components = self.parser.parse(query)
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Date Range: {components.date_range}")
            print(f"  Confidence: {components.confidence:.3f}")
            print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
            
            # Check that date was extracted
            self.assert_condition(components.player_name is not None, f"No player extracted from '{query}'")
            self.assert_condition(components.date_range is not None, f"No date extracted from '{query}'")
            self.assert_condition(components.confidence >= 0.70, f"Low confidence: {components.confidence:.3f}")
            
            print("  ✅ PASSED\n")
    
    def test_relative_dates(self) -> None:
        """Test relative date expressions."""
        queries = [
            "Luka Doncic last 30 days",
            "Paolo Banchero since January",
            "Victor Wembanyama after last month",
            "Shai Gilgeous-Alexander since last 60 days",
            "Jayson Tatum after this month",
        ]
        
        print("\n=== RELATIVE DATES ===")
        for query in queries:
            components = self.parser.parse(query)
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Date Range: {components.date_range}")
            print(f"  Time Period: {components.time_period}")
            print(f"  Game Count: {components.game_count}")
            print(f"  Confidence: {components.confidence:.3f}")
            print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
            
            # Check that date was extracted
            self.assert_condition(components.player_name is not None, f"No player extracted from '{query}'")
            # Note: Some queries might parse as time_period instead of date_range
            has_time_info = components.date_range is not None or components.time_period is not None or components.game_count is not None
            self.assert_condition(has_time_info, f"No time/date info extracted from '{query}'")
            self.assert_condition(components.confidence >= 0.70, f"Low confidence: {components.confidence:.3f}")
            
            print("  ✅ PASSED\n")
    
    def test_explicit_dates(self) -> None:
        """Test explicit date formats."""
        queries = [
            "Anthony Davis since 2024-01-15",
            "Nikola Jokic from January 15, 2024",
            "Jimmy Butler after 01/15/2024",
            "Stephen Curry since December 25, 2023",
            "Giannis from 2024-02-01",
        ]
        
        print("\n=== EXPLICIT DATES ===")
        for query in queries:
            components = self.parser.parse(query)
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Date Range: {components.date_range}")
            print(f"  Confidence: {components.confidence:.3f}")
            print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
            
            # Check that date was extracted
            self.assert_condition(components.player_name is not None, f"No player extracted from '{query}'")
            self.assert_condition(components.date_range is not None, f"No date extracted from '{query}'")
            self.assert_condition(components.confidence >= 0.80, f"Low confidence: {components.confidence:.3f}")
            
            print("  ✅ PASSED\n")
    
    def test_complex_date_queries(self) -> None:
        """Test complex queries with dates and other filters."""
        queries = [
            "LeBron James with Anthony Davis since All-Star break at home",
            "Stephen Curry 30+ minutes after January 1st away games",
            "Luka Doncic without Kyrie Irving since trade deadline last 10 games",
            "Giannis at home since Christmas with 25+ minutes",
            "Paolo Banchero since last month on the road recent games",
        ]
        
        print("\n=== COMPLEX DATE QUERIES ===")
        for query in queries:
            components = self.parser.parse(query)
            breakdown = components.confidence_breakdown
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Date Range: {components.date_range}")
            print(f"  Time Period: {components.time_period}")
            print(f"  Game Count: {components.game_count}")
            print(f"  Location: {components.location}")
            print(f"  Minutes: {components.minutes_filter}")
            print(f"  Players On: {components.players_on}")
            print(f"  Players Off: {components.players_off}")
            print(f"  Confidence: {components.confidence:.3f}")
            print(f"  Coverage: {breakdown.coverage_score:.3f}")
            print(f"  Should use LLM: {breakdown.should_use_llm}")
            
            # Check that multiple components were extracted
            self.assert_condition(components.player_name is not None, f"No player extracted from '{query}'")
            
            # Count components
            component_count = 0
            if components.player_name: component_count += 1
            if components.date_range: component_count += 1
            if components.time_period or components.game_count: component_count += 1
            if components.location: component_count += 1
            if components.minutes_filter: component_count += 1
            if components.players_on: component_count += 1
            if components.players_off: component_count += 1
            
            self.assert_condition(component_count >= 2, f"Too few components extracted: {component_count}")
            self.assert_condition(components.confidence >= 0.60, f"Low confidence: {components.confidence:.3f}")
            
            print("  ✅ PASSED\n")
    
    def test_date_edge_cases(self) -> None:
        """Test edge cases and potential parsing issues."""
        queries = [
            # Ambiguous date references
            "Anthony Davis since last game",
            "Stephen Curry after the injury",
            "Luka Doncic since he came back",
            
            # Multiple date references
            "Jimmy Butler since January after Christmas",
            "Giannis from last month until this week",
            
            # No clear date
            "Paolo Banchero recent performance", 
            "Victor Wembanyama latest stats",
            
            # Conflicting time references
            "Shai last 10 games since January",
            "Jayson Tatum this season after All-Star break",
        ]
        
        print("\n=== DATE EDGE CASES ===")
        for query in queries:
            components = self.parser.parse(query)
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Date Range: {components.date_range}")
            print(f"  Time Period: {components.time_period}")
            print(f"  Game Count: {components.game_count}")
            print(f"  Confidence: {components.confidence:.3f}")
            print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
            
            # For edge cases, just check that we got a player
            self.assert_condition(components.player_name is not None, f"No player extracted from '{query}'")
            # Confidence might be lower for edge cases
            self.assert_condition(components.confidence >= 0.40, f"Very low confidence: {components.confidence:.3f}")
            
            print("  ✅ PASSED\n")
    
    def test_coverage_analysis(self) -> None:
        """Test coverage analysis for date parsing."""
        test_queries = [
            "LeBron James since January 15, 2024",
            "Stephen Curry after All-Star break at home", 
            "Luka Doncic last 30 days 30+ minutes",
        ]
        
        print("\n=== COVERAGE ANALYSIS ===")
        for query in test_queries:
            components = self.parser.parse(query)
            breakdown = components.confidence_breakdown
            
            print(f"Query: '{query}'")
            print(f"  Coverage Score: {breakdown.coverage_score:.3f}")
            print(f"  Components Found: {breakdown.details.get('coverage_components', 0)}")
            print(f"  Uncovered Text: '{breakdown.details.get('uncovered_text', '')}'")
            
            # Coverage should be reasonable with date parsing
            self.assert_condition(breakdown.coverage_score >= 0.60, f"Low coverage: {breakdown.coverage_score:.3f}")
            
            print("  ✅ PASSED\n")
    
    def run_all_tests(self) -> None:
        """Run all tests and report results."""
        try:
            print("Testing integrated date parsing functionality...")
            print("Validating that NBA date parser works with the main parser.")
            print()
            
            self.test_nba_specific_dates()
            self.test_relative_dates()
            self.test_explicit_dates()
            self.test_complex_date_queries()
            self.test_date_edge_cases()
            self.test_coverage_analysis()
            
            print("=" * 70)
            print(f"📊 DATE PARSING INTEGRATION TEST RESULTS:")
            print(f"  ✅ Passed: {self.passed}")
            print(f"  ❌ Failed: {self.failed}")
            print(f"  📈 Success Rate: {self.passed/(self.passed+self.failed)*100:.1f}%")
            print("=" * 70)
            
        except AssertionError as e:
            print("=" * 70)
            print(f"❌ DATE PARSING INTEGRATION TEST FAILED!")
            print(f"  ✅ Passed: {self.passed}")
            print(f"  ❌ Failed: {self.failed}")
            print(f"  Last error: {e}")
            print("=" * 70)
            return False
        
        return True


def run_date_parsing_integration_test() -> None:
    """Run the date parsing integration test."""
    print("=" * 70)
    print("DATE PARSING INTEGRATION TEST")
    print("=" * 70)
    
    try:
        # Create mock parser
        mock_engine = MockEngine()
        parser = BaseQueryParser(mock_engine)
        
        # Create test instance
        test_instance = TestDateParsingIntegration(parser)
        
        # Run all tests
        success = test_instance.run_all_tests()
        
        if success:
            print("\n🎉 DATE PARSING INTEGRATION TEST COMPLETED SUCCESSFULLY!")
            print("The NBA date parser is working well with the main parser.")
        else:
            print("\n💥 DATE PARSING INTEGRATION TEST FAILED!")
            
    except Exception as e:
        print(f"\n💥 ERROR RUNNING TESTS: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    run_date_parsing_integration_test() 