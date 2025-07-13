#!/usr/bin/env python3
"""
Confidence threshold testing for NBA query parser.

This module tests the confidence scoring system and LLM fallback threshold
to determine optimal settings for when queries should be sent to LLM vs
handled directly by the parser.
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
class ConfidenceTestCase:
    """Test case for confidence threshold analysis."""
    
    query: str
    description: str
    category: str
    expected_confidence_level: str  # "high", "medium", "low"
    should_use_llm: bool
    parser_should_handle: bool  # Whether parser should handle this well


class ConfidenceThresholdTestSuite:
    """Test suite for analyzing confidence thresholds and LLM fallback."""
    
    def __init__(self):
        """Initialize the test suite with database connection."""
        self.engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
        self.parser = BaseQueryParser(self.engine)
        
        # Current threshold from parser
        self.current_threshold = self.parser.confidence_calculator.llm_threshold
        print(f"Current LLM threshold: {self.current_threshold}")
    
    def get_confidence_test_dataset(self) -> List[ConfidenceTestCase]:
        """
        Get test dataset spanning different confidence levels.
        
        Returns:
            List[ConfidenceTestCase]: Test cases across confidence spectrum.
        """
        test_cases = []
        
        # Category 1: High Confidence Queries (Should NOT use LLM)
        high_confidence_tests = [
            ConfidenceTestCase(
                query="LeBron last 10 games",
                description="Simple player + time query",
                category="high_confidence",
                expected_confidence_level="high",
                should_use_llm=False,
                parser_should_handle=True
            ),
            ConfidenceTestCase(
                query="Curry home games this season",
                description="Player + location + time",
                category="high_confidence", 
                expected_confidence_level="high",
                should_use_llm=False,
                parser_should_handle=True
            ),
            ConfidenceTestCase(
                query="Giannis with Lopez 30+ minutes",
                description="Player + with + minutes",
                category="high_confidence",
                expected_confidence_level="high", 
                should_use_llm=False,
                parser_should_handle=True
            ),
            ConfidenceTestCase(
                query="Durant without Kyrie away games last 15",
                description="Multi-component clear query",
                category="high_confidence",
                expected_confidence_level="high",
                should_use_llm=False,
                parser_should_handle=True
            ),
            ConfidenceTestCase(
                query="Tatum with Brown but without Smart at home",
                description="Complex but clear relationships",
                category="high_confidence",
                expected_confidence_level="high",
                should_use_llm=False,
                parser_should_handle=True
            ),
        ]
        test_cases.extend(high_confidence_tests)
        
        # Category 2: Medium Confidence Queries (Borderline)
        medium_confidence_tests = [
            ConfidenceTestCase(
                query="Johnson recent games",
                description="Ambiguous last name",
                category="medium_confidence",
                expected_confidence_level="medium",
                should_use_llm=True,  # Due to ambiguity
                parser_should_handle=True  # But parser can handle
            ),
            ConfidenceTestCase(
                query="LeBron when AD is playing well",
                description="Vague condition",
                category="medium_confidence",
                expected_confidence_level="medium",
                should_use_llm=True,
                parser_should_handle=False
            ),
            ConfidenceTestCase(
                query="Curry clutch time performance",
                description="Unsupported feature (clutch time)",
                category="medium_confidence", 
                expected_confidence_level="medium",
                should_use_llm=True,
                parser_should_handle=False
            ),
            ConfidenceTestCase(
                query="Giannis triple double games",
                description="Stat category not implemented",
                category="medium_confidence",
                expected_confidence_level="medium",
                should_use_llm=True,
                parser_should_handle=False
            ),
            ConfidenceTestCase(
                query="Durant vs top defenses",
                description="Vague opponent reference",
                category="medium_confidence",
                expected_confidence_level="medium",
                should_use_llm=True,
                parser_should_handle=False
            ),
        ]
        test_cases.extend(medium_confidence_tests)
        
        # Category 3: Low Confidence Queries (Should use LLM)
        low_confidence_tests = [
            ConfidenceTestCase(
                query="best player performance recently",
                description="No specific player",
                category="low_confidence",
                expected_confidence_level="low",
                should_use_llm=True,
                parser_should_handle=False
            ),
            ConfidenceTestCase(
                query="who had the most points last week",
                description="Comparative query",
                category="low_confidence",
                expected_confidence_level="low", 
                should_use_llm=True,
                parser_should_handle=False
            ),
            ConfidenceTestCase(
                query="analyze shooting efficiency trends",
                description="Complex analysis request",
                category="low_confidence",
                expected_confidence_level="low",
                should_use_llm=True,
                parser_should_handle=False
            ),
            ConfidenceTestCase(
                query="compare Lakers and Warriors offense",
                description="Team comparison",
                category="low_confidence",
                expected_confidence_level="low",
                should_use_llm=True,
                parser_should_handle=False
            ),
            ConfidenceTestCase(
                query="what happened in the game yesterday",
                description="Vague game reference",
                category="low_confidence",
                expected_confidence_level="low",
                should_use_llm=True,
                parser_should_handle=False
            ),
        ]
        test_cases.extend(low_confidence_tests)
        
        # Category 4: Edge Cases and Problematic Queries
        edge_case_tests = [
            ConfidenceTestCase(
                query="",
                description="Empty query",
                category="edge_cases",
                expected_confidence_level="low",
                should_use_llm=True,
                parser_should_handle=False
            ),
            ConfidenceTestCase(
                query="asdfghjkl random text",
                description="Nonsense query",
                category="edge_cases",
                expected_confidence_level="low",
                should_use_llm=True,
                parser_should_handle=False
            ),
            ConfidenceTestCase(
                query="LeBron with with without",
                description="Malformed relationship",
                category="edge_cases",
                expected_confidence_level="low",
                should_use_llm=True,
                parser_should_handle=False
            ),
            ConfidenceTestCase(
                query="Curry 500 minutes last game",
                description="Impossible values",
                category="edge_cases",
                expected_confidence_level="low",
                should_use_llm=True,
                parser_should_handle=False
            ),
        ]
        test_cases.extend(edge_case_tests)
        
        # Category 5: Currently Unsupported but Clear Queries
        unsupported_clear_tests = [
            ConfidenceTestCase(
                query="LeBron scoring 30+ points last 10 games",
                description="Clear stat threshold query",
                category="unsupported_clear",
                expected_confidence_level="medium",
                should_use_llm=True,  # Due to unsupported feature
                parser_should_handle=False  # Not yet implemented
            ),
            ConfidenceTestCase(
                query="Curry against Western Conference teams",
                description="Clear conference filtering",
                category="unsupported_clear",
                expected_confidence_level="medium",
                should_use_llm=True,
                parser_should_handle=False
            ),
            ConfidenceTestCase(
                query="Giannis in January games",
                description="Clear month filtering",
                category="unsupported_clear",
                expected_confidence_level="medium",
                should_use_llm=True,
                parser_should_handle=False
            ),
        ]
        test_cases.extend(unsupported_clear_tests)
        
        return test_cases
    
    def test_threshold_analysis(self, threshold: float = None) -> Dict[str, Any]:
        """
        Test confidence threshold and analyze LLM fallback behavior.
        
        Args:
            threshold: Optional threshold to test (default uses current)
            
        Returns:
            Dict with analysis results
        """
        if threshold is not None:
            # Temporarily change threshold for testing
            original_threshold = self.parser.confidence_calculator.llm_threshold
            self.parser.confidence_calculator.llm_threshold = threshold
        
        test_cases = self.get_confidence_test_dataset()
        results = []
        
        print(f"\n🔬 Testing Confidence Threshold: {threshold or self.current_threshold}")
        print("=" * 80)
        
        # Group by category for analysis
        categories = {}
        for test_case in test_cases:
            if test_case.category not in categories:
                categories[test_case.category] = []
            categories[test_case.category].append(test_case)
        
        total_correct_decisions = 0
        total_tests = len(test_cases)
        
        for category, cases in categories.items():
            print(f"\n📂 Category: {category.replace('_', ' ').title()}")
            print("-" * 60)
            
            category_correct = 0
            
            for test_case in cases:
                try:
                    components = self.parser.parse(test_case.query)
                    
                    # Analyze confidence and LLM decision
                    confidence = components.confidence
                    should_use_llm = components.confidence_breakdown.should_use_llm if components.confidence_breakdown else confidence < (threshold or self.current_threshold)
                    
                    # Check if decision matches expectation
                    correct_decision = should_use_llm == test_case.should_use_llm
                    
                    if correct_decision:
                        category_correct += 1
                        total_correct_decisions += 1
                        status = "✅ CORRECT"
                    else:
                        status = "❌ INCORRECT"
                    
                    # Confidence level assessment
                    if confidence >= 0.85:
                        actual_level = "high"
                    elif confidence >= 0.65:
                        actual_level = "medium" 
                    else:
                        actual_level = "low"
                    
                    print(f"{status} {test_case.description}")
                    print(f"    Query: '{test_case.query}'")
                    print(f"    Confidence: {confidence:.3f} ({actual_level})")
                    print(f"    Should use LLM: {should_use_llm} (expected: {test_case.should_use_llm})")
                    
                    if components.confidence_breakdown:
                        breakdown = components.confidence_breakdown
                        print(f"    Breakdown: coverage={breakdown.coverage_score:.2f}, semantic={breakdown.semantic_score:.2f}, ambiguity={breakdown.ambiguity_score:.2f}")
                    
                    print()
                    
                    results.append({
                        'test_case': test_case,
                        'confidence': confidence,
                        'should_use_llm': should_use_llm,
                        'correct_decision': correct_decision,
                        'actual_level': actual_level
                    })
                    
                except Exception as e:
                    print(f"❌ ERROR {test_case.description}")
                    print(f"    Query: '{test_case.query}'")
                    print(f"    Error: {str(e)}")
                    print()
            
            # Category summary
            category_accuracy = category_correct / len(cases) * 100
            print(f"📈 Category Accuracy: {category_correct}/{len(cases)} ({category_accuracy:.1f}%)")
        
        # Overall analysis
        overall_accuracy = total_correct_decisions / total_tests * 100
        
        print(f"\n🎯 Overall Threshold Analysis")
        print("=" * 80)
        print(f"Threshold tested: {threshold or self.current_threshold}")
        print(f"Overall accuracy: {total_correct_decisions}/{total_tests} ({overall_accuracy:.1f}%)")
        
        # Analyze confidence distribution
        confidence_values = [r['confidence'] for r in results]
        high_conf = sum(1 for c in confidence_values if c >= 0.85)
        medium_conf = sum(1 for c in confidence_values if 0.65 <= c < 0.85)
        low_conf = sum(1 for c in confidence_values if c < 0.65)
        
        print(f"\n📊 Confidence Distribution:")
        print(f"    High (≥0.85): {high_conf}/{total_tests} ({high_conf/total_tests*100:.1f}%)")
        print(f"    Medium (0.65-0.85): {medium_conf}/{total_tests} ({medium_conf/total_tests*100:.1f}%)")
        print(f"    Low (<0.65): {low_conf}/{total_tests} ({low_conf/total_tests*100:.1f}%)")
        
        # LLM routing analysis
        llm_routed = sum(1 for r in results if r['should_use_llm'])
        parser_handled = total_tests - llm_routed
        
        print(f"\n🔀 Routing Analysis:")
        print(f"    Sent to LLM: {llm_routed}/{total_tests} ({llm_routed/total_tests*100:.1f}%)")
        print(f"    Handled by Parser: {parser_handled}/{total_tests} ({parser_handled/total_tests*100:.1f}%)")
        
        # Restore original threshold if changed
        if threshold is not None:
            self.parser.confidence_calculator.llm_threshold = original_threshold
        
        return {
            'threshold': threshold or self.current_threshold,
            'overall_accuracy': overall_accuracy,
            'total_tests': total_tests,
            'correct_decisions': total_correct_decisions,
            'llm_routed': llm_routed,
            'confidence_distribution': {
                'high': high_conf,
                'medium': medium_conf, 
                'low': low_conf
            },
            'results': results
        }
    
    def compare_thresholds(self, thresholds: List[float]) -> None:
        """
        Compare multiple threshold values to find optimal setting.
        
        Args:
            thresholds: List of threshold values to test
        """
        print("\n🔬 Confidence Threshold Comparison")
        print("=" * 80)
        
        comparison_results = []
        
        for threshold in thresholds:
            print(f"\n{'='*20} Testing Threshold: {threshold} {'='*20}")
            result = self.test_threshold_analysis(threshold)
            comparison_results.append(result)
        
        # Summary comparison
        print(f"\n📊 Threshold Comparison Summary")
        print("=" * 80)
        print(f"{'Threshold':<10} {'Accuracy':<10} {'LLM %':<10} {'Parser %':<12} {'High Conf %':<12}")
        print("-" * 60)
        
        for result in comparison_results:
            threshold = result['threshold']
            accuracy = result['overall_accuracy']
            llm_pct = result['llm_routed'] / result['total_tests'] * 100
            parser_pct = 100 - llm_pct
            high_conf_pct = result['confidence_distribution']['high'] / result['total_tests'] * 100
            
            print(f"{threshold:<10.2f} {accuracy:<10.1f} {llm_pct:<10.1f} {parser_pct:<12.1f} {high_conf_pct:<12.1f}")
        
        # Recommendation
        best_result = max(comparison_results, key=lambda x: x['overall_accuracy'])
        print(f"\n🎯 Recommendation:")
        print(f"    Optimal threshold: {best_result['threshold']:.2f}")
        print(f"    Achieves {best_result['overall_accuracy']:.1f}% accuracy")
        print(f"    Routes {best_result['llm_routed']}/{best_result['total_tests']} queries to LLM")


def main():
    """Main function to run confidence threshold analysis."""
    suite = ConfidenceThresholdTestSuite()
    
    # Test current threshold
    suite.test_threshold_analysis()
    
    # Compare different thresholds
    thresholds_to_test = [0.60, 0.65, 0.70, 0.75, 0.80, 0.85]
    suite.compare_thresholds(thresholds_to_test)


if __name__ == "__main__":
    main() 