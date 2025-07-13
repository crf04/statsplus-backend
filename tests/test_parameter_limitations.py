#!/usr/bin/env python3
"""
Parameter limitations test for NBA query parser.

This module tests specific parameter types and edge cases to identify
what the parser cannot handle currently.
"""

import sys
import os
from typing import List, Dict, Tuple, Optional, Any
from dataclasses import dataclass

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.nl_query.parser import BaseQueryParser, QueryComponents
from sqlalchemy import create_engine
from config import Config

if False:  # TYPE_CHECKING
    from _pytest.capture import CaptureFixture
    from _pytest.fixtures import FixtureRequest
    from _pytest.logging import LogCaptureFixture
    from _pytest.monkeypatch import MonkeyPatch
    from pytest_mock.plugin import MockerFixture


@dataclass
class ParameterLimitationTest:
    """Test case for parameter limitations."""
    
    query: str
    description: str
    category: str
    expected_failure_reason: str
    should_fail: bool = True


class ParameterLimitationTestSuite:
    """Test suite for identifying parameter limitations."""
    
    def __init__(self):
        """Initialize the test suite with database connection."""
        self.engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
        self.parser = BaseQueryParser(self.engine)
    
    def get_limitation_tests(self) -> List[ParameterLimitationTest]:
        """
        Get test cases for parameter limitations.
        
        Returns:
            List[ParameterLimitationTest]: Test cases for unsupported parameters.
        """
        tests = []
        
        # Category 1: Unimplemented Stat Categories
        stat_category_tests = [
            ParameterLimitationTest(
                query="LeBron points per game last 10 games",
                description="Specific stat category (points per game) not extracted",
                category="unimplemented_stat_categories",
                expected_failure_reason="stat_categories not implemented"
            ),
            ParameterLimitationTest(
                query="Curry three point percentage this season",
                description="Specific stat category (3PT%) not extracted",
                category="unimplemented_stat_categories",
                expected_failure_reason="stat_categories not implemented"
            ),
            ParameterLimitationTest(
                query="Giannis rebounds and assists last 15 games",
                description="Multiple stat categories not extracted",
                category="unimplemented_stat_categories",
                expected_failure_reason="stat_categories not implemented"
            ),
            ParameterLimitationTest(
                query="Durant field goal percentage away games",
                description="Specific shooting stat not extracted",
                category="unimplemented_stat_categories",
                expected_failure_reason="stat_categories not implemented"
            ),
        ]
        tests.extend(stat_category_tests)
        
        # Category 2: Advanced Opponent Filters Not Supported
        advanced_opponent_tests = [
            ParameterLimitationTest(
                query="LeBron against Western Conference teams",
                description="Conference-based opponent filtering not supported",
                category="advanced_opponent_filters",
                expected_failure_reason="Conference filters not implemented"
            ),
            ParameterLimitationTest(
                query="Curry against teams with winning records",
                description="Record-based opponent filtering not supported",
                category="advanced_opponent_filters",
                expected_failure_reason="Record-based filters not implemented"
            ),
            ParameterLimitationTest(
                query="Giannis against playoff teams",
                description="Playoff status filtering not supported",
                category="advanced_opponent_filters",
                expected_failure_reason="Playoff status filters not implemented"
            ),
            ParameterLimitationTest(
                query="Durant against teams allowing 110+ points",
                description="Opponent performance-based filtering not supported",
                category="advanced_opponent_filters",
                expected_failure_reason="Performance-based opponent filters not implemented"
            ),
        ]
        tests.extend(advanced_opponent_tests)
        
        # Category 3: Advanced Time Filters Not Supported
        advanced_time_tests = [
            ParameterLimitationTest(
                query="LeBron in January games",
                description="Month-specific filtering not supported",
                category="advanced_time_filters",
                expected_failure_reason="Month-specific filters not implemented"
            ),
            ParameterLimitationTest(
                query="Curry in back-to-back games",
                description="Back-to-back game filtering not supported",
                category="advanced_time_filters",
                expected_failure_reason="Back-to-back filters not implemented"
            ),
            ParameterLimitationTest(
                query="Giannis in playoff games",
                description="Game type filtering not supported",
                category="advanced_time_filters",
                expected_failure_reason="Game type filters not implemented"
            ),
            ParameterLimitationTest(
                query="Durant between January 1 and February 15",
                description="Specific date range filtering not supported",
                category="advanced_time_filters",
                expected_failure_reason="date_range not implemented"
            ),
        ]
        tests.extend(advanced_time_tests)
        
        # Category 4: Game Situation Filters Not Supported
        game_situation_tests = [
            ParameterLimitationTest(
                query="LeBron in clutch time situations",
                description="Clutch time filtering not supported",
                category="game_situation_filters",
                expected_failure_reason="Clutch time filters not implemented"
            ),
            ParameterLimitationTest(
                query="Curry in fourth quarter",
                description="Quarter-specific filtering not supported",
                category="game_situation_filters",
                expected_failure_reason="Quarter filters not implemented"
            ),
            ParameterLimitationTest(
                query="Giannis in overtime games",
                description="Overtime filtering not supported",
                category="game_situation_filters",
                expected_failure_reason="Overtime filters not implemented"
            ),
            ParameterLimitationTest(
                query="Durant in close games (within 5 points)",
                description="Game margin filtering not supported",
                category="game_situation_filters",
                expected_failure_reason="Game margin filters not implemented"
            ),
        ]
        tests.extend(game_situation_tests)
        
        # Category 5: Advanced Player Conditions Not Supported
        advanced_player_tests = [
            ParameterLimitationTest(
                query="LeBron when he scores 30+ points",
                description="Player performance conditions not supported",
                category="advanced_player_conditions",
                expected_failure_reason="Performance conditions not implemented"
            ),
            ParameterLimitationTest(
                query="Curry when he makes 5+ threes",
                description="Specific stat thresholds not supported",
                category="advanced_player_conditions",
                expected_failure_reason="Stat threshold conditions not implemented"
            ),
            ParameterLimitationTest(
                query="Giannis when he gets a triple double",
                description="Triple double conditions not supported",
                category="advanced_player_conditions",
                expected_failure_reason="Triple double conditions not implemented"
            ),
        ]
        tests.extend(advanced_player_tests)
        
        # Category 6: Complex Logical Operators Not Supported
        logical_operator_tests = [
            ParameterLimitationTest(
                query="LeBron with AD or Westbrook but not both",
                description="Complex logical OR conditions not supported",
                category="logical_operators",
                expected_failure_reason="Complex logical operators not implemented"
            ),
            ParameterLimitationTest(
                query="Curry with either Klay or Poole",
                description="Either/or conditions not supported",
                category="logical_operators",
                expected_failure_reason="Either/or logic not implemented"
            ),
            ParameterLimitationTest(
                query="Giannis with at least 2 of Lopez, Middleton, Holiday",
                description="Conditional player requirements not supported",
                category="logical_operators",
                expected_failure_reason="Conditional logic not implemented"
            ),
        ]
        tests.extend(logical_operator_tests)
        
        # Category 7: Team-Based Queries Not Fully Supported
        team_query_tests = [
            ParameterLimitationTest(
                query="Lakers starting lineup last 10 games",
                description="Team lineup queries not supported",
                category="team_queries",
                expected_failure_reason="Team lineup extraction not implemented"
            ),
            ParameterLimitationTest(
                query="Warriors bench performance this season",
                description="Team unit queries not supported",
                category="team_queries",
                expected_failure_reason="Team unit extraction not implemented"
            ),
            ParameterLimitationTest(
                query="Celtics when Tatum is out",
                description="Team performance with player out not supported",
                category="team_queries",
                expected_failure_reason="Team impact queries not implemented"
            ),
        ]
        tests.extend(team_query_tests)
        
        # Category 8: Comparative Queries Not Supported
        comparative_tests = [
            ParameterLimitationTest(
                query="LeBron vs KD head to head stats",
                description="Head-to-head comparisons not supported",
                category="comparative_queries",
                expected_failure_reason="Player comparisons not implemented"
            ),
            ParameterLimitationTest(
                query="Curry better than league average three point percentage",
                description="League average comparisons not supported",
                category="comparative_queries",
                expected_failure_reason="League average comparisons not implemented"
            ),
            ParameterLimitationTest(
                query="Giannis compared to other power forwards",
                description="Position-based comparisons not supported",
                category="comparative_queries",
                expected_failure_reason="Position comparisons not implemented"
            ),
        ]
        tests.extend(comparative_tests)
        
        return tests
    
    def run_limitation_tests(self) -> None:
        """
        Run parameter limitation tests to identify unsupported features.
        """
        print("🔍 NBA Query Parser Parameter Limitations Test")
        print("=" * 80)
        
        tests = self.get_limitation_tests()
        
        # Group tests by category
        categories = {}
        for test in tests:
            if test.category not in categories:
                categories[test.category] = []
            categories[test.category].append(test)
        
        unsupported_parameters = []
        
        for category, test_cases in categories.items():
            print(f"\n📂 Category: {category.replace('_', ' ').title()}")
            print("-" * 60)
            
            for test_case in test_cases:
                try:
                    components = self.parser.parse(test_case.query)
                    
                    # Check if the expected parameter is missing
                    parameter_extracted = self._check_parameter_extraction(test_case, components)
                    
                    if not parameter_extracted:
                        status = "❌ UNSUPPORTED"
                        unsupported_parameters.append(test_case.expected_failure_reason)
                    else:
                        status = "✅ SUPPORTED"
                    
                    print(f"{status} {test_case.description}")
                    print(f"    Query: '{test_case.query}'")
                    print(f"    Reason: {test_case.expected_failure_reason}")
                    print(f"    Components: {self._summarize_components(components)}")
                    print()
                    
                except Exception as e:
                    print(f"❌ ERROR {test_case.description}")
                    print(f"    Query: '{test_case.query}'")
                    print(f"    Error: {str(e)}")
                    print()
        
        # Summary
        print("\n📊 Parameter Limitations Summary")
        print("=" * 80)
        
        unique_limitations = list(set(unsupported_parameters))
        
        print(f"🚫 Unsupported Parameters/Features:")
        for i, limitation in enumerate(unique_limitations, 1):
            print(f"    {i}. {limitation}")
        
        print(f"\n📈 Statistics:")
        print(f"    Total limitation tests: {len(tests)}")
        print(f"    Unique unsupported features: {len(unique_limitations)}")
        print(f"    Categories tested: {len(categories)}")
        
        print(f"\n🎯 Implementation Priority:")
        print(f"    1. Stat categories extraction (most commonly needed)")
        print(f"    2. Advanced opponent filters (conference, record)")
        print(f"    3. Date range filtering (specific dates)")
        print(f"    4. Game situation filters (clutch, quarter)")
        print(f"    5. Complex logical operators (OR, either/or)")
    
    def _check_parameter_extraction(self, test_case: ParameterLimitationTest, components: QueryComponents) -> bool:
        """
        Check if the expected parameter was extracted.
        
        Args:
            test_case: Test case to check
            components: Parsed components
            
        Returns:
            bool: True if parameter was extracted, False otherwise
        """
        # Check based on expected failure reason
        if "stat_categories" in test_case.expected_failure_reason:
            return len(components.stat_categories) > 0
        elif "date_range" in test_case.expected_failure_reason:
            return components.date_range is not None
        elif "Conference filters" in test_case.expected_failure_reason:
            return any("conference" in str(f).lower() for f in components.opponent_filters)
        elif "Record-based filters" in test_case.expected_failure_reason:
            return any("record" in str(f).lower() for f in components.opponent_filters)
        elif "Playoff status filters" in test_case.expected_failure_reason:
            return any("playoff" in str(f).lower() for f in components.opponent_filters)
        elif "Performance-based opponent filters" in test_case.expected_failure_reason:
            return any("points" in str(f).lower() for f in components.opponent_filters)
        else:
            # For other cases, check if query was parsed with reasonable confidence
            return components.confidence > 0.7
    
    def _summarize_components(self, components: QueryComponents) -> str:
        """
        Summarize the parsed components for display.
        
        Args:
            components: QueryComponents to summarize
            
        Returns:
            str: Summary of components
        """
        parts = []
        if components.player_name:
            parts.append(f"player={components.player_name}")
        if components.time_period:
            parts.append(f"time={components.time_period}")
        if components.game_count:
            parts.append(f"games={components.game_count}")
        if components.location:
            parts.append(f"location={components.location}")
        if components.minutes_filter:
            parts.append(f"minutes={components.minutes_filter}")
        if components.opponent_filters:
            parts.append(f"opponents={len(components.opponent_filters)}")
        if components.players_on:
            parts.append(f"with={len(components.players_on)}")
        if components.players_off:
            parts.append(f"without={len(components.players_off)}")
        if components.stat_categories:
            parts.append(f"stats={len(components.stat_categories)}")
        
        return ", ".join(parts) if parts else "none"


def main():
    """Main function to run the parameter limitation tests."""
    suite = ParameterLimitationTestSuite()
    suite.run_limitation_tests()


if __name__ == "__main__":
    main() 