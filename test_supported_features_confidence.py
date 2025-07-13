#!/usr/bin/env python3
"""
Test suite for confidence scoring on supported features.

This test validates that well-formed queries using only supported features
receive high confidence scores, ensuring the parser works effectively on
its intended use cases.
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
                    ("LeBron James",), ("Anthony Davis",), ("Anthony Edwards",), 
                    ("Stephen Curry",), ("Austin Reaves",), ("Luka Doncic",), 
                    ("Kyrie Irving",), ("Jayson Tatum",), ("Jaylen Brown",),
                    ("Giannis Antetokounmpo",), ("Damian Lillard",), ("Kevin Durant",),
                    ("James Harden",), ("Russell Westbrook",), ("Nikola Jokic",),
                    ("Jimmy Butler",), ("Bam Adebayo",), ("Tyler Herro",),
                    ("Shai Gilgeous-Alexander",), ("Josh Giddey",), ("Chet Holmgren",),
                    ("Paolo Banchero",), ("Franz Wagner",), ("Jalen Suggs",),
                    ("Victor Wembanyama",), ("Devin Vassell",), ("Keldon Johnson",),
                    ("Klay Thompson",), ("Jamal Murray",), ("Draymond Green",),
                    ("Andrew Wiggins",), ("Marcus Smart",), ("Robert Williams",),
                    ("Tim Hardaway Jr.",), ("Dwight Powell",), ("Deandre Ayton",),
                    ("Devin Booker",), ("Chris Paul",), ("Kawhi Leonard",),
                    ("Paul George",), ("Russell Westbrook",), ("Zion Williamson",),
                    ("Brandon Ingram",), ("CJ McCollum",), ("Jonas Valanciunas",)
                ]
        return MockResult()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        pass


class TestSupportedFeaturesConfidence:
    """Test suite for confidence scoring on supported features."""
    
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
    
    def test_single_player_queries(self) -> None:
        """Test confidence for single player queries (well-supported feature)."""
        queries = [
            "LeBron James",
            "Stephen Curry stats",
            "Luka Doncic performance",
            "Giannis Antetokounmpo game logs",
            "Anthony Davis recent games"
        ]
        
        print("\n=== SINGLE PLAYER QUERIES ===")
        for query in queries:
            components = self.parser.parse(query)
            confidence = components.confidence
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Confidence: {confidence:.3f}")
            print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
            
            # Single player queries should have high confidence
            self.assert_condition(confidence >= 0.70, f"Query '{query}' got low confidence: {confidence:.3f}")
            self.assert_condition(components.player_name is not None, f"No player extracted from '{query}'")
            print("  ✅ PASSED\n")
    
    def test_time_period_queries(self) -> None:
        """Test confidence for time period queries (well-supported feature)."""
        queries = [
            "LeBron James last 10 games",
            "Stephen Curry last 5 games",
            "Luka Doncic this season",
            "Giannis this month",
            "Anthony Davis recent 15 games"
        ]
        
        print("\n=== TIME PERIOD QUERIES ===")
        for query in queries:
            components = self.parser.parse(query)
            confidence = components.confidence
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Time Period: {components.time_period}")
            print(f"  Game Count: {components.game_count}")
            print(f"  Confidence: {confidence:.3f}")
            print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
            
            # Time period queries should have high confidence
            self.assert_condition(confidence >= 0.75, f"Query '{query}' got low confidence: {confidence:.3f}")
            self.assert_condition(components.player_name is not None, f"No player extracted from '{query}'")
            self.assert_condition(components.time_period is not None or components.game_count is not None, f"No time info from '{query}'")
            print("  ✅ PASSED\n")
    
    def test_location_queries(self) -> None:
        """Test confidence for location queries (well-supported feature)."""
        queries = [
            "LeBron James at home",
            "Stephen Curry away games",
            "Luka Doncic home games this season",
            "Giannis on the road last 10 games",
            "Anthony Davis home court performance"
        ]
        
        print("\n=== LOCATION QUERIES ===")
        for query in queries:
            components = self.parser.parse(query)
            confidence = components.confidence
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Location: {components.location}")
            print(f"  Confidence: {confidence:.3f}")
            print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
            
            # Location queries should have high confidence
            self.assert_condition(confidence >= 0.75, f"Query '{query}' got low confidence: {confidence:.3f}")
            self.assert_condition(components.player_name is not None, f"No player extracted from '{query}'")
            self.assert_condition(components.location is not None, f"No location extracted from '{query}'")
            print("  ✅ PASSED\n")
    
    def test_minutes_filter_queries(self) -> None:
        """Test confidence for minutes filter queries (well-supported feature)."""
        queries = [
            "LeBron James 30+ minutes",
            "Stephen Curry less than 35 minutes",
            "Luka Doncic 25-40 minutes",
            "Giannis over 32 minutes last 10 games",
            "Anthony Davis minimum 28 minutes"
        ]
        
        print("\n=== MINUTES FILTER QUERIES ===")
        for query in queries:
            components = self.parser.parse(query)
            confidence = components.confidence
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Minutes Filter: {components.minutes_filter}")
            print(f"  Confidence: {confidence:.3f}")
            print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
            
            # Minutes filter queries should have high confidence
            self.assert_condition(confidence >= 0.75, f"Query '{query}' got low confidence: {confidence:.3f}")
            self.assert_condition(components.player_name is not None, f"No player extracted from '{query}'")
            self.assert_condition(components.minutes_filter is not None, f"No minutes filter from '{query}'")
            print("  ✅ PASSED\n")
    
    def test_player_relationships_queries(self) -> None:
        """Test confidence for player relationship queries (well-supported feature)."""
        queries = [
            "LeBron James with Anthony Davis",
            "Stephen Curry with Klay Thompson last 10 games",
            "Luka Doncic without Kyrie Irving",
            "Giannis with Damian Lillard at home",
            "Anthony Davis with LeBron but without Russell Westbrook"
        ]
        
        print("\n=== PLAYER RELATIONSHIPS QUERIES ===")
        for query in queries:
            components = self.parser.parse(query)
            confidence = components.confidence
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Players On: {components.players_on}")
            print(f"  Players Off: {components.players_off}")
            print(f"  Confidence: {confidence:.3f}")
            print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
            
            # Player relationship queries should have high confidence
            self.assert_condition(confidence >= 0.70, f"Query '{query}' got low confidence: {confidence:.3f}")
            self.assert_condition(components.player_name is not None, f"No player extracted from '{query}'")
            self.assert_condition(components.players_on or components.players_off, f"No relationships from '{query}'")
            print("  ✅ PASSED\n")
    
    def test_multi_component_queries(self) -> None:
        """Test confidence for multi-component queries using supported features."""
        queries = [
            "LeBron James with Anthony Davis at home last 10 games",
            "Stephen Curry 30+ minutes away games this season",
            "Luka Doncic with Kyrie Irving 25-40 minutes",
            "Giannis without Damian Lillard on the road recent 5 games",
            "Anthony Davis with LeBron James home games 35+ minutes"
        ]
        
        print("\n=== MULTI-COMPONENT QUERIES ===")
        for query in queries:
            components = self.parser.parse(query)
            confidence = components.confidence
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Players On: {components.players_on}")
            print(f"  Players Off: {components.players_off}")
            print(f"  Location: {components.location}")
            print(f"  Time Period: {components.time_period}")
            print(f"  Game Count: {components.game_count}")
            print(f"  Minutes Filter: {components.minutes_filter}")
            print(f"  Confidence: {confidence:.3f}")
            print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
            
            # Multi-component queries should have very high confidence
            self.assert_condition(confidence >= 0.80, f"Query '{query}' got low confidence: {confidence:.3f}")
            self.assert_condition(components.player_name is not None, f"No player extracted from '{query}'")
            
            # Count components to verify complexity
            component_count = 0
            if components.player_name: component_count += 1
            if components.players_on: component_count += 1
            if components.players_off: component_count += 1
            if components.location: component_count += 1
            if components.time_period or components.game_count: component_count += 1
            if components.minutes_filter: component_count += 1
            
            self.assert_condition(component_count >= 3, f"Query '{query}' should have 3+ components, got {component_count}")
            print("  ✅ PASSED\n")
    
    def test_confidence_breakdown_analysis(self) -> None:
        """Analyze confidence breakdown for supported features."""
        test_queries = [
            "LeBron James last 10 games",  # Simple, well-supported
            "Stephen Curry with Klay Thompson at home",  # Multi-component
            "Luka Doncic 30+ minutes away games",  # Complex but supported
            "Giannis without Damian Lillard recent 5 games",  # Relationships
        ]
        
        print("\n=== CONFIDENCE BREAKDOWN ANALYSIS ===")
        for query in test_queries:
            components = self.parser.parse(query)
            breakdown = components.confidence_breakdown
            
            print(f"Query: '{query}'")
            print(f"  Final Confidence: {breakdown.final_confidence:.3f}")
            print(f"  Should use LLM: {breakdown.should_use_llm}")
            print(f"  Coverage Score: {breakdown.coverage_score:.3f}")
            print(f"  Semantic Score: {breakdown.semantic_score:.3f}")
            print(f"  Ambiguity Score: {breakdown.ambiguity_score:.3f}")
            print(f"  Complexity Score: {breakdown.complexity_score:.3f}")
            print(f"  Completeness Score: {breakdown.completeness_score:.3f}")
            print(f"  Uncovered Text: '{breakdown.details.get('uncovered_text', '')}'")
            print(f"  Components Found: {breakdown.details.get('coverage_components', 0)}")
            
            # All scores should be reasonable for supported features
            self.assert_condition(breakdown.coverage_score >= 0.60, f"Low coverage: {breakdown.coverage_score:.3f}")
            self.assert_condition(breakdown.semantic_score >= 0.80, f"Low semantic: {breakdown.semantic_score:.3f}")
            self.assert_condition(breakdown.ambiguity_score >= 0.60, f"High ambiguity: {breakdown.ambiguity_score:.3f}")
            self.assert_condition(breakdown.completeness_score >= 0.70, f"Low completeness: {breakdown.completeness_score:.3f}")
            
            print("  ✅ PASSED\n")
    
    def test_confidence_threshold_validation(self) -> None:
        """Validate that supported queries meet confidence thresholds."""
        well_supported_queries = [
            "LeBron James",
            "Stephen Curry last 10 games", 
            "Luka Doncic at home",
            "Giannis 30+ minutes",
            "Anthony Davis with LeBron James",
            "Jimmy Butler without Tyler Herro",
            "Shai Gilgeous-Alexander away games",
            "Paolo Banchero this season",
            "Victor Wembanyama recent 5 games",
            "Nikola Jokic with Jamal Murray at home last 15 games"
        ]
        
        print("\n=== CONFIDENCE THRESHOLD VALIDATION ===")
        print("Testing well-supported queries against confidence thresholds...")
        
        threshold_75 = 0
        threshold_80 = 0
        threshold_85 = 0
        total_queries = len(well_supported_queries)
        
        for query in well_supported_queries:
            components = self.parser.parse(query)
            confidence = components.confidence
            
            print(f"'{query}' -> {confidence:.3f}")
            
            if confidence >= 0.75:
                threshold_75 += 1
            if confidence >= 0.80:
                threshold_80 += 1
            if confidence >= 0.85:
                threshold_85 += 1
        
        print(f"\nTHRESHOLD ANALYSIS:")
        print(f"  >= 0.75: {threshold_75}/{total_queries} ({threshold_75/total_queries*100:.1f}%)")
        print(f"  >= 0.80: {threshold_80}/{total_queries} ({threshold_80/total_queries*100:.1f}%)")
        print(f"  >= 0.85: {threshold_85}/{total_queries} ({threshold_85/total_queries*100:.1f}%)")
        
        # At least 80% of well-supported queries should meet 0.75 threshold
        self.assert_condition(threshold_75 >= total_queries * 0.8, f"Only {threshold_75}/{total_queries} queries meet 0.75 threshold")
        
        # At least 60% should meet 0.80 threshold
        self.assert_condition(threshold_80 >= total_queries * 0.6, f"Only {threshold_80}/{total_queries} queries meet 0.80 threshold")
        
        print("  ✅ THRESHOLD VALIDATION PASSED\n")
    
    def run_all_tests(self) -> None:
        """Run all tests and report results."""
        try:
            self.test_single_player_queries()
            self.test_time_period_queries()
            self.test_location_queries()
            self.test_minutes_filter_queries()
            self.test_player_relationships_queries()
            self.test_multi_component_queries()
            self.test_confidence_breakdown_analysis()
            self.test_confidence_threshold_validation()
            
            print("=" * 60)
            print(f"✅ ALL TESTS PASSED! ({self.passed} passed, {self.failed} failed)")
            print("=" * 60)
            
        except AssertionError as e:
            print("=" * 60)
            print(f"❌ TEST SUITE FAILED! ({self.passed} passed, {self.failed} failed)")
            print(f"Last error: {e}")
            print("=" * 60)
            return False
        
        return True


def run_supported_features_confidence_test() -> None:
    """Run the supported features confidence test."""
    print("=" * 60)
    print("SUPPORTED FEATURES CONFIDENCE TEST")
    print("=" * 60)
    
    try:
        # Create mock parser
        mock_engine = MockEngine()
        parser = BaseQueryParser(mock_engine)
        
        # Create test instance
        test_instance = TestSupportedFeaturesConfidence(parser)
        
        # Run all tests
        success = test_instance.run_all_tests()
        
        if success:
            print("\n🎉 CONFIDENCE TEST COMPLETED SUCCESSFULLY!")
        else:
            print("\n💥 CONFIDENCE TEST FAILED!")
            
    except Exception as e:
        print(f"\n💥 ERROR RUNNING TESTS: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    run_supported_features_confidence_test() 