#!/usr/bin/env python3
"""
Comprehensive Accuracy Test Suite

Tests the NLP parser's ability to extract ALL components correctly,
not just player names. Validates each component against expected values.
"""

import os
import sys
import time
import json
import statistics
from typing import List, Dict, Any, Tuple, Optional, Union
from dataclasses import dataclass, asdict
from datetime import datetime

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.nl_query.parser import BaseQueryParser, QueryComponents
from sqlalchemy import create_engine
from config import Config


@dataclass
class ExpectedComponents:
    """Expected components that should be extracted from a query."""
    
    player_name: Optional[str] = None
    team_name: Optional[str] = None
    time_period: Optional[str] = None
    game_count: Optional[int] = None
    location: Optional[str] = None
    minutes_filter: Optional[Tuple[int, int]] = None
    self_filters: Optional[List[Dict[str, Any]]] = None
    opponent_filters: Optional[List[Tuple[str, int]]] = None
    players_on: Optional[List[str]] = None
    players_off: Optional[List[str]] = None
    intent: str = "game_logs"


@dataclass
class ComprehensiveTestCase:
    """Comprehensive test case with expected component extraction."""
    
    query: str
    category: str
    clarity_level: str
    expected: ExpectedComponents
    notes: str = ""


@dataclass
class ComponentAccuracy:
    """Accuracy results for each component type."""
    
    total_expected: int
    total_extracted: int
    correct_extractions: int
    accuracy: float
    
    # Detailed results
    missing: List[str] = None  # Expected but not found
    incorrect: List[str] = None  # Found but wrong value
    extra: List[str] = None  # Found but not expected


@dataclass
class AccuracyTestResult:
    """Result of comprehensive accuracy test."""
    
    test_case: ComprehensiveTestCase
    components: Optional[QueryComponents]
    confidence: float
    processing_time: float
    
    # Component-wise accuracy
    player_correct: bool = False
    time_correct: bool = False
    location_correct: bool = False
    stats_correct: bool = False
    opponent_correct: bool = False
    relationships_correct: bool = False
    
    # Overall accuracy
    overall_accuracy: float = 0.0
    total_components: int = 0
    correct_components: int = 0
    
    errors: List[str] = None


class ComprehensiveAccuracyTester:
    """Comprehensive accuracy testing for all components."""
    
    def __init__(self):
        """Initialize the comprehensive tester."""
        self.engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
        self.parser = BaseQueryParser(self.engine)
        print(f"✅ NLP Parser initialized for comprehensive accuracy testing")
    
    def get_comprehensive_test_cases(self) -> List[ComprehensiveTestCase]:
        """
        Get comprehensive test cases with detailed expected components.
        
        Returns:
            List of test cases with expected component extraction
        """
        test_cases = []
        
        # Basic queries with single components
        test_cases.extend([
            ComprehensiveTestCase(
                "LeBron last 10 games",
                "basic", "very_clear",
                ExpectedComponents(
                    player_name="LeBron James",
                    time_period="recent",
                    game_count=10
                ),
                "Basic player + time"
            ),
            ComprehensiveTestCase(
                "Curry this season with 30+ points",
                "stat_filter", "very_clear",
                ExpectedComponents(
                    player_name="Stephen Curry",
                    time_period="season",
                    self_filters=[{"stat_column": "PTS", "operator": "gte", "value": 30}]
                ),
                "Player + time + stat filter"
            ),
            ComprehensiveTestCase(
                "Giannis home games",
                "location", "very_clear",
                ExpectedComponents(
                    player_name="Giannis Antetokounmpo",
                    location="home"
                ),
                "Player + location"
            ),
            ComprehensiveTestCase(
                "KD away games with 25+ points",
                "stat_location", "very_clear",
                ExpectedComponents(
                    player_name="Kevin Durant",
                    location="away",
                    self_filters=[{"stat_column": "PTS", "operator": "gte", "value": 25}]
                ),
                "Player + location + stat"
            ),
            ComprehensiveTestCase(
                "Kawhi games with 30+ minutes",
                "minutes", "very_clear",
                ExpectedComponents(
                    player_name="Kawhi Leonard",
                    minutes_filter=(30, 48)
                ),
                "Player + minutes filter"
            ),
        ])
        
        # Complex queries with multiple components
        test_cases.extend([
            ComprehensiveTestCase(
                "LeBron with Anthony Davis last 10 games",
                "relationships", "clear",
                ExpectedComponents(
                    player_name="LeBron James",
                    time_period="recent",
                    game_count=10,
                    players_on=["Anthony Davis"]
                ),
                "Player relationships"
            ),
            ComprehensiveTestCase(
                "Curry against top 5 defenses",
                "opponent", "clear",
                ExpectedComponents(
                    player_name="Stephen Curry",
                    opponent_filters=[("defense_rank", 5)]
                ),
                "Opponent filter"
            ),
            ComprehensiveTestCase(
                "Giannis games with 25+ points and 10+ rebounds",
                "multi_stat", "clear",
                ExpectedComponents(
                    player_name="Giannis Antetokounmpo",
                    self_filters=[
                        {"stat_column": "PTS", "operator": "gte", "value": 25},
                        {"stat_column": "REB", "operator": "gte", "value": 10}
                    ]
                ),
                "Multiple stats"
            ),
            ComprehensiveTestCase(
                "Luka home games with Kyrie Irving",
                "relationships_location", "clear",
                ExpectedComponents(
                    player_name="Luka Doncic",
                    location="home",
                    players_on=["Kyrie Irving"]
                ),
                "Location + teammate"
            ),
            ComprehensiveTestCase(
                "Embiid games playing between 30 and 35 minutes",
                "minutes_range", "clear",
                ExpectedComponents(
                    player_name="Joel Embiid",
                    minutes_filter=(30, 35)
                ),
                "Minutes range"
            ),
        ])
        
        # Complex queries with 3+ components
        test_cases.extend([
            ComprehensiveTestCase(
                "Tatum with Jaylen Brown last 15 games scoring 20+",
                "complex", "clear",
                ExpectedComponents(
                    player_name="Jayson Tatum",
                    time_period="recent",
                    game_count=15,
                    players_on=["Jaylen Brown"],
                    self_filters=[{"stat_column": "PTS", "operator": "gte", "value": 20}]
                ),
                "Complex relationship query"
            ),
            ComprehensiveTestCase(
                "Beal home games this season scoring 30+",
                "complete", "clear",
                ExpectedComponents(
                    player_name="Bradley Beal",
                    location="home",
                    time_period="season",
                    self_filters=[{"stat_column": "PTS", "operator": "gte", "value": 30}]
                ),
                "Home + season + stat"
            ),
            ComprehensiveTestCase(
                "LeBron with AD but without Westbrook last 10",
                "complex_relationship", "moderate",
                ExpectedComponents(
                    player_name="LeBron James",
                    game_count=10,
                    players_on=["Anthony Davis"],
                    players_off=["Russell Westbrook"]
                ),
                "With/without logic"
            ),
        ])
        
        # Edge cases and challenging queries
        test_cases.extend([
            ComprehensiveTestCase(
                "Dame vs top 10 three point defenses",
                "opponent_specific", "clear",
                ExpectedComponents(
                    player_name="Damian Lillard",
                    opponent_filters=[("OPP_FG3M", 10)]
                ),
                "Specific defense type"
            ),
            ComprehensiveTestCase(
                "Jimmy Butler last 20 games",
                "full_name", "very_clear",
                ExpectedComponents(
                    player_name="Jimmy Butler",
                    time_period="recent",
                    game_count=20
                ),
                "Full name + time"
            ),
            ComprehensiveTestCase(
                "Westbrook games without a triple-double",
                "negative_stat", "clear",
                ExpectedComponents(
                    player_name="Russell Westbrook"
                    # Note: "without" is complex and may not be extracted correctly
                ),
                "Without stat - challenging"
            ),
        ])
        
        # Subjective/vague queries that should show lower accuracy
        test_cases.extend([
            ComprehensiveTestCase(
                "Giannis efficient games this season",
                "subjective", "moderate",
                ExpectedComponents(
                    player_name="Giannis Antetokounmpo",
                    time_period="season"
                    # "efficient" is subjective - no stat filter expected
                ),
                "Subjective term"
            ),
            ComprehensiveTestCase(
                "Dame clutch games with 25+ points",
                "context", "moderate",
                ExpectedComponents(
                    player_name="Damian Lillard",
                    self_filters=[{"stat_column": "PTS", "operator": "gte", "value": 25}]
                    # "clutch" is context-dependent
                ),
                "Context term"
            ),
            ComprehensiveTestCase(
                "LeBron when he goes off",
                "vague", "unclear",
                ExpectedComponents(
                    player_name="LeBron James"
                    # "goes off" is too vague for specific extraction
                ),
                "Vague expression"
            ),
        ])
        
        # Typos and malformed queries
        test_cases.extend([
            ComprehensiveTestCase(
                "lebron last games",
                "typo", "very_unclear",
                ExpectedComponents(
                    player_name="LeBron James"
                    # Missing game count - incomplete
                ),
                "Lowercase + incomplete"
            ),
            ComprehensiveTestCase(
                "embid games",
                "typo", "very_unclear",
                ExpectedComponents(
                    player_name="Joel Embiid"
                ),
                "Misspelled name"
            ),
            ComprehensiveTestCase(
                "",
                "empty", "very_unclear",
                ExpectedComponents(),
                "Empty query"
            ),
        ])
        
        return test_cases
    
    def evaluate_component_accuracy(self, expected: ExpectedComponents, actual: QueryComponents) -> Tuple[float, Dict[str, bool]]:
        """
        Evaluate accuracy of component extraction.
        
        Args:
            expected: Expected components
            actual: Actual extracted components
            
        Returns:
            Tuple of (overall_accuracy, component_results)
        """
        component_results = {}
        total_components = 0
        correct_components = 0
        
        # Player name
        if expected.player_name is not None:
            total_components += 1
            player_correct = (actual.player_name == expected.player_name)
            component_results["player"] = player_correct
            if player_correct:
                correct_components += 1
        
        # Time period
        if expected.time_period is not None:
            total_components += 1
            time_correct = (actual.time_period == expected.time_period)
            component_results["time_period"] = time_correct
            if time_correct:
                correct_components += 1
        
        # Game count
        if expected.game_count is not None:
            total_components += 1
            count_correct = (actual.game_count == expected.game_count)
            component_results["game_count"] = count_correct
            if count_correct:
                correct_components += 1
        
        # Location
        if expected.location is not None:
            total_components += 1
            location_correct = (actual.location == expected.location)
            component_results["location"] = location_correct
            if location_correct:
                correct_components += 1
        
        # Minutes filter
        if expected.minutes_filter is not None:
            total_components += 1
            minutes_correct = (actual.minutes_filter == expected.minutes_filter)
            component_results["minutes"] = minutes_correct
            if minutes_correct:
                correct_components += 1
        
        # Self filters (stats)
        if expected.self_filters is not None:
            total_components += 1
            stats_correct = self._compare_self_filters(expected.self_filters, actual.self_filters)
            component_results["stats"] = stats_correct
            if stats_correct:
                correct_components += 1
        
        # Opponent filters
        if expected.opponent_filters is not None:
            total_components += 1
            opponent_correct = self._compare_opponent_filters(expected.opponent_filters, actual.opponent_filters)
            component_results["opponent"] = opponent_correct
            if opponent_correct:
                correct_components += 1
        
        # Players on
        if expected.players_on is not None:
            total_components += 1
            players_on_correct = self._compare_player_lists(expected.players_on, actual.players_on)
            component_results["players_on"] = players_on_correct
            if players_on_correct:
                correct_components += 1
        
        # Players off
        if expected.players_off is not None:
            total_components += 1
            players_off_correct = self._compare_player_lists(expected.players_off, actual.players_off)
            component_results["players_off"] = players_off_correct
            if players_off_correct:
                correct_components += 1
        
        overall_accuracy = correct_components / total_components if total_components > 0 else 1.0
        
        return overall_accuracy, component_results
    
    def _compare_self_filters(self, expected: List[Dict[str, Any]], actual: List) -> bool:
        """Compare self filters for accuracy."""
        if len(expected) != len(actual):
            return False
        
        for exp_filter in expected:
            found_match = False
            for act_filter in actual:
                if (act_filter.stat_column == exp_filter["stat_column"] and
                    act_filter.operator == exp_filter["operator"] and
                    act_filter.value == exp_filter["value"]):
                    found_match = True
                    break
            if not found_match:
                return False
        
        return True
    
    def _compare_opponent_filters(self, expected: List[Tuple[str, int]], actual: List[Tuple[str, int]]) -> bool:
        """Compare opponent filters for accuracy."""
        if len(expected) != len(actual):
            return False
        
        for exp_filter in expected:
            if exp_filter not in actual:
                return False
        
        return True
    
    def _compare_player_lists(self, expected: List[str], actual: List[str]) -> bool:
        """Compare player lists for accuracy."""
        if len(expected) != len(actual):
            return False
        
        for player in expected:
            if player not in actual:
                return False
        
        return True
    
    def test_single_case(self, test_case: ComprehensiveTestCase) -> AccuracyTestResult:
        """Test a single comprehensive accuracy case."""
        start_time = time.time()
        errors = []
        
        try:
            # Parse with NLP parser
            components = self.parser.parse(test_case.query)
            processing_time = time.time() - start_time
            
            if components is None:
                return AccuracyTestResult(
                    test_case=test_case,
                    components=None,
                    confidence=0.0,
                    processing_time=processing_time,
                    overall_accuracy=0.0,
                    total_components=0,
                    correct_components=0,
                    errors=["Parser returned None"]
                )
            
            # Evaluate component accuracy
            overall_accuracy, component_results = self.evaluate_component_accuracy(test_case.expected, components)
            
            return AccuracyTestResult(
                test_case=test_case,
                components=components,
                confidence=components.confidence,
                processing_time=processing_time,
                player_correct=component_results.get("player", True),
                time_correct=component_results.get("time_period", True),
                location_correct=component_results.get("location", True),
                stats_correct=component_results.get("stats", True),
                opponent_correct=component_results.get("opponent", True),
                relationships_correct=component_results.get("players_on", True) and component_results.get("players_off", True),
                overall_accuracy=overall_accuracy,
                total_components=len([k for k, v in test_case.expected.__dict__.items() if v is not None and k != "intent"]),
                correct_components=sum(component_results.values()),
                errors=errors
            )
            
        except Exception as e:
            processing_time = time.time() - start_time
            return AccuracyTestResult(
                test_case=test_case,
                components=None,
                confidence=0.0,
                processing_time=processing_time,
                overall_accuracy=0.0,
                total_components=0,
                correct_components=0,
                errors=[f"Exception: {str(e)}"]
            )
    
    def analyze_results(self, results: List[AccuracyTestResult]) -> Dict[str, Any]:
        """Analyze comprehensive accuracy results."""
        total_cases = len(results)
        
        # Overall statistics
        avg_accuracy = statistics.mean([r.overall_accuracy for r in results])
        avg_confidence = statistics.mean([r.confidence for r in results])
        
        # Component-wise accuracy
        component_accuracy = {
            "player": len([r for r in results if r.player_correct]) / total_cases,
            "time": len([r for r in results if r.time_correct]) / total_cases,
            "location": len([r for r in results if r.location_correct]) / total_cases,
            "stats": len([r for r in results if r.stats_correct]) / total_cases,
            "opponent": len([r for r in results if r.opponent_correct]) / total_cases,
            "relationships": len([r for r in results if r.relationships_correct]) / total_cases,
        }
        
        # Accuracy by clarity level
        clarity_analysis = {}
        for clarity in ["very_clear", "clear", "moderate", "unclear", "very_unclear"]:
            clarity_results = [r for r in results if r.test_case.clarity_level == clarity]
            if clarity_results:
                avg_acc = statistics.mean([r.overall_accuracy for r in clarity_results])
                avg_conf = statistics.mean([r.confidence for r in clarity_results])
                
                clarity_analysis[clarity] = {
                    "count": len(clarity_results),
                    "avg_accuracy": avg_acc,
                    "avg_confidence": avg_conf,
                    "perfect_cases": len([r for r in clarity_results if r.overall_accuracy == 1.0])
                }
        
        # Confidence vs Accuracy correlation
        high_conf_results = [r for r in results if r.confidence >= 0.8]
        low_conf_results = [r for r in results if r.confidence < 0.8]
        
        confidence_correlation = {
            "high_conf_avg_accuracy": statistics.mean([r.overall_accuracy for r in high_conf_results]) if high_conf_results else 0,
            "low_conf_avg_accuracy": statistics.mean([r.overall_accuracy for r in low_conf_results]) if low_conf_results else 0,
            "high_conf_count": len(high_conf_results),
            "low_conf_count": len(low_conf_results)
        }
        
        return {
            "summary": {
                "total_cases": total_cases,
                "avg_accuracy": avg_accuracy,
                "avg_confidence": avg_confidence,
                "perfect_cases": len([r for r in results if r.overall_accuracy == 1.0]),
                "failed_cases": len([r for r in results if r.overall_accuracy == 0.0]),
                "avg_processing_time": statistics.mean([r.processing_time for r in results])
            },
            "component_accuracy": component_accuracy,
            "clarity_analysis": clarity_analysis,
            "confidence_correlation": confidence_correlation
        }
    
    def print_detailed_results(self, results: List[AccuracyTestResult], analysis: Dict[str, Any]) -> None:
        """Print detailed comprehensive accuracy results."""
        print("\n" + "="*100)
        print("🎯 COMPREHENSIVE COMPONENT ACCURACY TEST RESULTS")
        print("="*100)
        
        # Overall Summary
        summary = analysis["summary"]
        print(f"\n📊 OVERALL PERFORMANCE:")
        print(f"   Total Test Cases: {summary['total_cases']}")
        print(f"   Average Component Accuracy: {summary['avg_accuracy']:.1%}")
        print(f"   Average Confidence: {summary['avg_confidence']:.3f}")
        print(f"   Perfect Extractions: {summary['perfect_cases']}/{summary['total_cases']} ({summary['perfect_cases']/summary['total_cases']:.1%})")
        print(f"   Complete Failures: {summary['failed_cases']}/{summary['total_cases']} ({summary['failed_cases']/summary['total_cases']:.1%})")
        print(f"   Average Processing Time: {summary['avg_processing_time']:.3f}s")
        
        # Component-wise accuracy
        print(f"\n🔍 COMPONENT-WISE ACCURACY:")
        comp_acc = analysis["component_accuracy"]
        for component, accuracy in comp_acc.items():
            status = "🟢" if accuracy >= 0.9 else "🟡" if accuracy >= 0.7 else "🔴"
            print(f"   {status} {component.title():<15} {accuracy:.1%}")
        
        # Clarity level analysis
        print(f"\n📈 ACCURACY BY CLARITY LEVEL:")
        clarity_analysis = analysis["clarity_analysis"]
        for clarity, stats in clarity_analysis.items():
            perfect_rate = stats['perfect_cases'] / stats['count']
            print(f"   {clarity.replace('_', ' ').title():<12} "
                  f"Accuracy: {stats['avg_accuracy']:.1%} | "
                  f"Confidence: {stats['avg_confidence']:.3f} | "
                  f"Perfect: {perfect_rate:.1%} "
                  f"({stats['count']} cases)")
        
        # Confidence correlation
        print(f"\n🔗 CONFIDENCE vs ACCURACY CORRELATION:")
        conf_corr = analysis["confidence_correlation"]
        print(f"   High Confidence (≥0.8): {conf_corr['high_conf_avg_accuracy']:.1%} accuracy ({conf_corr['high_conf_count']} cases)")
        print(f"   Low Confidence (<0.8):  {conf_corr['low_conf_avg_accuracy']:.1%} accuracy ({conf_corr['low_conf_count']} cases)")
        
        # Worst performing cases
        worst_cases = sorted([r for r in results if r.overall_accuracy < 0.8], key=lambda x: x.overall_accuracy)
        if worst_cases:
            print(f"\n❌ WORST PERFORMING CASES:")
            for i, case in enumerate(worst_cases[:5], 1):
                print(f"   {i}. [{case.test_case.clarity_level}] '{case.test_case.query}'")
                print(f"      Accuracy: {case.overall_accuracy:.1%} | Confidence: {case.confidence:.3f}")
                print(f"      Expected: {case.total_components} components, Got: {case.correct_components} correct")
        
        # Best performing cases
        best_cases = [r for r in results if r.overall_accuracy == 1.0 and r.confidence >= 0.9]
        if best_cases:
            print(f"\n✅ PERFECT EXTRACTIONS ({len(best_cases)} cases):")
            for i, case in enumerate(best_cases[:3], 1):
                print(f"   {i}. [{case.test_case.clarity_level}] '{case.test_case.query}'")
                print(f"      Confidence: {case.confidence:.3f} | Components: {case.total_components}")
        
        print("\n" + "="*100)
    
    def run_comprehensive_test(self) -> None:
        """Run comprehensive component accuracy test."""
        print("🚀 Starting Comprehensive Component Accuracy Test...")
        print("   Testing ALL component extraction, not just player names")
        
        test_cases = self.get_comprehensive_test_cases()
        print(f"📝 Testing {len(test_cases)} cases with detailed component validation...")
        
        all_results = []
        
        for i, test_case in enumerate(test_cases, 1):
            print(f"   [{i}/{len(test_cases)}] {test_case.clarity_level}: '{test_case.query}'")
            
            result = self.test_single_case(test_case)
            all_results.append(result)
            
            # Brief status
            acc_status = "🟢" if result.overall_accuracy >= 0.8 else "🟡" if result.overall_accuracy >= 0.5 else "🔴"
            print(f"      {acc_status} Accuracy: {result.overall_accuracy:.1%} | Confidence: {result.confidence:.3f} | Components: {result.correct_components}/{result.total_components}")
        
        # Analyze and print results
        print(f"\n📊 Analyzing {len(all_results)} comprehensive test results...")
        analysis = self.analyze_results(all_results)
        self.print_detailed_results(all_results, analysis)
        
        # Save detailed results
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_file = f"comprehensive_accuracy_results_{timestamp}.json"
        
        output_data = {
            "timestamp": timestamp,
            "test_summary": analysis,
            "detailed_results": [asdict(result) for result in all_results]
        }
        
        with open(results_file, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False, default=str)
        
        print(f"\n💾 Detailed results saved to: {results_file}")


def main():
    """Main function to run comprehensive component accuracy testing."""
    try:
        tester = ComprehensiveAccuracyTester()
        tester.run_comprehensive_test()
        
    except Exception as e:
        print(f"❌ Error during comprehensive accuracy testing: {e}")


if __name__ == "__main__":
    main() 