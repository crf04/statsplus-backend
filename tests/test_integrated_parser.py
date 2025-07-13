#!/usr/bin/env python3
"""
Integrated test suite for NBA query parser.

This module tests multiple parser functionalities working together to validate
the complete parsing pipeline including entity recognition, relationships,
filters, confidence scoring, and complex query handling.
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
class IntegratedTestCase:
    """Comprehensive test case for integrated parser functionality."""
    
    query: str
    description: str
    category: str
    
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
    allow_partial_match: bool = True  # Allow some components to be missing if others are correct
    
    def __post_init__(self):
        """Initialize default empty lists."""
        if self.expected_players_on is None:
            self.expected_players_on = []
        if self.expected_players_off is None:
            self.expected_players_off = []
        if self.expected_opponent_filters is None:
            self.expected_opponent_filters = []


@dataclass
class IntegratedTestResult:
    """Result of an integrated test case execution."""
    
    test_case: IntegratedTestCase
    components: QueryComponents
    passed: bool
    errors: List[str]
    partial_successes: List[str]
    score: float  # 0-1 based on how many components matched


class IntegratedParserTestSuite:
    """Comprehensive integrated test suite for NBA query parser."""
    
    def __init__(self):
        """Initialize test suite with parser."""
        engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
        self.parser = BaseQueryParser(engine)
    
    def get_integrated_test_dataset(self) -> List[IntegratedTestCase]:
        """
        Get comprehensive integrated test dataset.
        
        Returns:
            List[IntegratedTestCase]: Complete test dataset with multiple components.
        """
        test_cases = []
        
        # Category 1: Single Player + Time + Location
        single_player_time_location = [
            IntegratedTestCase(
                query="LeBron last 10 games at home",
                description="Single player with time and location",
                category="single_player_time_location",
                expected_main_player="LeBron James",
                expected_time_period="recent",
                expected_game_count=10,
                expected_location="home",
                expected_intent="game_logs"
            ),
            IntegratedTestCase(
                query="Curry away games this season",
                description="Player alias with location and season",
                category="single_player_time_location",
                expected_main_player="Stephen Curry",
                expected_time_period="season",
                expected_location="away",
                expected_intent="game_logs"
            ),
            IntegratedTestCase(
                query="Durant on the road last 5 games",
                description="Last name with location and time",
                category="single_player_time_location",
                expected_main_player="Kevin Durant",
                expected_time_period="recent",
                expected_game_count=5,
                expected_location="away",
                expected_intent="game_logs"
            ),
        ]
        test_cases.extend(single_player_time_location)
        
        # Category 2: Player Relationships + Time
        player_relationships_time = [
            IntegratedTestCase(
                query="Murray with Jokic last 15 games",
                description="Player with teammate and time filter",
                category="player_relationships_time",
                expected_main_player="Jamal Murray",
                expected_players_on=["Nikola Jokic"],
                expected_time_period="recent",
                expected_game_count=15,
                expected_intent="game_logs"
            ),
            IntegratedTestCase(
                query="Tatum without Brown this season",
                description="Player without teammate for season",
                category="player_relationships_time",
                expected_main_player="Jayson Tatum",
                expected_players_off=["Jaylen Brown"],
                expected_time_period="season",
                expected_intent="game_logs"
            ),
            IntegratedTestCase(
                query="Curry with Draymond and Klay last 20 games",
                description="Player with multiple teammates and time",
                category="player_relationships_time",
                expected_main_player="Stephen Curry",
                expected_players_on=["Draymond Green", "Klay Thompson"],
                expected_time_period="recent",
                expected_game_count=20,
                expected_intent="game_logs"
            ),
        ]
        test_cases.extend(player_relationships_time)
        
        # Category 3: Player + Minutes Filter + Location
        player_minutes_location = [
            IntegratedTestCase(
                query="LeBron 30+ minutes at home",
                description="Player with minutes filter and location",
                category="player_minutes_location",
                expected_main_player="LeBron James",
                expected_minutes_filter=(30, 48),
                expected_location="home",
                expected_intent="game_logs"
            ),
            IntegratedTestCase(
                query="Embiid less than 25 minutes on the road",
                description="Player with max minutes and away location",
                category="player_minutes_location",
                expected_main_player="Joel Embiid",
                expected_minutes_filter=(0, 25),
                expected_location="away",
                expected_intent="game_logs"
            ),
            IntegratedTestCase(
                query="Jokic 35-42 minutes at home",
                description="Player with minutes range and home location",
                category="player_minutes_location",
                expected_main_player="Nikola Jokic",
                expected_minutes_filter=(35, 42),
                expected_location="home",
                expected_intent="game_logs"
            ),
        ]
        test_cases.extend(player_minutes_location)
        
        # Category 4: Complex Multi-Component Queries
        complex_multi_component = [
            IntegratedTestCase(
                query="Curry with Draymond but without Klay last 10 games at home",
                description="Complex query with with/without, time, and location",
                category="complex_multi_component",
                expected_main_player="Stephen Curry",
                expected_players_on=["Draymond Green"],
                expected_players_off=["Klay Thompson"],
                expected_time_period="recent",
                expected_game_count=10,
                expected_location="home",
                expected_intent="game_logs"
            ),
            IntegratedTestCase(
                query="LeBron with AD 30+ minutes away games this season",
                description="Player relationships with minutes and location filters",
                category="complex_multi_component",
                expected_main_player="LeBron James",
                expected_players_on=["Anthony Davis"],
                expected_minutes_filter=(30, 48),
                expected_location="away",
                expected_time_period="season",
                expected_intent="game_logs"
            ),
            IntegratedTestCase(
                query="Tatum without Brown and Smart last 15 games on the road",
                description="Multiple player exclusions with time and location",
                category="complex_multi_component",
                expected_main_player="Jayson Tatum",
                expected_players_off=["Jaylen Brown", "Marcus Smart"],
                expected_time_period="recent",
                expected_game_count=15,
                expected_location="away",
                expected_intent="game_logs"
            ),
        ]
        test_cases.extend(complex_multi_component)
        
        # Category 5: Common Last Names + Filters
        common_names_filters = [
            IntegratedTestCase(
                query="Johnson home games last 10",
                description="Common last name with location and time",
                category="common_names_filters",
                # Note: Don't specify exact player due to alphabetical ordering
                expected_location="home",
                expected_time_period="recent",
                expected_game_count=10,
                expected_intent="game_logs",
                allow_partial_match=True
            ),
            IntegratedTestCase(
                query="Williams 25+ minutes away games",
                description="Common last name with minutes and location",
                category="common_names_filters",
                expected_minutes_filter=(25, 48),
                expected_location="away",
                expected_intent="game_logs",
                allow_partial_match=True
            ),
            IntegratedTestCase(
                query="Thompson with Brown last 5 games",
                description="Common last names in relationship",
                category="common_names_filters",
                expected_time_period="recent",
                expected_game_count=5,
                expected_intent="game_logs",
                allow_partial_match=True
            ),
        ]
        test_cases.extend(common_names_filters)
        
        # Category 6: Edge Cases and Error Handling
        edge_cases = [
            IntegratedTestCase(
                query="Smart defense at home",
                description="Common word as last name with location",
                category="edge_cases",
                expected_main_player="Marcus Smart",
                expected_location="home",
                expected_intent="game_logs"
            ),
            IntegratedTestCase(
                query="Love rebounds 20+ minutes",
                description="Common word as last name with minutes",
                category="edge_cases",
                expected_main_player="Kevin Love",
                expected_minutes_filter=(20, 48),
                expected_intent="game_logs"
            ),
            IntegratedTestCase(
                query="Holiday shooting on the road last 7 games",
                description="Common word as last name with multiple filters",
                category="edge_cases",
                # Allow any Holiday player due to multiple in database
                expected_location="away",
                expected_time_period="recent",
                expected_game_count=7,
                expected_intent="game_logs",
                allow_partial_match=True
            ),
        ]
        test_cases.extend(edge_cases)
        
        # Category 7: Confidence and Quality Tests
        confidence_tests = [
            IntegratedTestCase(
                query="Giannis triple double performance",
                description="Clear query should have high confidence",
                category="confidence_tests",
                expected_main_player="Giannis Antetokounmpo",
                expected_intent="game_logs",
                min_confidence=0.8
            ),
            IntegratedTestCase(
                query="some random player stats",
                description="Ambiguous query should have low confidence",
                category="confidence_tests",
                should_pass=False,
                min_confidence=0.5
            ),
            IntegratedTestCase(
                query="KD last 10 games at home with Kyrie",
                description="Multiple clear components should have high confidence",
                category="confidence_tests",
                expected_main_player="Kevin Durant",
                expected_players_on=["Kyrie Irving"],
                expected_time_period="recent",
                expected_game_count=10,
                expected_location="home",
                min_confidence=0.85
            ),
        ]
        test_cases.extend(confidence_tests)
        
        # Category 8: Intent Classification
        intent_tests = [
            IntegratedTestCase(
                query="LeBron game logs last 10",
                description="Explicit game logs intent",
                category="intent_tests",
                expected_main_player="LeBron James",
                expected_intent="game_logs",
                expected_time_period="recent",
                expected_game_count=10
            ),
            IntegratedTestCase(
                query="Curry player profile overview",
                description="Player profile intent",
                category="intent_tests",
                expected_main_player="Stephen Curry",
                expected_intent="player_profile"
            ),
        ]
        test_cases.extend(intent_tests)
        
        return test_cases
    
    def evaluate_components(self, test_case: IntegratedTestCase, components: QueryComponents) -> IntegratedTestResult:
        """
        Evaluate how well the parsed components match expected results.
        
        Args:
            test_case: The test case with expected results.
            components: The actual parsed components.
            
        Returns:
            IntegratedTestResult: Detailed evaluation results.
        """
        errors = []
        partial_successes = []
        score_components = []
        
        # Check main player
        if test_case.expected_main_player:
            if components.player_name == test_case.expected_main_player:
                partial_successes.append(f"Main player: {components.player_name}")
                score_components.append(1.0)
            elif components.player_name:
                if test_case.allow_partial_match:
                    partial_successes.append(f"Player found (different): {components.player_name}")
                    score_components.append(0.7)  # Partial credit for finding a player
                else:
                    errors.append(f"Main player: expected {test_case.expected_main_player}, got {components.player_name}")
                    score_components.append(0.0)
            else:
                errors.append(f"Main player: expected {test_case.expected_main_player}, got None")
                score_components.append(0.0)
        elif components.player_name and not test_case.allow_partial_match:
            errors.append(f"Main player: expected None, got {components.player_name}")
            score_components.append(0.0)
        else:
            score_components.append(1.0)  # No expectation, any result is fine
        
        # Check players on
        if test_case.expected_players_on:
            if set(components.players_on) == set(test_case.expected_players_on):
                partial_successes.append(f"Players on: {components.players_on}")
                score_components.append(1.0)
            elif components.players_on:
                overlap = set(components.players_on) & set(test_case.expected_players_on)
                if overlap:
                    partial_successes.append(f"Partial players on match: {list(overlap)}")
                    score_components.append(len(overlap) / len(test_case.expected_players_on))
                else:
                    errors.append(f"Players on: expected {test_case.expected_players_on}, got {components.players_on}")
                    score_components.append(0.0)
            else:
                errors.append(f"Players on: expected {test_case.expected_players_on}, got []")
                score_components.append(0.0)
        else:
            score_components.append(1.0)
        
        # Check players off
        if test_case.expected_players_off:
            if set(components.players_off) == set(test_case.expected_players_off):
                partial_successes.append(f"Players off: {components.players_off}")
                score_components.append(1.0)
            elif components.players_off:
                overlap = set(components.players_off) & set(test_case.expected_players_off)
                if overlap:
                    partial_successes.append(f"Partial players off match: {list(overlap)}")
                    score_components.append(len(overlap) / len(test_case.expected_players_off))
                else:
                    errors.append(f"Players off: expected {test_case.expected_players_off}, got {components.players_off}")
                    score_components.append(0.0)
            else:
                errors.append(f"Players off: expected {test_case.expected_players_off}, got []")
                score_components.append(0.0)
        else:
            score_components.append(1.0)
        
        # Check time period
        if test_case.expected_time_period:
            if components.time_period == test_case.expected_time_period:
                partial_successes.append(f"Time period: {components.time_period}")
                score_components.append(1.0)
            else:
                errors.append(f"Time period: expected {test_case.expected_time_period}, got {components.time_period}")
                score_components.append(0.0)
        else:
            score_components.append(1.0)
        
        # Check game count
        if test_case.expected_game_count:
            if components.game_count == test_case.expected_game_count:
                partial_successes.append(f"Game count: {components.game_count}")
                score_components.append(1.0)
            else:
                errors.append(f"Game count: expected {test_case.expected_game_count}, got {components.game_count}")
                score_components.append(0.0)
        else:
            score_components.append(1.0)
        
        # Check location
        if test_case.expected_location:
            if components.location == test_case.expected_location:
                partial_successes.append(f"Location: {components.location}")
                score_components.append(1.0)
            else:
                errors.append(f"Location: expected {test_case.expected_location}, got {components.location}")
                score_components.append(0.0)
        else:
            score_components.append(1.0)
        
        # Check minutes filter
        if test_case.expected_minutes_filter:
            if components.minutes_filter == test_case.expected_minutes_filter:
                partial_successes.append(f"Minutes filter: {components.minutes_filter}")
                score_components.append(1.0)
            else:
                errors.append(f"Minutes filter: expected {test_case.expected_minutes_filter}, got {components.minutes_filter}")
                score_components.append(0.0)
        else:
            score_components.append(1.0)
        
        # Check intent
        if test_case.expected_intent:
            if components.intent == test_case.expected_intent:
                partial_successes.append(f"Intent: {components.intent}")
                score_components.append(1.0)
            else:
                errors.append(f"Intent: expected {test_case.expected_intent}, got {components.intent}")
                score_components.append(0.0)
        else:
            score_components.append(1.0)
        
        # Calculate overall score
        score = sum(score_components) / len(score_components) if score_components else 0.0
        
        # Check confidence threshold
        if components.confidence < test_case.min_confidence:
            errors.append(f"Confidence {components.confidence:.3f} below minimum {test_case.min_confidence}")
        
        # Determine if test passed
        if test_case.should_pass:
            passed = score >= 0.7 and components.confidence >= test_case.min_confidence
        else:
            passed = score < 0.3 or components.confidence < test_case.min_confidence
        
        return IntegratedTestResult(
            test_case=test_case,
            components=components,
            passed=passed,
            errors=errors,
            partial_successes=partial_successes,
            score=score
        )
    
    def run_integrated_test(self, test_case: IntegratedTestCase) -> IntegratedTestResult:
        """
        Run a single integrated test case.
        
        Args:
            test_case: The test case to execute.
            
        Returns:
            IntegratedTestResult: Results of the test execution.
        """
        try:
            components = self.parser.parse(test_case.query)
            return self.evaluate_components(test_case, components)
            
        except Exception as e:
            return IntegratedTestResult(
                test_case=test_case,
                components=QueryComponents(raw_query=test_case.query),
                passed=False,
                errors=[f"Exception: {str(e)}"],
                partial_successes=[],
                score=0.0
            )
    
    def run_comprehensive_integrated_test(self) -> None:
        """Run the complete integrated test suite and report results."""
        print("=" * 80)
        print("COMPREHENSIVE INTEGRATED NBA QUERY PARSER TEST SUITE")
        print("Testing Multiple Components Working Together")
        print("=" * 80)
        
        # Get all test cases
        all_test_cases = self.get_integrated_test_dataset()
        categories = list(set(tc.category for tc in all_test_cases))
        
        print(f"\nTotal integrated test cases: {len(all_test_cases)}")
        print(f"Categories: {len(categories)}")
        
        # Run tests by category
        results_by_category: Dict[str, List[IntegratedTestResult]] = {}
        
        for category in categories:
            category_tests = [tc for tc in all_test_cases if tc.category == category]
            results = [self.run_integrated_test(tc) for tc in category_tests]
            results_by_category[category] = results
        
        # Report results by category
        overall_passed = 0
        overall_total = 0
        overall_score = 0.0
        
        for category in sorted(categories):
            results = results_by_category[category]
            passed_count = sum(1 for r in results if r.passed)
            total_count = len(results)
            avg_score = sum(r.score for r in results) / len(results) if results else 0.0
            
            print(f"\n{category.upper().replace('_', ' ')}: {passed_count}/{total_count} ({passed_count/total_count*100:.1f}%)")
            print(f"Average component score: {avg_score:.3f}")
            
            for result in results:
                status = "✅" if result.passed else "❌"
                confidence = result.components.confidence
                print(f"  {status} '{result.test_case.query}' (score: {result.score:.2f}, conf: {confidence:.3f})")
                
                if result.partial_successes:
                    for success in result.partial_successes[:2]:  # Show first 2 successes
                        print(f"    ✓ {success}")
                
                if result.errors and not result.passed:
                    for error in result.errors[:2]:  # Show first 2 errors
                        print(f"    ✗ {error}")
            
            overall_passed += passed_count
            overall_total += total_count
            overall_score += avg_score
        
        # Overall summary
        overall_percentage = (overall_passed / overall_total * 100) if overall_total > 0 else 0
        avg_overall_score = overall_score / len(categories) if categories else 0
        
        print(f"\n" + "=" * 80)
        print(f"INTEGRATED TEST SUITE RESULTS")
        print(f"=" * 80)
        print(f"Overall Success Rate: {overall_passed}/{overall_total} ({overall_percentage:.1f}%)")
        print(f"Average Component Score: {avg_overall_score:.3f}")
        print(f"Categories Tested: {len(categories)}")
        
        # Determine overall success
        min_success_rate = 85.0  # 85% for integrated tests
        min_component_score = 0.75  # 75% average component accuracy
        
        if overall_percentage >= min_success_rate and avg_overall_score >= min_component_score:
            print(f"🎉 INTEGRATED TEST SUITE PASSED!")
            print(f"   - Success rate: {overall_percentage:.1f}% (target: {min_success_rate:.1f}%)")
            print(f"   - Component accuracy: {avg_overall_score:.3f} (target: {min_component_score:.2f})")
            print(f"   - Multiple parser components working together successfully!")
        else:
            print(f"⚠️  INTEGRATED TEST SUITE ISSUES:")
            if overall_percentage < min_success_rate:
                print(f"   - Success rate {overall_percentage:.1f}% below target {min_success_rate:.1f}%")
            if avg_overall_score < min_component_score:
                print(f"   - Component accuracy {avg_overall_score:.3f} below target {min_component_score:.2f}")
        
        # Test spaCy integration specifically
        print(f"\n" + "=" * 80)
        print("SPACY ENTITY INTEGRATION VALIDATION")
        print("=" * 80)
        
        spacy_test_queries = [
            "Johnson with Brown last 10 games at home",
            "Williams 30+ minutes on the road",
            "Thompson without Green this season"
        ]
        
        spacy_success = True
        for query in spacy_test_queries:
            components = self.parser.parse(query)
            doc = self.parser.nlp(query)
            entities = [(ent.text, ent.label_, ent.ent_id_) for ent in doc.ents]
            
            has_entities = len(entities) > 0
            has_main_player = components.player_name is not None
            
            status = "✅" if has_entities and has_main_player else "❌"
            print(f"{status} '{query}'")
            print(f"    Entities: {entities}")
            print(f"    Main player: {components.player_name}")
            print(f"    Other components: location={components.location}, time={components.time_period}")
            
            if not (has_entities and has_main_player):
                spacy_success = False
        
        if spacy_success:
            print(f"🎉 spaCy entity recognition integrated successfully with other components!")
        else:
            print(f"❌ spaCy entity recognition integration issues detected")


def main():
    """Main function to run the integrated test suite."""
    try:
        test_suite = IntegratedParserTestSuite()
        test_suite.run_comprehensive_integrated_test()
        
    except Exception as e:
        print(f"\n💥 ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main() 