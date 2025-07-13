#!/usr/bin/env python3
"""
Advanced multi-component test suite for NBA query parser.

This module tests the most complex multi-component queries that combine 4+ features
including players, relationships, time, location, minutes, opponents, and edge cases.
These tests validate the parser's ability to handle real-world basketball analytics scenarios.
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
class AdvancedTestCase:
    """Advanced multi-component test case for complex parser scenarios."""
    
    query: str
    description: str
    category: str
    complexity_level: int  # 1-5 scale
    
    # Expected results
    expected_main_player: Optional[str] = None
    expected_players_on: List[str] = None
    expected_players_off: List[str] = None
    expected_time_period: Optional[str] = None
    expected_game_count: Optional[int] = None
    expected_location: Optional[str] = None
    expected_minutes_filter: Optional[Tuple[int, int]] = None
    expected_opponent_filters: List[Tuple[str, int]] = None
    expected_intent: Optional[str] = None
    
    # Test criteria
    min_confidence: float = 0.7
    should_pass: bool = True
    allow_partial_match: bool = True
    expected_components_count: int = 4  # Minimum number of components expected
    
    def __post_init__(self):
        """Initialize default empty lists."""
        if self.expected_players_on is None:
            self.expected_players_on = []
        if self.expected_players_off is None:
            self.expected_players_off = []
        if self.expected_opponent_filters is None:
            self.expected_opponent_filters = []


@dataclass
class AdvancedTestResult:
    """Result of an advanced multi-component test execution."""
    
    test_case: AdvancedTestCase
    components: QueryComponents
    passed: bool
    errors: List[str]
    partial_successes: List[str]
    components_found: int
    complexity_handled: bool
    score: float  # 0-1 based on how many components matched


class AdvancedMultiComponentTestSuite:
    """Advanced test suite for complex multi-component NBA queries."""
    
    def __init__(self):
        """Initialize the test suite with database connection."""
        self.engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
        self.parser = BaseQueryParser(self.engine)
    
    def get_advanced_test_dataset(self) -> List[AdvancedTestCase]:
        """
        Get comprehensive advanced test dataset with complex multi-component queries.
        
        Returns:
            List[AdvancedTestCase]: Complete dataset with complex scenarios.
        """
        test_cases = []
        
        # Category 1: 4+ Component Combinations (High Complexity)
        four_plus_components = [
            AdvancedTestCase(
                query="LeBron with AD but without Russ 30+ minutes at home last 10 games",
                description="Player + with + without + minutes + location + time (6 components)",
                category="four_plus_components",
                complexity_level=5,
                expected_main_player="LeBron James",
                expected_players_on=["Anthony Davis"],
                expected_players_off=["Russell Westbrook"],
                expected_minutes_filter=(30, 48),
                expected_location="home",
                expected_time_period="recent",
                expected_game_count=10,
                expected_intent="game_logs",
                expected_components_count=6,
                min_confidence=0.8
            ),
            AdvancedTestCase(
                query="Curry with Draymond and Klay without Wiggins 25-35 minutes on the road this season",
                description="Player + multiple with + without + minutes range + location + time (7 components)",
                category="four_plus_components",
                complexity_level=5,
                expected_main_player="Stephen Curry",
                expected_players_on=["Draymond Green", "Klay Thompson"],
                expected_players_off=["Andrew Wiggins"],
                expected_minutes_filter=(25, 35),
                expected_location="away",
                expected_time_period="season",
                expected_intent="game_logs",
                expected_components_count=7,
                min_confidence=0.8
            ),
            AdvancedTestCase(
                query="Tatum with Brown and Smart without Horford 20+ minutes away games last 15 games",
                description="Player + multiple with + without + minutes + location + time (7 components)",
                category="four_plus_components",
                complexity_level=5,
                expected_main_player="Jayson Tatum",
                expected_players_on=["Jaylen Brown", "Marcus Smart"],
                expected_players_off=["Al Horford"],
                expected_minutes_filter=(20, 48),
                expected_location="away",
                expected_time_period="recent",
                expected_game_count=15,
                expected_intent="game_logs",
                expected_components_count=7,
                min_confidence=0.8
            ),
            AdvancedTestCase(
                query="Durant with Kyrie but without Harden 35+ minutes home games last 20 games",
                description="Player + with + without + minutes + location + time (6 components)",
                category="four_plus_components",
                complexity_level=5,
                expected_main_player="Kevin Durant",
                expected_players_on=["Kyrie Irving"],
                expected_players_off=["James Harden"],
                expected_minutes_filter=(35, 48),
                expected_location="home",
                expected_time_period="recent",
                expected_game_count=20,
                expected_intent="game_logs",
                expected_components_count=6,
                min_confidence=0.8
            ),
        ]
        test_cases.extend(four_plus_components)
        
        # Category 2: Advanced Time-Based Multi-Component Queries
        advanced_time_queries = [
            AdvancedTestCase(
                query="Jokic with Murray and Porter 30+ minutes at home this season",
                description="Player + multiple with + minutes + location + time (5 components)",
                category="advanced_time_queries",
                complexity_level=4,
                expected_main_player="Nikola Jokic",
                expected_players_on=["Jamal Murray", "Michael Porter Jr."],
                expected_minutes_filter=(30, 48),
                expected_location="home",
                expected_time_period="season",
                expected_intent="game_logs",
                expected_components_count=5,
                min_confidence=0.8
            ),
            AdvancedTestCase(
                query="Embiid without Harden 25-40 minutes on the road last 12 games",
                description="Player + without + minutes range + location + time (5 components)",
                category="advanced_time_queries",
                complexity_level=4,
                expected_main_player="Joel Embiid",
                expected_players_off=["James Harden"],
                expected_minutes_filter=(25, 40),
                expected_location="away",
                expected_time_period="recent",
                expected_game_count=12,
                expected_intent="game_logs",
                expected_components_count=5,
                min_confidence=0.8
            ),
            AdvancedTestCase(
                query="Giannis with Dame but without Middleton 35+ minutes away games this season",
                description="Player + with + without + minutes + location + time (6 components)",
                category="advanced_time_queries",
                complexity_level=5,
                expected_main_player="Giannis Antetokounmpo",
                expected_players_on=["Damian Lillard"],
                expected_players_off=["Khris Middleton"],
                expected_minutes_filter=(35, 48),
                expected_location="away",
                expected_time_period="season",
                expected_intent="game_logs",
                expected_components_count=6,
                min_confidence=0.8
            ),
            AdvancedTestCase(
                query="Luka with Kyrie and Washington without Wood 28+ minutes at home last 8 games",
                description="Player + multiple with + without + minutes + location + time (7 components)",
                category="advanced_time_queries",
                complexity_level=5,
                expected_main_player="Luka Doncic",
                expected_players_on=["Kyrie Irving", "P.J. Washington"],
                expected_players_off=["Christian Wood"],
                expected_minutes_filter=(28, 48),
                expected_location="home",
                expected_time_period="recent",
                expected_game_count=8,
                expected_intent="game_logs",
                expected_components_count=7,
                min_confidence=0.8
            ),
        ]
        test_cases.extend(advanced_time_queries)
        
        # Category 3: Complex Player Relationship Scenarios
        complex_relationships = [
            AdvancedTestCase(
                query="Murray with Jokic, Gordon, and KCP without Bruce Brown 30+ minutes",
                description="Player + multiple teammates + multiple without + minutes (6 components)",
                category="complex_relationships",
                complexity_level=4,
                expected_main_player="Jamal Murray",
                expected_players_on=["Nikola Jokic", "Aaron Gordon", "Kentavious Caldwell-Pope"],
                expected_players_off=["Bruce Brown"],
                expected_minutes_filter=(30, 48),
                expected_intent="game_logs",
                expected_components_count=6,
                min_confidence=0.75
            ),
            AdvancedTestCase(
                query="Booker with Durant and Beal without Ayton at home last 15 games",
                description="Player + multiple with + without + location + time (6 components)",
                category="complex_relationships",
                complexity_level=4,
                expected_main_player="Devin Booker",
                expected_players_on=["Kevin Durant", "Bradley Beal"],
                expected_players_off=["Deandre Ayton"],
                expected_location="home",
                expected_time_period="recent",
                expected_game_count=15,
                expected_intent="game_logs",
                expected_components_count=6,
                min_confidence=0.75
            ),
            AdvancedTestCase(
                query="Edwards with McDaniels and Gobert without Towns 25+ minutes away",
                description="Player + multiple with + without + minutes + location (6 components)",
                category="complex_relationships",
                complexity_level=4,
                expected_main_player="Anthony Edwards",
                expected_players_on=["Jaden McDaniels", "Rudy Gobert"],
                expected_players_off=["Karl-Anthony Towns"],
                expected_minutes_filter=(25, 48),
                expected_location="away",
                expected_intent="game_logs",
                expected_components_count=6,
                min_confidence=0.75
            ),
            AdvancedTestCase(
                query="Haliburton with Turner and Siakam without Buddy Hield 20-35 minutes on the road",
                description="Player + multiple with + without + minutes range + location (6 components)",
                category="complex_relationships",
                complexity_level=4,
                expected_main_player="Tyrese Haliburton",
                expected_players_on=["Myles Turner", "Pascal Siakam"],
                expected_players_off=["Buddy Hield"],
                expected_minutes_filter=(20, 35),
                expected_location="away",
                expected_intent="game_logs",
                expected_components_count=6,
                min_confidence=0.75
            ),
        ]
        test_cases.extend(complex_relationships)
        
        # Category 4: Real-World Basketball Scenarios
        real_world_scenarios = [
            AdvancedTestCase(
                query="Curry with the starting lineup but without Wiggins 30+ minutes at home",
                description="Real scenario: starter injuries affecting rotations",
                category="real_world_scenarios",
                complexity_level=4,
                expected_main_player="Stephen Curry",
                expected_players_off=["Andrew Wiggins"],
                expected_minutes_filter=(30, 48),
                expected_location="home",
                expected_intent="game_logs",
                expected_components_count=4,
                min_confidence=0.7,
                allow_partial_match=True
            ),
            AdvancedTestCase(
                query="LeBron with AD when they both play 35+ minutes away games this season",
                description="Real scenario: star player load management analysis",
                category="real_world_scenarios",
                complexity_level=4,
                expected_main_player="LeBron James",
                expected_players_on=["Anthony Davis"],
                expected_minutes_filter=(35, 48),
                expected_location="away",
                expected_time_period="season",
                expected_intent="game_logs",
                expected_components_count=5,
                min_confidence=0.75
            ),
            AdvancedTestCase(
                query="Tatum with Brown when Smart is out 25+ minutes home games last 10",
                description="Real scenario: key player injury impact",
                category="real_world_scenarios",
                complexity_level=4,
                expected_main_player="Jayson Tatum",
                expected_players_on=["Jaylen Brown"],
                expected_players_off=["Marcus Smart"],
                expected_minutes_filter=(25, 48),
                expected_location="home",
                expected_time_period="recent",
                expected_game_count=10,
                expected_intent="game_logs",
                expected_components_count=6,
                min_confidence=0.75
            ),
            AdvancedTestCase(
                query="Durant with Kyrie in clutch time situations 35+ minutes last 20 games",
                description="Real scenario: clutch performance analysis",
                category="real_world_scenarios",
                complexity_level=4,
                expected_main_player="Kevin Durant",
                expected_players_on=["Kyrie Irving"],
                expected_minutes_filter=(35, 48),
                expected_time_period="recent",
                expected_game_count=20,
                expected_intent="game_logs",
                expected_components_count=5,
                min_confidence=0.75
            ),
        ]
        test_cases.extend(real_world_scenarios)
        
        # Category 5: Edge Cases with Multiple Components
        multi_component_edge_cases = [
            AdvancedTestCase(
                query="Brown with Brown but without Brown 30+ minutes at home",
                description="Multiple players with same last name in complex query",
                category="multi_component_edge_cases",
                complexity_level=3,
                expected_minutes_filter=(30, 48),
                expected_location="home",
                expected_intent="game_logs",
                expected_components_count=4,
                min_confidence=0.6,
                allow_partial_match=True
            ),
            AdvancedTestCase(
                query="Young with Young without Young 25+ minutes away games last 5",
                description="Multiple Young players in complex relationships",
                category="multi_component_edge_cases",
                complexity_level=3,
                expected_minutes_filter=(25, 48),
                expected_location="away",
                expected_time_period="recent",
                expected_game_count=5,
                expected_intent="game_logs",
                expected_components_count=5,
                min_confidence=0.6,
                allow_partial_match=True
            ),
            AdvancedTestCase(
                query="Smart defense with Love rebounds but without Green 20+ minutes",
                description="Common words as player names in complex query",
                category="multi_component_edge_cases",
                complexity_level=3,
                expected_main_player="Marcus Smart",
                expected_players_on=["Kevin Love"],
                expected_players_off=["Draymond Green"],
                expected_minutes_filter=(20, 48),
                expected_intent="game_logs",
                expected_components_count=4,
                min_confidence=0.7
            ),
            AdvancedTestCase(
                query="Holiday shooting with Murray assists without Walker 30+ minutes home",
                description="Multiple common words as player names with basketball terms",
                category="multi_component_edge_cases",
                complexity_level=3,
                expected_minutes_filter=(30, 48),
                expected_location="home",
                expected_intent="game_logs",
                expected_components_count=4,
                min_confidence=0.6,
                allow_partial_match=True
            ),
        ]
        test_cases.extend(multi_component_edge_cases)
        
        # Category 6: Advanced Minutes Filter Combinations
        advanced_minutes_combinations = [
            AdvancedTestCase(
                query="Giannis with Lopez but without Portis 32-42 minutes at home this season",
                description="Specific minutes range with multiple components",
                category="advanced_minutes_combinations",
                complexity_level=4,
                expected_main_player="Giannis Antetokounmpo",
                expected_players_on=["Brook Lopez"],
                expected_players_off=["Bobby Portis"],
                expected_minutes_filter=(32, 42),
                expected_location="home",
                expected_time_period="season",
                expected_intent="game_logs",
                expected_components_count=6,
                min_confidence=0.8
            ),
            AdvancedTestCase(
                query="Jokic with Murray exactly 38 minutes on the road last 10 games",
                description="Exact minutes specification with multiple components",
                category="advanced_minutes_combinations",
                complexity_level=4,
                expected_main_player="Nikola Jokic",
                expected_players_on=["Jamal Murray"],
                expected_minutes_filter=(36, 40),  # ±2 minutes for exact
                expected_location="away",
                expected_time_period="recent",
                expected_game_count=10,
                expected_intent="game_logs",
                expected_components_count=5,
                min_confidence=0.8
            ),
            AdvancedTestCase(
                query="Embiid without Harden less than 30 minutes away games this season",
                description="Maximum minutes with multiple components",
                category="advanced_minutes_combinations",
                complexity_level=4,
                expected_main_player="Joel Embiid",
                expected_players_off=["James Harden"],
                expected_minutes_filter=(0, 30),
                expected_location="away",
                expected_time_period="season",
                expected_intent="game_logs",
                expected_components_count=5,
                min_confidence=0.8
            ),
            AdvancedTestCase(
                query="Curry with Draymond over 40 minutes at home last 5 games",
                description="High minutes threshold with multiple components",
                category="advanced_minutes_combinations",
                complexity_level=4,
                expected_main_player="Stephen Curry",
                expected_players_on=["Draymond Green"],
                expected_minutes_filter=(40, 48),
                expected_location="home",
                expected_time_period="recent",
                expected_game_count=5,
                expected_intent="game_logs",
                expected_components_count=5,
                min_confidence=0.8
            ),
        ]
        test_cases.extend(advanced_minutes_combinations)
        
        # Category 7: Ultra-Complex Queries (5 components)
        ultra_complex_queries = [
            AdvancedTestCase(
                query="Luka with Kyrie and Washington but without Wood and Hardaway 30+ minutes at home last 15 games",
                description="Ultra-complex: 8 component query with multiple with/without players",
                category="ultra_complex_queries",
                complexity_level=5,
                expected_main_player="Luka Doncic",
                expected_players_on=["Kyrie Irving", "P.J. Washington"],
                expected_players_off=["Christian Wood", "Tim Hardaway Jr."],
                expected_minutes_filter=(30, 48),
                expected_location="home",
                expected_time_period="recent",
                expected_game_count=15,
                expected_intent="game_logs",
                expected_components_count=8,
                min_confidence=0.75
            ),
            AdvancedTestCase(
                query="Booker with Durant and Beal but without Ayton and Nurkic 35+ minutes away games this season",
                description="Ultra-complex: 8 component query with season timeframe",
                category="ultra_complex_queries",
                complexity_level=5,
                expected_main_player="Devin Booker",
                expected_players_on=["Kevin Durant", "Bradley Beal"],
                expected_players_off=["Deandre Ayton", "Jusuf Nurkic"],
                expected_minutes_filter=(35, 48),
                expected_location="away",
                expected_time_period="season",
                expected_intent="game_logs",
                expected_components_count=8,
                min_confidence=0.75
            ),
            AdvancedTestCase(
                query="Edwards with McDaniels, Gobert, and Conley without Towns and Alexander-Walker 25+ minutes on the road last 12 games",
                description="Ultra-complex: 9 component query with multiple teammates",
                category="ultra_complex_queries",
                complexity_level=5,
                expected_main_player="Anthony Edwards",
                expected_players_on=["Jaden McDaniels", "Rudy Gobert", "Mike Conley"],
                expected_players_off=["Karl-Anthony Towns", "Nickeil Alexander-Walker"],
                expected_minutes_filter=(25, 48),
                expected_location="away",
                expected_time_period="recent",
                expected_game_count=12,
                expected_intent="game_logs",
                expected_components_count=9,
                min_confidence=0.75
            ),
        ]
        test_cases.extend(ultra_complex_queries)
        
        return test_cases
    
    def count_parsed_components(self, components: QueryComponents) -> int:
        """
        Count the number of successfully parsed components.
        
        Args:
            components: QueryComponents object to analyze
            
        Returns:
            int: Number of non-empty components found
        """
        count = 0
        if components.player_name:
            count += 1
        if components.players_on:
            count += len(components.players_on)
        if components.players_off:
            count += len(components.players_off)
        if components.time_period:
            count += 1
        if components.game_count:
            count += 1
        if components.location:
            count += 1
        if components.minutes_filter:
            count += 1
        if components.opponent_filters:
            count += len(components.opponent_filters)
        if components.intent:
            count += 1
        return count
    
    def evaluate_advanced_components(self, test_case: AdvancedTestCase, components: QueryComponents) -> AdvancedTestResult:
        """
        Evaluate components against advanced test case expectations.
        
        Args:
            test_case: AdvancedTestCase with expected results
            components: QueryComponents from parser
            
        Returns:
            AdvancedTestResult: Detailed test result
        """
        errors = []
        partial_successes = []
        total_checks = 0
        passed_checks = 0
        
        # Check main player
        if test_case.expected_main_player:
            total_checks += 1
            if components.player_name == test_case.expected_main_player:
                passed_checks += 1
                partial_successes.append(f"✓ Main player: {components.player_name}")
            else:
                errors.append(f"✗ Main player: expected '{test_case.expected_main_player}', got '{components.player_name}'")
        
        # Check players on
        if test_case.expected_players_on:
            total_checks += 1
            if set(components.players_on) == set(test_case.expected_players_on):
                passed_checks += 1
                partial_successes.append(f"✓ Players on: {components.players_on}")
            else:
                errors.append(f"✗ Players on: expected {test_case.expected_players_on}, got {components.players_on}")
        
        # Check players off
        if test_case.expected_players_off:
            total_checks += 1
            if set(components.players_off) == set(test_case.expected_players_off):
                passed_checks += 1
                partial_successes.append(f"✓ Players off: {components.players_off}")
            else:
                errors.append(f"✗ Players off: expected {test_case.expected_players_off}, got {components.players_off}")
        
        # Check time period
        if test_case.expected_time_period:
            total_checks += 1
            if components.time_period == test_case.expected_time_period:
                passed_checks += 1
                partial_successes.append(f"✓ Time period: {components.time_period}")
            else:
                errors.append(f"✗ Time period: expected '{test_case.expected_time_period}', got '{components.time_period}'")
        
        # Check game count
        if test_case.expected_game_count:
            total_checks += 1
            if components.game_count == test_case.expected_game_count:
                passed_checks += 1
                partial_successes.append(f"✓ Game count: {components.game_count}")
            else:
                errors.append(f"✗ Game count: expected {test_case.expected_game_count}, got {components.game_count}")
        
        # Check location
        if test_case.expected_location:
            total_checks += 1
            if components.location == test_case.expected_location:
                passed_checks += 1
                partial_successes.append(f"✓ Location: {components.location}")
            else:
                errors.append(f"✗ Location: expected '{test_case.expected_location}', got '{components.location}'")
        
        # Check minutes filter
        if test_case.expected_minutes_filter:
            total_checks += 1
            if components.minutes_filter == test_case.expected_minutes_filter:
                passed_checks += 1
                partial_successes.append(f"✓ Minutes filter: {components.minutes_filter}")
            else:
                errors.append(f"✗ Minutes filter: expected {test_case.expected_minutes_filter}, got {components.minutes_filter}")
        
        # Check opponent filters
        if test_case.expected_opponent_filters:
            total_checks += 1
            if set(components.opponent_filters) == set(test_case.expected_opponent_filters):
                passed_checks += 1
                partial_successes.append(f"✓ Opponent filters: {components.opponent_filters}")
            else:
                errors.append(f"✗ Opponent filters: expected {test_case.expected_opponent_filters}, got {components.opponent_filters}")
        
        # Check intent
        if test_case.expected_intent:
            total_checks += 1
            if components.intent == test_case.expected_intent:
                passed_checks += 1
                partial_successes.append(f"✓ Intent: {components.intent}")
            else:
                errors.append(f"✗ Intent: expected '{test_case.expected_intent}', got '{components.intent}'")
        
        # Check confidence
        total_checks += 1
        if components.confidence >= test_case.min_confidence:
            passed_checks += 1
            partial_successes.append(f"✓ Confidence: {components.confidence:.3f} >= {test_case.min_confidence}")
        else:
            errors.append(f"✗ Confidence: {components.confidence:.3f} < {test_case.min_confidence}")
        
        # Check component count
        components_found = self.count_parsed_components(components)
        complexity_handled = components_found >= test_case.expected_components_count
        
        if complexity_handled:
            partial_successes.append(f"✓ Component count: {components_found} >= {test_case.expected_components_count}")
        else:
            errors.append(f"✗ Component count: {components_found} < {test_case.expected_components_count}")
        
        # Calculate score
        score = passed_checks / total_checks if total_checks > 0 else 0.0
        
        # Determine pass/fail
        if test_case.allow_partial_match:
            passed = score >= 0.6 and components.confidence >= test_case.min_confidence
        else:
            passed = score >= 0.8 and components.confidence >= test_case.min_confidence
        
        # Override for should_pass=False cases
        if not test_case.should_pass:
            passed = not passed
        
        return AdvancedTestResult(
            test_case=test_case,
            components=components,
            passed=passed,
            errors=errors,
            partial_successes=partial_successes,
            components_found=components_found,
            complexity_handled=complexity_handled,
            score=score
        )
    
    def run_advanced_test(self, test_case: AdvancedTestCase) -> AdvancedTestResult:
        """
        Run a single advanced test case.
        
        Args:
            test_case: AdvancedTestCase to execute
            
        Returns:
            AdvancedTestResult: Test execution result
        """
        try:
            components = self.parser.parse(test_case.query)
            result = self.evaluate_advanced_components(test_case, components)
            return result
        except Exception as e:
            return AdvancedTestResult(
                test_case=test_case,
                components=QueryComponents(),
                passed=False,
                errors=[f"Parser error: {str(e)}"],
                partial_successes=[],
                components_found=0,
                complexity_handled=False,
                score=0.0
            )
    
    def run_comprehensive_advanced_test(self) -> None:
        """
        Run the complete advanced multi-component test suite.
        
        Executes all test cases and provides detailed reporting of results,
        including complexity analysis and component-level breakdown.
        """
        print("🔬 Advanced Multi-Component NBA Query Parser Test Suite")
        print("=" * 80)
        
        test_cases = self.get_advanced_test_dataset()
        results = []
        
        # Group tests by category
        categories = {}
        for test_case in test_cases:
            if test_case.category not in categories:
                categories[test_case.category] = []
            categories[test_case.category].append(test_case)
        
        # Run tests by category
        for category, cases in categories.items():
            print(f"\n📊 Category: {category.replace('_', ' ').title()}")
            print("-" * 60)
            
            category_results = []
            for test_case in cases:
                result = self.run_advanced_test(test_case)
                results.append(result)
                category_results.append(result)
                
                # Print individual test result
                status = "✅ PASS" if result.passed else "❌ FAIL"
                complexity_indicator = "⭐" * test_case.complexity_level
                print(f"{status} {complexity_indicator} {test_case.description}")
                print(f"    Query: '{test_case.query}'")
                print(f"    Components: {result.components_found}/{test_case.expected_components_count}")
                print(f"    Confidence: {result.components.confidence:.3f}")
                print(f"    Score: {result.score:.3f}")
                
                if result.partial_successes:
                    print(f"    Successes: {', '.join(result.partial_successes[:3])}")
                if result.errors:
                    print(f"    Errors: {', '.join(result.errors[:2])}")
                print()
            
            # Category summary
            category_passed = sum(1 for r in category_results if r.passed)
            category_total = len(category_results)
            avg_components = sum(r.components_found for r in category_results) / len(category_results)
            avg_complexity = sum(r.test_case.complexity_level for r in category_results) / len(category_results)
            
            print(f"📈 Category Summary:")
            print(f"    Success Rate: {category_passed}/{category_total} ({category_passed/category_total*100:.1f}%)")
            print(f"    Avg Components: {avg_components:.1f}")
            print(f"    Avg Complexity: {avg_complexity:.1f}/5")
        
        # Overall summary
        print("\n🎯 Overall Advanced Test Results")
        print("=" * 80)
        
        total_passed = sum(1 for r in results if r.passed)
        total_tests = len(results)
        overall_success_rate = total_passed / total_tests * 100
        
        complexity_breakdown = {}
        for result in results:
            complexity = result.test_case.complexity_level
            if complexity not in complexity_breakdown:
                complexity_breakdown[complexity] = {'passed': 0, 'total': 0}
            complexity_breakdown[complexity]['total'] += 1
            if result.passed:
                complexity_breakdown[complexity]['passed'] += 1
        
        print(f"📊 Overall Results:")
        print(f"    Total Tests: {total_tests}")
        print(f"    Passed: {total_passed}")
        print(f"    Success Rate: {overall_success_rate:.1f}%")
        print(f"    Average Components Found: {sum(r.components_found for r in results) / len(results):.1f}")
        
        print(f"\n🔥 Complexity Analysis:")
        for complexity in sorted(complexity_breakdown.keys()):
            data = complexity_breakdown[complexity]
            rate = data['passed'] / data['total'] * 100
            stars = "⭐" * complexity
            print(f"    Level {complexity} {stars}: {data['passed']}/{data['total']} ({rate:.1f}%)")
        
        print(f"\n🎖️ Performance Benchmarks:")
        ultra_complex = [r for r in results if r.test_case.complexity_level == 5]
        ultra_passed = sum(1 for r in ultra_complex if r.passed)
        if ultra_complex:
            print(f"    Ultra-Complex (Level 5): {ultra_passed}/{len(ultra_complex)} ({ultra_passed/len(ultra_complex)*100:.1f}%)")
        
        multi_component = [r for r in results if r.components_found >= 6]
        multi_passed = sum(1 for r in multi_component if r.passed)
        if multi_component:
            print(f"    6+ Components: {multi_passed}/{len(multi_component)} ({multi_passed/len(multi_component)*100:.1f}%)")
        
        high_confidence = [r for r in results if r.components.confidence >= 0.8]
        print(f"    High Confidence (≥0.8): {len(high_confidence)}/{total_tests} ({len(high_confidence)/total_tests*100:.1f}%)")
        
        # Target benchmarks
        print(f"\n🎯 Target Benchmarks:")
        print(f"    Overall Success Rate: {overall_success_rate:.1f}% (Target: 75.0%)")
        print(f"    Level 4-5 Success Rate: {sum(1 for r in results if r.test_case.complexity_level >= 4 and r.passed)}/{sum(1 for r in results if r.test_case.complexity_level >= 4)} (Target: 70.0%)")
        print(f"    Multi-Component Success: {multi_passed}/{len(multi_component)} (Target: 80.0%)")
        
        if overall_success_rate >= 75.0:
            print("\n🏆 EXCELLENT: Parser handles advanced multi-component queries exceptionally well!")
        elif overall_success_rate >= 65.0:
            print("\n✅ GOOD: Parser performs well on complex multi-component queries")
        else:
            print("\n⚠️  NEEDS IMPROVEMENT: Parser struggles with advanced multi-component scenarios")


def main():
    """Main function to run the advanced multi-component test suite."""
    suite = AdvancedMultiComponentTestSuite()
    suite.run_comprehensive_advanced_test()


if __name__ == "__main__":
    main() 